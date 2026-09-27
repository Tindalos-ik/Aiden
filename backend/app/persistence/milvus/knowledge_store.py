"""Milvus `knowledge` 集合的建表、写入、删除与 dense/BM25 检索封装。

原文与业务字段的权威来源始终是 MySQL 的 knowledge_chunks 表。标量字段用于 Milvus
召回前过滤；三种检索只返回候选 id 和策略分数，有效性判断由 MySQL 侧完成。

幂等的关键：`chunk_id` 就是 MySQL knowledge_chunks 的主键。写入使用 upsert 而不是 insert，
所以“Milvus 已写成功、MySQL 状态尚未回填”时重跑只会覆盖同一条向量，不会产生重复向量。

向量维度从实际模型输出得到并显式传入；集合若已按其他维度存在，这里会直接报错而不是
尝试混写，避免后续写入持续失败却难以定位。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.config.rag import rag_settings

# 集合与字段名在这里集中定义，写入端和后续在线检索端共用同一份常量。
CHUNK_ID_FIELD = "chunk_id"
VECTOR_FIELD = "embedding"
TEXT_FIELD = "text"
SPARSE_FIELD = "sparse_embedding"
HYBRID_CANDIDATE_LIMIT = 50
METADATA_FIELD = "metadata"
# HNSW 的度量方式。建索引与检索必须一致，否则相似度含义不同、排序不可比。
METRIC_TYPE = "COSINE"


def _hit_field(hit: Any, name: str) -> Any:
    """从 pymilvus 命中项里取字段值，兼容 dict 与带 entity 的两种返回形态。

    不同版本的 pymilvus 把 `chunk_id` 放在命中项本身还是放在 `entity` 里并不一致，这里两种
    都试；取不到返回 None，由调用方决定是跳过还是报错。
    """
    if isinstance(hit, dict) and name in hit:
        return hit[name]
    entity = hit.get("entity") if isinstance(hit, dict) else getattr(hit, "entity", None)
    if isinstance(entity, dict):
        return entity.get(name)
    getter = getattr(entity, "get", None)
    if callable(getter):
        return getter(name)
    return None


@dataclass(frozen=True)
class KnowledgeVector:
    """一条待写入 Milvus 的知识向量。

    `text` 是 BM25 function 的正文输入；`content` 保留旧集合的可读正文副本。
    metadata 保存问法、前后块 id 等，不参与召回，在线原文仍从 MySQL 读取。
    """

    chunk_id: str
    embedding: list[float]
    source_type: str
    source_id: str
    source_path: str | None
    category: str
    section_path: str
    content_type: str
    is_key_clause: bool
    chunk_index: int
    content: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeMatch:
    """一次检索的命中项。

    `score` 是当前策略的原始分数：COSINE、BM25 或 RRF，不可跨策略比较。
    `rank` 是 Milvus 返回顺序；有效性与原文一律以 MySQL 为准。
    """

    chunk_id: str
    score: float
    rank: int


class KnowledgeVectorStore:
    """Milvus knowledge 集合的薄封装。

    `pymilvus` 只在实例化时导入：没有装这个可选依赖时，只导入模块不应该失败，从而让
    切分和 MySQL 侧逻辑仍然可用。
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        token: str | None = None,
        collection: str | None = None,
        dimension: int | None = None,
    ) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pymilvus 依赖，请安装 backend/requirements-rag.txt 后再运行建库命令。"
            ) from exc

        self._collection = collection or rag_settings.milvus_collection
        self._dimension = dimension or rag_settings.embedding_dimension
        self._client = MilvusClient(uri=uri or rag_settings.milvus_uri, token=token or rag_settings.milvus_token or "")

    @property
    def collection(self) -> str:
        """当前集合名。"""
        return self._collection

    @property
    def dimension(self) -> int:
        """当前集合使用的向量维度。"""
        return self._dimension

    def ensure_collection(self) -> None:
        """集合不存在时创建；已存在时校验维度和 BM25 字段。

        维度不一致直接报错，因为 Milvus 不允许同一集合内混用不同维度，报错能立刻指出
        需要换集合名或重建集合，而不是在写入阶段留下难以理解的失败。
        """
        if self._client.has_collection(self._collection):
            self.require_bm25_schema()
            return
        from pymilvus import DataType, Function, FunctionType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(CHUNK_ID_FIELD, DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field(VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=self._dimension)
        schema.add_field("source_type", DataType.VARCHAR, max_length=32)
        schema.add_field("source_id", DataType.VARCHAR, max_length=256)
        # 文档相对路径可能较长，Milvus VARCHAR 上限按字节计，这里按中文字符留足空间。
        schema.add_field("source_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("category", DataType.VARCHAR, max_length=512)
        schema.add_field("section_path", DataType.VARCHAR, max_length=1024)
        schema.add_field("content_type", DataType.VARCHAR, max_length=32)
        schema.add_field("is_key_clause", DataType.BOOL)
        schema.add_field("chunk_index", DataType.INT32)
        # 正文随向量一起存放，方便在线检索直接取回候选内容；MySQL 仍是权威原文。
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        # text 由 Milvus 中文 analyzer 分词，BM25 function 自动生成稀疏向量。
        schema.add_field(TEXT_FIELD, DataType.VARCHAR, max_length=65535,
                         enable_analyzer=True, analyzer_params={"type": "chinese"})
        schema.add_field(SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(
            name="text_bm25", input_field_names=[TEXT_FIELD],
            output_field_names=[SPARSE_FIELD], function_type=FunctionType.BM25,
        ))
        schema.add_field(METADATA_FIELD, DataType.JSON)

        index_params = self._client.prepare_index_params()
        # 规模不大、追求低延迟，选 HNSW；dense 向量用 COSINE 距离。
        index_params.add_index(
            field_name=VECTOR_FIELD,
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name=SPARSE_FIELD, index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
        )

    def existing_dimension(self) -> int:
        """读取已有集合的 dense 维度，供不调用向量服务的旧集合迁移使用。"""
        if not self._client.has_collection(self._collection):
            raise RuntimeError(f"Milvus 集合 {self._collection} 不存在。")
        dimension = self._existing_dimension()
        if dimension is None:
            raise RuntimeError(f"无法读取 Milvus 集合 {self._collection} 的向量维度。")
        return dimension

    def require_bm25_schema(self, *, check_dimension: bool = True) -> None:
        """在线只读检查 schema；旧集合给出明确迁移提示，不会自动改动集合。"""
        actual = self.existing_dimension()
        description = self._client.describe_collection(collection_name=self._collection)
        fields = {item["name"] for item in description.get("fields", [])}
        if not {TEXT_FIELD, SPARSE_FIELD}.issubset(fields):
            raise RuntimeError(
                f"Milvus 集合 {self._collection} 是旧 schema，缺少 text/BM25 字段。"
                "请迁移到新集合后设置 MILVUS_KNOWLEDGE_COLLECTION；旧集合不会被修改。"
            )
        if check_dimension and actual != self._dimension:
            raise RuntimeError(
                f"Milvus 集合 {self._collection} 为 {actual} 维，当前 EMBEDDING_DIMENSION "
                f"为 {self._dimension}。请将配置改为与原向量模型一致的维度。"
            )

    def read_embeddings(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        """按 MySQL 主键只读取旧集合 dense 向量；供迁移时避免重新调用 embedding 服务。"""
        if not chunk_ids:
            return {}
        rows = self._client.query(
            collection_name=self._collection,
            filter=f"{CHUNK_ID_FIELD} in {json.dumps(chunk_ids, ensure_ascii=False)}",
            output_fields=[CHUNK_ID_FIELD, VECTOR_FIELD], limit=len(chunk_ids),
        )
        return {str(row[CHUNK_ID_FIELD]): list(row[VECTOR_FIELD]) for row in rows}

    def upsert(self, vectors: list[KnowledgeVector]) -> list[str]:
        """写入或覆盖向量，返回写成功的 chunk_id 列表。

        使用 upsert 语义：同一个 chunk_id 重复写入只覆盖旧数据，因此重跑补齐不会产生重复。
        每批写入后 flush，保证后续操作立刻能看到这批数据。
        """
        if not vectors:
            return []
        rows = [self._to_row(vector) for vector in vectors]
        batch_size = max(1, rag_settings.milvus_insert_batch_size)
        written: list[str] = []
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            self._client.upsert(collection_name=self._collection, data=batch)
            self._client.flush(collection_name=self._collection)
            written.extend(row[CHUNK_ID_FIELD] for row in batch)
        return written

    def search(self, embedding: list[float], *, limit: int, filter: str = "") -> list[KnowledgeMatch]:
        """纯 dense 入口；filter 在 Milvus 召回前生效。"""
        if not embedding or limit <= 0:
            return []
        results = self._client.search(
            collection_name=self._collection,
            data=[embedding],
            limit=limit,
            # ef 至少覆盖当前候选数，避免请求 Top-50 时 HNSW 搜索宽度不足。
            search_params={"metric_type": METRIC_TYPE, "params": {"ef": max(64, limit)}},
            output_fields=[CHUNK_ID_FIELD],
            filter=filter,
        )
        return self._matches(results)

    def search_bm25(self, query: str, *, limit: int, filter: str = "") -> list[KnowledgeMatch]:
        """纯 Milvus 原生 BM25 入口，query 由集合的中文 analyzer 处理。"""
        if not query.strip() or limit <= 0:
            return []
        results = self._client.search(
            collection_name=self._collection, data=[query], anns_field=SPARSE_FIELD,
            search_params={"metric_type": "BM25", "params": {}},
            limit=limit, filter=filter, output_fields=[CHUNK_ID_FIELD],
        )
        return self._matches(results)

    def hybrid_search(
        self, embedding: list[float], query: str, *, limit: int = 50, filter: str = "",
    ) -> list[KnowledgeMatch]:
        """dense 与 BM25 各最多取 50 条；RRF 融合结果也最多返回 50 条。"""
        if not embedding or not query.strip() or limit <= 0:
            return []
        from pymilvus import AnnSearchRequest, RRFRanker

        candidate_limit = min(limit, HYBRID_CANDIDATE_LIMIT)
        requests = [
            AnnSearchRequest(
                data=[embedding], anns_field=VECTOR_FIELD,
                param={"metric_type": METRIC_TYPE, "params": {"ef": max(64, candidate_limit)}},
                limit=candidate_limit, expr=filter or None,
            ),
            AnnSearchRequest(
                data=[query], anns_field=SPARSE_FIELD,
                param={"metric_type": "BM25", "params": {}},
                limit=candidate_limit, expr=filter or None,
            ),
        ]
        results = self._client.hybrid_search(
            collection_name=self._collection, reqs=requests, ranker=RRFRanker(60),
            limit=candidate_limit, output_fields=[CHUNK_ID_FIELD],
        )
        return self._matches(results)

    @staticmethod
    def _matches(results: Any) -> list[KnowledgeMatch]:
        """统一提取三种搜索的主键和原始分数。"""
        matches: list[KnowledgeMatch] = []
        for rank, hit in enumerate(results[0] if results else []):
            # 主键在不同版本里可能落在 chunk_id、id 或 pk 上，逐个尝试后仍取不到就跳过这条命中，
            # 不让单条异常结果影响整次检索。
            chunk_id = next(
                (value for value in (_hit_field(hit, key) for key in (CHUNK_ID_FIELD, "id", "pk")) if value),
                None,
            )
            if not chunk_id:
                continue
            matches.append(
                KnowledgeMatch(
                    chunk_id=str(chunk_id),
                    score=float(_hit_field(hit, "distance") or 0.0),
                    rank=rank,
                )
            )
        return matches

    def delete_by_chunk_ids(self, chunk_ids: Iterable[str]) -> int:
        """按 chunk_id 删除向量，返回请求删除的条数。

        内容更新或块数量变少时用它清理旧向量，避免失效知识继续被检索到。
        """
        ids = [chunk_id for chunk_id in chunk_ids if chunk_id]
        if not ids:
            return 0
        self._client.delete(collection_name=self._collection, ids=ids)
        self._client.flush(collection_name=self._collection)
        return len(ids)

    def inspect(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """只读查看集合与少量标量字段，不读取高维向量或修改集合。"""
        if not self._client.has_collection(self._collection):
            return {"exists": False, "dimension": None, "count": 0, "rows": []}
        stats = self._client.get_collection_stats(collection_name=self._collection)
        rows = self._client.query(
            collection_name=self._collection,
            filter=f'{CHUNK_ID_FIELD} != ""',
            output_fields=[CHUNK_ID_FIELD, "source_type", "source_path", "category", "section_path", "content_type"],
            limit=limit,
            offset=offset,
        )
        row_count = stats.get("row_count")
        return {
            "exists": True,
            "dimension": self._existing_dimension(),
            "count": int(row_count) if row_count is not None else None,
            "rows": rows,
        }

    def _to_row(self, vector: KnowledgeVector) -> dict[str, Any]:
        """把知识向量转成 Milvus 行；空字符串用于占位，避免 VARCHAR 字段收到 None。"""
        return {
            CHUNK_ID_FIELD: vector.chunk_id,
            VECTOR_FIELD: vector.embedding,
            "source_type": vector.source_type,
            "source_id": vector.source_id,
            "source_path": vector.source_path or "",
            "category": vector.category,
            "section_path": vector.section_path,
            "content_type": vector.content_type,
            "is_key_clause": vector.is_key_clause,
            "chunk_index": vector.chunk_index,
            "content": vector.content,
            TEXT_FIELD: vector.text,
            METADATA_FIELD: vector.metadata,
        }

    def _existing_dimension(self) -> int | None:
        """读取已存在集合的向量维度；读取失败时返回 None，交给后续写入暴露问题。"""
        try:
            description = self._client.describe_collection(collection_name=self._collection)
        except Exception:
            return None
        for item in description.get("fields", []):
            if item.get("name") == VECTOR_FIELD:
                params = item.get("params") or {}
                dimension = params.get("dim")
                return int(dimension) if dimension is not None else None
        return None
