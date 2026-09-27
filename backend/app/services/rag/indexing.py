"""离线建库编排：Markdown / FAQ -> MySQL -> BGE-M3 -> Milvus -> 回填状态。

建库顺序是固定的，也是本模块存在的主要理由：

1. **先写 MySQL**，每块标为 `pending`、`vector_id` 为空。原文是权威源，即使后面全失败，
   知识也不会丢，重跑可以从这一步之后继续。
2. **再写 Milvus**：先算 embedding，再用 MySQL 主键 upsert 进集合。
3. **成功后回填 MySQL**，写 `vector_id` 并置为 `vectorized`。

中断窗口的处理：Milvus 写入成功、MySQL 状态尚未回填时进程被杀，重跑会重新读到这些
`pending` 块，因为 Milvus 写入用 upsert 且主键就是 MySQL 主键，重新写入只覆盖同一向量，
不产生重复。`scan-pending` 命令就是为这个场景准备的，可反复执行。

文档内容更新或块数量变化时：块内容哈希进入 `chunk_key`，内容变了就是新主键，旧块在导入
时被置为 `superseded` 并从 Milvus 删除，旧向量不会继续作为有效知识。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from app.config.rag import rag_settings
from app.persistence.mysql import knowledge as knowledge_repo
from app.persistence.milvus.knowledge_store import KnowledgeVector, KnowledgeVectorStore

from .chunking import ChunkDraft
from .embedding import EmbeddingClient
from .embedding_text import build_embedding_text, embedding_fingerprint
from .knowledge import (
    build_document_chunks,
    build_faq_chunks,
    content_hash,
    parse_markdown_document,
)
from .length import get_length_meter

SOURCE_TYPE_MARKDOWN = "markdown"
SOURCE_TYPE_FAQ = "faq"

# FAQ 来源的稳定标识前缀：FAQ 行 id 不变时，重复导入只会更新同一来源的块。
FAQ_SOURCE_PREFIX = "faq"


@dataclass(frozen=True)
class SourceImportResult:
    """一个来源的导入结果，用于命令行逐来源说明。"""

    source_id: str
    source_label: str
    chunks: int
    inserted: int
    updated: int
    reset_to_pending: int
    need_manual_review: int
    superseded: int


@dataclass(frozen=True)
class VectorizeResult:
    """一次待向量化补齐的结果统计。"""

    scanned: int
    vectorized: int


def _chunk_key(source_id: str, chunk_index: int, content_digest: str) -> str:
    """生成块的稳定幂等键。

    键包含内容哈希：内容变化会产生新键（旧键在导入时被置为 superseded 并删除向量），
    因此不会出现“旧行的 embedding_text 与新内容不一致”这种静默错误。
    """
    return sha256(f"{source_id}\n{chunk_index}\n{content_digest}".encode("utf-8")).hexdigest()


def _document_source_id(relative_path: str) -> str:
    """文档来源标识：相对目录的 POSIX 风格路径，保证跨平台一致。"""
    return f"markdown:{Path(relative_path).as_posix()}"


def _faq_source_id(faq_id: str) -> str:
    """FAQ 来源标识：按 FAQ 行 id 建立，便于单独判断某条 FAQ 是否已入库。"""
    return f"{FAQ_SOURCE_PREFIX}:{faq_id}"


def _to_payloads(chunks: list[ChunkDraft], source_id: str) -> list[dict]:
    """把切分结果补齐成可直接入库的字典序列。

    `embedding_text` 在这里生成：它只包含 category、questions、answer，章节路径、内容类型、
    关键条款标记和前后块指针都不进入向量化文本。
    """
    fingerprint = embedding_fingerprint()
    payloads: list[dict] = []
    for index, chunk in enumerate(chunks):
        digest = content_hash(chunk.content)
        payloads.append(
            {
                "chunk_key": _chunk_key(source_id, index, digest),
                "chunk_index": index,
                "category": chunk.category,
                "questions": list(chunk.questions),
                "answer": chunk.content,
                "embedding_text": build_embedding_text(chunk.category, chunk.questions, chunk.content),
                "embedding_fingerprint": fingerprint,
                "section_path": chunk.section_path,
                "content_type": chunk.content_type,
                "is_key_clause": chunk.is_key_clause,
                "needs_manual_review": chunk.needs_manual_review,
                "content_hash": digest,
            }
        )
    return payloads


def index_markdown_file(
    path: Path,
    *,
    relative_path: str | None = None,
    source_title: str | None = None,
) -> SourceImportResult:
    """把一个 Markdown 文件切分、写入 MySQL，并建立前后块指针。

    只做“解析 -> 切分 -> 写 MySQL（pending）”，不调用向量服务；向量化由 `vectorize_pending`
    统一处理，这样源文件再多也只需要一次向量化扫描。

    文档必须至少有一个标题：所有知识块的章节归属和 category 都来自标题层级，没有标题的
    文档无法切分，这里直接报错而不是生成归属不明的块。
    """
    text = path.read_text(encoding="utf-8")
    display_path = relative_path or path.name
    parsed = parse_markdown_document(text, fallback_title=source_title or path.stem)
    resolved_title = (source_title or parsed.fallback_title or "").strip()
    if not parsed.sections:
        raise ValueError(f"Markdown 文档缺少标题，无法按章节切分：{display_path}")
    chunks = build_document_chunks(parsed, source_title=resolved_title)
    source_id = _document_source_id(display_path)
    payloads = _to_payloads(chunks, source_id)
    result = knowledge_repo.upsert_source_chunks(
        SOURCE_TYPE_MARKDOWN,
        source_id,
        payloads,
        source_path=display_path,
        source_title=resolved_title,
    )
    knowledge_repo.link_neighbour_chunks(SOURCE_TYPE_MARKDOWN, source_id)
    return SourceImportResult(
        source_id=source_id,
        source_label=display_path,
        chunks=len(payloads),
        inserted=result.inserted,
        updated=result.updated,
        reset_to_pending=result.reset_to_pending,
        need_manual_review=result.need_manual_review,
        superseded=result.superseded,
    )


def index_faq_rows(rows: list[dict[str, str | None]]) -> list[SourceImportResult]:
    """把启用中的 FAQ 行逐条导入为知识块。

    每条 FAQ 独立成一个来源（`faq:<行 id>`）：单条答案变更只影响它自己的块，不会牵动其他
    FAQ 的向量。原有 FAQ 业务数据只读，不会被修改或清空。
    """
    results: list[SourceImportResult] = []
    for row in rows:
        faq_id = str(row.get("id") or "").strip()
        if not faq_id:
            continue
        chunks = build_faq_chunks([row])
        if not chunks:
            continue
        source_id = _faq_source_id(faq_id)
        payloads = _to_payloads(chunks, source_id)
        result = knowledge_repo.upsert_source_chunks(
            SOURCE_TYPE_FAQ,
            source_id,
            payloads,
            source_path=None,
            source_title=(row.get("question") or "").strip() or None,
        )
        knowledge_repo.link_neighbour_chunks(SOURCE_TYPE_FAQ, source_id)
        results.append(
            SourceImportResult(
                source_id=source_id,
                source_label=(row.get("question") or faq_id).strip(),
                chunks=len(payloads),
                inserted=result.inserted,
                updated=result.updated,
                reset_to_pending=result.reset_to_pending,
                need_manual_review=result.need_manual_review,
                superseded=result.superseded,
            )
        )
    return results


def discover_markdown_files(directory: Path, *, pattern: str = "**/*.md") -> list[Path]:
    """按文件名排序列出目录下的 Markdown 文件，保证导入顺序稳定可复现。"""
    if not directory.is_dir():
        raise FileNotFoundError(f"知识目录不存在或不是目录：{directory}")
    return sorted(path for path in directory.glob(pattern) if path.is_file())


def index_markdown_directory(directory: Path, *, pattern: str = "**/*.md") -> list[SourceImportResult]:
    """导入目录下所有 Markdown 文件。"""
    results: list[SourceImportResult] = []
    for path in discover_markdown_files(directory, pattern=pattern):
        relative_path = path.relative_to(directory).as_posix()
        results.append(index_markdown_file(path, relative_path=relative_path))
    return results


def vectorize_pending(
    *,
    limit: int | None = None,
    store: KnowledgeVectorStore | None = None,
    client: EmbeddingClient | None = None,
) -> VectorizeResult:
    """扫描待向量化块，补齐 Milvus 向量并回填 MySQL 状态。

    可以反复执行：已经 `vectorized` 的块不会被再次扫描；上次中断留下的 `pending` 块会被
    重新向量化，而 Milvus 侧是 upsert，所以不会产生重复向量。

    返回值说明本次扫描与写入条数，便于判断是否还有遗留。
    """
    pending = knowledge_repo.list_pending_chunks(limit)
    if not pending:
        return VectorizeResult(scanned=0, vectorized=0)

    active_client = client or EmbeddingClient()
    dimension = active_client.expected_dimension
    active_store = store or KnowledgeVectorStore(dimension=dimension)
    active_store.ensure_collection()

    vectorized = 0
    for start in range(0, len(pending), rag_settings.embedding_batch_size):
        batch = pending[start : start + rag_settings.embedding_batch_size]
        vectors = active_client.embed_documents([item.embedding_text for item in batch])
        rows = [
            KnowledgeVector(
                chunk_id=item.id,
                embedding=vector,
                source_type=item.source_type,
                source_id=item.source_id,
                source_path=item.source_path,
                category=item.category,
                section_path=item.section_path,
                content_type=item.content_type,
                is_key_clause=item.is_key_clause,
                chunk_index=item.chunk_index,
                content=item.answer,
                # BM25 只索引权威正文；问法与分类仍由 dense 模板和标量字段承载。
                text=item.answer,
                metadata={
                    "questions": item.questions,
                    "prev_chunk_id": item.prev_chunk_id,
                    "next_chunk_id": item.next_chunk_id,
                    "embedding_fingerprint": embedding_fingerprint(),
                },
            )
            for item, vector in zip(batch, vectors)
        ]
        written = active_store.upsert(rows)
        # 只有 Milvus 确认写成功后才回填状态；回填前中断则下次重跑，覆盖同一主键。
        vectorized += knowledge_repo.mark_chunks_vectorized(written)
    return VectorizeResult(scanned=len(pending), vectorized=vectorized)


def reindex_vectorized(
    *, store: KnowledgeVectorStore | None = None, client: EmbeddingClient | None = None,
    page_size: int = 200,
) -> int:
    """将 MySQL 已向量化的有效块重写到当前 Milvus 集合，供 BM25 schema 切换使用。

    只读取 MySQL，保持块 id 与状态不变；目标集合应先配置为新名字。按页读取避免一次
    持有全部知识，重复执行仍按主键 upsert。pending 块随后由 vectorize_pending 补齐。
    """
    if page_size <= 0:
        raise ValueError("page_size 必须大于零")
    active_client = client or EmbeddingClient()
    active_store = store or KnowledgeVectorStore(dimension=active_client.expected_dimension)
    active_store.ensure_collection()
    total = 0
    offset = 0
    seen_ids: set[str] = set()
    while True:
        page = knowledge_repo.list_knowledge_chunks(limit=page_size, offset=offset)
        if not page:
            break
        offset += len(page)
        current = [item for item in page if item.vector_status == "vectorized" and item.id not in seen_ids]
        seen_ids.update(item.id for item in current)
        for start in range(0, len(current), rag_settings.embedding_batch_size):
            batch = current[start : start + rag_settings.embedding_batch_size]
            embeddings = active_client.embed_documents([item.embedding_text for item in batch])
            vectors = [
                KnowledgeVector(
                    chunk_id=item.id, embedding=embedding, source_type=item.source_type,
                    source_id=item.source_id, source_path=item.source_path,
                    category=item.category, section_path=item.section_path,
                    content_type=item.content_type, is_key_clause=item.is_key_clause,
                    chunk_index=item.chunk_index, content=item.answer, text=item.answer,
                    metadata={
                        "questions": item.questions,
                        "prev_chunk_id": item.prev_chunk_id,
                        "next_chunk_id": item.next_chunk_id,
                        "embedding_fingerprint": item.embedding_fingerprint,
                    },
                )
                for item, embedding in zip(batch, embeddings)
            ]
            total += len(active_store.upsert(vectors))
    expected = knowledge_repo.count_chunks_by_status().get("vectorized", 0)
    if len(seen_ids) != expected:
        raise RuntimeError(
            f"重建索引只扫描到 {len(seen_ids)}/{expected} 个已向量化块；"
            "请在知识库停止写入时重跑，并调大 page_size。"
        )
    return total


def migrate_legacy_collection(
    source_collection: str, target_collection: str, *, page_size: int = 64,
) -> tuple[int, int]:
    """把旧集合 dense 向量和 MySQL 权威正文复制到新 BM25 集合。

    源集合只读，目标集合创建或按同一 chunk_id upsert；不调用 embedding/reranker 服务，
    不改变 MySQL 状态。返回 (复制块数, 向量维度)。运行期间应暂停知识写入。
    """
    source_name = source_collection.strip()
    target_name = target_collection.strip()
    if not source_name or not target_name or source_name == target_name:
        raise ValueError("必须指定不同的源集合和目标集合名称")
    if page_size <= 0:
        raise ValueError("page_size 必须大于零")

    source_store = KnowledgeVectorStore(collection=source_name)
    dimension = source_store.existing_dimension()
    target_store = KnowledgeVectorStore(collection=target_name, dimension=dimension)
    target_store.ensure_collection()

    copied = 0
    offset = 0
    seen_ids: set[str] = set()
    while True:
        page = knowledge_repo.list_knowledge_chunks(limit=page_size, offset=offset)
        if not page:
            break
        offset += len(page)
        current = [item for item in page if item.vector_status == "vectorized" and item.id not in seen_ids]
        ids = [item.id for item in current]
        seen_ids.update(ids)
        embeddings = source_store.read_embeddings(ids)
        missing = [chunk_id for chunk_id in ids if chunk_id not in embeddings]
        if missing:
            raise RuntimeError(
                f"旧集合 {source_name} 缺少 {len(missing)} 条已向量化块，首个 chunk_id={missing[0]}。"
                "目标集合已写入的块可保留，补齐旧集合后重跑即可。"
            )
        vectors = [
            KnowledgeVector(
                chunk_id=item.id, embedding=embeddings[item.id],
                source_type=item.source_type, source_id=item.source_id,
                source_path=item.source_path, category=item.category,
                section_path=item.section_path, content_type=item.content_type,
                is_key_clause=item.is_key_clause, chunk_index=item.chunk_index,
                content=item.answer, text=item.answer,
                metadata={
                    "questions": item.questions,
                    "prev_chunk_id": item.prev_chunk_id,
                    "next_chunk_id": item.next_chunk_id,
                    "embedding_fingerprint": item.embedding_fingerprint,
                },
            )
            for item in current
        ]
        copied += len(target_store.upsert(vectors))

    expected = knowledge_repo.count_chunks_by_status().get("vectorized", 0)
    if len(seen_ids) != expected:
        raise RuntimeError(
            f"迁移只扫描到 {len(seen_ids)}/{expected} 个已向量化块；"
            "请在知识库停止写入时重跑，并调大 page_size。"
        )
    return copied, dimension


def cleanup_superseded_vectors(store: KnowledgeVectorStore | None = None) -> int:
    """删除已作废块在 Milvus 中的向量，返回请求删除的条数。

    覆盖“文档更新后旧块被 superseded，但删除向量那一步失败”的情况；可重复执行。
    """
    active_store = store or KnowledgeVectorStore()
    active_store.ensure_collection()
    removed = 0
    for source_type, source_id in knowledge_repo.list_sources_with_superseded_chunks():
        ids = knowledge_repo.list_superseded_vector_ids(source_type, source_id)
        if not ids:
            continue
        removed += active_store.delete_by_chunk_ids(ids)
    return removed


def length_measurement_note() -> str:
    """返回当前长度计量的说明行，导入时输出以便核对切分参数是否符合预期。"""
    return get_length_meter().measurement.describe()


if __name__ == "__main__":
    # 在 backend 目录运行：python -m app.services.rag.indexing --source knowledge --target knowledge_bm25
    import argparse
    import json

    parser = argparse.ArgumentParser(description="非破坏性复制旧 Milvus 向量到新 BM25 集合")
    parser.add_argument("--source", required=True, help="旧 dense 集合名称，只读")
    parser.add_argument("--target", required=True, help="新 BM25 集合名称，创建或幂等写入")
    parser.add_argument("--page-size", type=int, default=64)
    args = parser.parse_args()
    count, dim = migrate_legacy_collection(args.source, args.target, page_size=args.page_size)
    print(json.dumps({"source": args.source, "target": args.target,
                      "copied": count, "dimension": dim}, ensure_ascii=False))
