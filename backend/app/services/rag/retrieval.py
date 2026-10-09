"""在线知识检索：query 归一化 -> Milvus 预过滤与召回 -> MySQL 校验 -> 可选精排。

MySQL 是知识正文和有效性的权威源。外部模型与 Milvus 调用期间不持有 MySQL Session；
服务故障抛 RetrievalError，不把失败伪装成空结果。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from app.config.rag import rag_settings
from app.persistence.mysql import knowledge as knowledge_repo
from app.persistence.mysql.knowledge import KnowledgeChunkRow
from app.persistence.milvus.knowledge_store import KnowledgeVectorStore

from .embedding import EmbeddingClient
from .embedding_text import build_query_embedding_text

RetrievalStrategy = Literal["dense", "bm25", "hybrid", "hybrid_rerank"]
_STRATEGIES = frozenset({"dense", "bm25", "hybrid", "hybrid_rerank"})
_FILLER_PREFIX = re.compile(r"^(?:请问|麻烦问一下|我想问一下|帮我看看|您好|你好)[，,\s]*")
_FILLER_SUFFIX = re.compile(r"(?:啊|呀|呢|哈|嘛|呗|哦|吧|啦|哇|请问|谢谢)+[？?！!。\s]*$")
_SPACE = re.compile(r"\s+")
# 只在检索 query 中扩展，不改变知识原文、BM25 索引或向量模板。
_SYNONYMS = {
    "退钱": "退款", "返钱": "退款", "退货款": "退款",
    "寄回": "退货", "退回去": "退货", "换一个": "换货",
    "快递": "物流", "包裹": "物流", "邮费": "运费",
    "开发票": "发票", "多久到": "配送时效",
}


class RetrievalError(RuntimeError):
    """受控的检索失败阶段；底层细节只保留在异常链，不进入浏览器文案。"""

    def __init__(self, stage: Literal["embedding", "milvus", "rerank"]) -> None:
        self.stage = stage
        super().__init__({
            "embedding": "生成问题向量失败。",
            "milvus": "Milvus 集合校验或检索失败。",
            "rerank": "重排模型加载、调用或结果校验失败。",
        }[stage])


@dataclass(frozen=True)
class SemanticKnowledgeHit:
    """通过 MySQL 有效性校验的结果。

    默认 hybrid_rerank 的 score 与 rerank_score 相同，均为原始 reranker logit 的 sigmoid
    值，范围 0..1，越大越相关，但不是校准后的正确率；0.5 可作待校准的初始低置信度
    阈值。其他策略 score 分别是 COSINE、
    BM25 或 RRF 原始分数，rerank_score 为 None，不能共用同一低置信度阈值。
    """

    chunk: KnowledgeChunkRow
    score: float
    rank: int
    rerank_score: float | None = None

    @property
    def chunk_id(self) -> str:
        """稳定知识块主键，四种策略均可用于评估对齐。"""
        return self.chunk.id


def normalize_query(question: str) -> str:
    """去除口语末尾语气词与多余空白，保留实体、否定词和业务条件。"""
    normalized = _FILLER_PREFIX.sub("", _SPACE.sub(" ", question).strip())
    normalized = _FILLER_SUFFIX.sub("", normalized).strip()
    return normalized or question.strip()


def expand_retrieval_query(question: str) -> str:
    """给 BM25 查询附加少量业务同义词；dense 仍只编码归一化问题。"""
    terms = [canonical for colloquial, canonical in _SYNONYMS.items()
             if colloquial in question and canonical not in question]
    return " ".join([question, *dict.fromkeys(terms)])


def _scalar_filter(
    *, category: str | None, source_type: str | None, source_id: str | None,
    content_type: str | None, is_key_clause: bool | None,
) -> str:
    """只允许固定标量列；值用 JSON 引号编码，避免用户文本进入 Milvus 表达式。"""
    values = {
        "category": category, "source_type": source_type,
        "source_id": source_id, "content_type": content_type,
        "is_key_clause": is_key_clause,
    }
    return " and ".join(
        f"{field} == {json.dumps(value, ensure_ascii=False)}"
        for field, value in values.items() if value is not None
    )


@lru_cache(maxsize=2)
def _load_reranker(model_name: str):
    """按进程缓存模型，避免每次在线查询重新加载权重。"""
    try:
        from FlagEmbedding import FlagReranker
    except ImportError as exc:
        raise RetrievalError("rerank") from exc
    return FlagReranker(model_name, use_fp16=False)


def _rerank(question: str, chunks: list[KnowledgeChunkRow]) -> list[float]:
    """使用 MySQL 权威正文计算 pair 分数，并将 logit 映射到 0..1。"""
    if not chunks:
        return []
    pairs = [
        [question, f"分类：{chunk.category}\n问题：{'｜'.join(chunk.questions)}\n答案：{chunk.answer}"]
        for chunk in chunks
    ]
    try:
        raw_scores = _load_reranker(rag_settings.reranker_model).compute_score(pairs)
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError("rerank") from exc
    # 单候选时某些 FlagEmbedding 版本返回 numpy 标量，它不是 Python float。
    if isinstance(raw_scores, (float, int)) or not hasattr(raw_scores, "__len__"):
        raw_scores = [raw_scores]
    if len(raw_scores) != len(chunks):
        raise RetrievalError("rerank")
    # 对极端 logit 使用稳定写法，保持分数在 0..1。
    normalized_scores: list[float] = []
    for score in raw_scores:
        value = float(score)
        if value >= 0:
            normalized_scores.append(1.0 / (1.0 + math.exp(-value)))
        else:
            exponent = math.exp(value)
            normalized_scores.append(exponent / (1.0 + exponent))
    return normalized_scores


def semantic_search(
    question: str,
    *,
    limit: int | None = None,
    candidate_limit: int | None = None,
    strategy: RetrievalStrategy | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    content_type: str | None = None,
    is_key_clause: bool | None = None,
    collection: str | None = None,
    store: KnowledgeVectorStore | None = None,
    client: EmbeddingClient | None = None,
) -> list[SemanticKnowledgeHit]:
    """检索最多 limit 条有效知识；默认 hybrid_rerank，默认结果 Top-10。

    dense、bm25、hybrid、hybrid_rerank 为评估策略入口。candidate_limit 是每一路的
    Top-K（默认 50）；hybrid 各路与 RRF 融合输出均封顶 50，随后精排最多保留
    RAG_RERANK_LIMIT（默认 10）。
    所有可选标量过滤均为精确匹配，在 Milvus 每一路召回前生效；collection 可显式
    覆盖 MILVUS_KNOWLEDGE_COLLECTION，便于迁移验收和隔离评估。
    """
    normalized = normalize_query(question)
    if not normalized:
        return []
    selected = strategy or rag_settings.online_strategy
    if selected not in _STRATEGIES:
        raise ValueError(f"未知检索策略：{selected}")
    result_limit = limit if limit is not None else rag_settings.effective_online_result_limit
    if result_limit <= 0:
        return []
    pool = candidate_limit if candidate_limit is not None else rag_settings.online_candidate_pool
    pool = max(pool, result_limit, 1)
    expression = _scalar_filter(
        category=category, source_type=source_type, source_id=source_id,
        content_type=content_type, is_key_clause=is_key_clause,
    )
    lexical_query = expand_retrieval_query(normalized)

    # 在向量调用前检查集合可用性；失败阶段使用固定文案，底层细节保留在异常链。
    try:
        configured_dimension = client.expected_dimension if client else rag_settings.embedding_dimension
        if store is not None and collection is not None and store.collection != collection:
            raise ValueError("store.collection 与显式 collection 不一致")
        active_store = store or KnowledgeVectorStore(
            collection=collection, dimension=configured_dimension
        )
        if selected != "dense":
            active_store.require_bm25_schema(check_dimension=selected != "bm25")
    except Exception as exc:
        raise RetrievalError("milvus") from exc

    vector = None
    active_client = None
    if selected != "bm25":
        try:
            active_client = client or EmbeddingClient()
            vector = active_client.embed_documents([build_query_embedding_text(normalized)])[0]
        except Exception as exc:
            raise RetrievalError("embedding") from exc

    try:
        if selected == "dense":
            matches = active_store.search(vector, limit=pool, filter=expression)
        elif selected == "bm25":
            matches = active_store.search_bm25(lexical_query, limit=pool, filter=expression)
        else:
            matches = active_store.hybrid_search(vector, lexical_query, limit=pool, filter=expression)
    except Exception as exc:
        raise RetrievalError("milvus") from exc
    if not matches:
        return []

    # 保留 Milvus 的融合顺序，再按主键回表校验当前状态，避免过期向量进入精排。
    unique_matches = list({match.chunk_id: match for match in matches}.values())
    chunks = knowledge_repo.get_vectorized_chunks_by_ids([match.chunk_id for match in unique_matches])
    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    valid = [(match, chunks_by_id[match.chunk_id]) for match in unique_matches
             if match.chunk_id in chunks_by_id]

    if selected == "hybrid_rerank":
        scores = _rerank(normalized, [chunk for _, chunk in valid])
        ranked = sorted(zip(valid, scores), key=lambda item: (-item[1], item[0][0].rank,
                                                               item[0][0].chunk_id))
        top = min(result_limit, max(rag_settings.rerank_limit, 1))
        return [
            SemanticKnowledgeHit(chunk=chunk, score=score, rank=rank, rerank_score=score)
            for rank, ((_, chunk), score) in enumerate(ranked[:top])
        ]
    return [
        SemanticKnowledgeHit(chunk=chunk, score=match.score, rank=rank)
        for rank, (match, chunk) in enumerate(valid[:result_limit])
    ]
