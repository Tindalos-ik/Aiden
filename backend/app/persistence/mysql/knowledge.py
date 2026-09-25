"""knowledge_chunks 表的写入、状态流转与读取接口。

这里的每个函数都在调用时自行创建短生命周期的 Session 并立即关闭，绝不跨向量服务调用
持有会话：建库流程是“写 MySQL（短事务）-> 调 BGE-M3（无会话）-> 写 Milvus -> 回填
MySQL（短事务）”，中途进程中断也不会留下未关闭的连接。

状态机与中断恢复的关系：

* 新块写入时 `vector_status='pending'`、`vector_id` 为空；
* 向量写入 Milvus 成功后回填 `vector_id` 并置为 `vectorized`；
* 可能超过单句/单块上限的块置为 `need_manual_review`，不进入 Milvus；
* 内容变更或块数量减少后，旧块置为 `superseded` 并删除对应向量，避免旧向量继续被检索。

因为 `chunk_key`（来源 + 序号 + 内容哈希）唯一，且 `id` 同时是 Milvus 主键，重复执行导入
只会更新同一行、覆盖同一条向量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, update

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import FAQ, KnowledgeChunk, utc_now_naive

# 向量状态；`terminal` 表示不会再进入 Milvus 的状态。
VECTOR_STATUS_PENDING = "pending"
VECTOR_STATUS_VECTORIZED = "vectorized"
VECTOR_STATUS_NEED_MANUAL_REVIEW = "need_manual_review"
VECTOR_STATUS_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ChunkUpsertResult:
    """一次来源导入的 MySQL 侧结果统计，供命令行输出说明实际写入内容。

    `chunk_ids` 把 chunk_key 映射到实际写入的 knowledge_chunks 主键：对话挖掘需要把候选回填到
    它最终并入的知识块，从而做到“从知识能反查抽取来源”。文档与 FAQ 导入不使用该字段。
    """

    inserted: int
    updated: int
    reset_to_pending: int
    need_manual_review: int
    superseded: int
    chunk_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeChunkRow:
    """读取接口返回的普通 Python 数据；调用方不会拿到 ORM 对象或连接。"""

    id: str
    chunk_key: str
    source_type: str
    source_id: str
    source_path: str | None
    source_title: str | None
    chunk_index: int
    category: str
    questions: list[str]
    answer: str
    embedding_text: str
    embedding_fingerprint: str
    section_path: str
    content_type: str
    is_key_clause: bool
    prev_chunk_id: str | None
    next_chunk_id: str | None
    vector_status: str
    vector_id: str | None
    content_hash: str


@dataclass(frozen=True)
class PendingChunk:
    """待向量化块的最小字段集，足够构造嵌入文本与 Milvus 行。"""

    id: str
    category: str
    questions: list[str]
    answer: str
    embedding_text: str
    source_type: str
    source_id: str
    source_path: str | None
    section_path: str
    content_type: str
    is_key_clause: bool
    chunk_index: int
    prev_chunk_id: str | None
    next_chunk_id: str | None


def _row_to_chunk(chunk: KnowledgeChunk) -> KnowledgeChunkRow:
    """把 ORM 行转成普通数据对象，避免 ORM 对象离开 Session。"""
    return KnowledgeChunkRow(
        id=chunk.id,
        chunk_key=chunk.chunk_key,
        source_type=chunk.source_type,
        source_id=chunk.source_id,
        source_path=chunk.source_path,
        source_title=chunk.source_title,
        chunk_index=chunk.chunk_index,
        category=chunk.category,
        questions=list(chunk.questions or []),
        answer=chunk.answer,
        embedding_text=chunk.embedding_text,
        embedding_fingerprint=chunk.embedding_fingerprint,
        section_path=chunk.section_path,
        content_type=chunk.content_type,
        is_key_clause=bool(chunk.is_key_clause),
        prev_chunk_id=chunk.prev_chunk_id,
        next_chunk_id=chunk.next_chunk_id,
        vector_status=chunk.vector_status,
        vector_id=chunk.vector_id,
        content_hash=chunk.content_hash,
    )


def upsert_source_chunks(
    source_type: str,
    source_id: str,
    chunks: list[dict],
    *,
    source_path: str | None = None,
    source_title: str | None = None,
) -> ChunkUpsertResult:
    """以 chunk_key 为幂等键写入一个来源的全部知识块，并清理本次没有生成的旧块。

    输入 `chunks` 是已经拼好 `embedding_text` 的字典序列，至少包含 chunk_key、chunk_index、
    category、questions、answer、embedding_text、embedding_fingerprint、section_path、
    content_type、is_key_clause、content_hash、needs_manual_review。

    行为要点：

    * 新块插入为 `pending`；已存在且内容哈希未变的块保持原状态（重跑补齐不会把已向量化的
      块打回待处理）；
    * 内容哈希变化、或原本是 `superseded` 又回到当前内容集的块，重置为 `pending` 并清空
      `vector_id`，这样“文档更新后旧向量不会继续作为有效知识”；
    * 本次未生成的同来源旧块（包括内容变化后被新块取代的）置为 `superseded` 并清空
      `vector_id`。

    判定“哪些旧块作废”在本次写入结束后按主键差集完成，不使用时间戳比较：`created_at` 只在
    首次插入时写入，不会随更新刷新，若拿它和本次运行时间比较，所有早于本次运行的行都会被
    误判为旧块，导致已向量化的块每次重导入都被打回待向量化。
    """
    # 同一个 chunk_key 只保留最后一条，与数据库唯一键的“后写覆盖”语义保持一致。
    deduped: dict[str, dict] = {chunk["chunk_key"]: chunk for chunk in chunks}
    factory = get_session_factory()

    with factory() as session:
        with session.begin():
            existing = {
                chunk.chunk_key: chunk
                for chunk in session.scalars(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.source_type == source_type,
                        KnowledgeChunk.source_id == source_id,
                    )
                )
            }

            inserted = updated = reset_to_pending = review_count = 0
            chunk_ids: dict[str, str] = {}
            for chunk_key, payload in deduped.items():
                status = (
                    VECTOR_STATUS_NEED_MANUAL_REVIEW
                    if payload.get("needs_manual_review")
                    else VECTOR_STATUS_PENDING
                )
                current = existing.get(chunk_key)
                if current is None:
                    created = KnowledgeChunk(
                        chunk_key=chunk_key,
                        source_type=source_type,
                        source_id=source_id,
                        source_path=source_path,
                        source_title=source_title,
                        chunk_index=payload["chunk_index"],
                        category=payload["category"],
                        questions=list(payload["questions"]),
                        answer=payload["answer"],
                        embedding_text=payload["embedding_text"],
                        embedding_fingerprint=payload["embedding_fingerprint"],
                        section_path=payload["section_path"],
                        content_type=payload["content_type"],
                        is_key_clause=bool(payload.get("is_key_clause")),
                        vector_status=status,
                        vector_id=None,
                        content_hash=payload["content_hash"],
                    )
                    session.add(created)
                    chunk_ids[chunk_key] = created.id
                    inserted += 1
                    if status == VECTOR_STATUS_NEED_MANUAL_REVIEW:
                        review_count += 1
                    continue

                content_changed = current.content_hash != payload["content_hash"]
                fingerprint_changed = current.embedding_fingerprint != payload["embedding_fingerprint"]
                reactivated = current.vector_status == VECTOR_STATUS_SUPERSEDED
                current.source_path = source_path
                current.source_title = source_title
                current.chunk_index = payload["chunk_index"]
                current.category = payload["category"]
                current.questions = list(payload["questions"])
                current.answer = payload["answer"]
                current.embedding_text = payload["embedding_text"]
                current.embedding_fingerprint = payload["embedding_fingerprint"]
                current.section_path = payload["section_path"]
                current.content_type = payload["content_type"]
                current.is_key_clause = bool(payload.get("is_key_clause"))
                current.content_hash = payload["content_hash"]
                chunk_ids[chunk_key] = current.id
                updated += 1
                if status == VECTOR_STATUS_NEED_MANUAL_REVIEW:
                    current.vector_status = VECTOR_STATUS_NEED_MANUAL_REVIEW
                    current.vector_id = None
                    review_count += 1
                elif content_changed or fingerprint_changed or reactivated:
                    # 内容或拼接模板变了，旧向量不能再当作有效知识，必须重新向量化。
                    current.vector_status = VECTOR_STATUS_PENDING
                    current.vector_id = None
                    current.vectorized_at = None
                    reset_to_pending += 1

            session.flush()
            # 本次没有生成的同来源旧块即作废：取主键差集，与时间无关，重跑结果稳定。
            current_keys = set(deduped)
            stale_ids = [
                chunk.id for chunk_key, chunk in existing.items() if chunk_key not in current_keys
            ]
            superseded = len(stale_ids)
            if stale_ids:
                session.execute(
                    update(KnowledgeChunk)
                    .where(KnowledgeChunk.id.in_(stale_ids))
                    .values(vector_status=VECTOR_STATUS_SUPERSEDED, vector_id=None)
                )

        return ChunkUpsertResult(
            inserted=inserted,
            updated=updated,
            reset_to_pending=reset_to_pending,
            need_manual_review=review_count,
            superseded=superseded,
            chunk_ids=chunk_ids,
        )


def list_superseded_vector_ids(source_type: str, source_id: str) -> list[str]:
    """列出某个来源中已作废的块 id，用于去 Milvus 清理对应向量。

    作废时 `vector_id` 会被清空，但 Milvus 侧的向量主键就是块主键，所以这里返回块 id 即可
    定位需要删除的向量；Milvus 对不存在的 id 删除是幂等的，重复清理不会出错。
    """
    factory = get_session_factory()
    with factory() as session:
        rows = session.scalars(
            select(KnowledgeChunk.id).where(
                KnowledgeChunk.source_type == source_type,
                KnowledgeChunk.source_id == source_id,
                KnowledgeChunk.vector_status == VECTOR_STATUS_SUPERSEDED,
            )
        )
        return list(rows)


def list_sources_with_superseded_chunks() -> list[tuple[str, str]]:
    """列出存在作废块的来源，供清理 Milvus 旧向量时遍历。

    返回 (source_type, source_id) 去重列表；块数量变化和内容更新后的清理都依赖它。
    """
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(KnowledgeChunk.source_type, KnowledgeChunk.source_id)
            .where(KnowledgeChunk.vector_status == VECTOR_STATUS_SUPERSEDED)
            .group_by(KnowledgeChunk.source_type, KnowledgeChunk.source_id)
        )
        return [(source_type, source_id) for source_type, source_id in rows]


def link_neighbour_chunks(source_type: str, source_id: str) -> int:
    """按 chunk_index 顺序回填 prev_chunk_id / next_chunk_id，返回更新的行数。

    前后块指针只在同一来源内建立，供在线检索时按需拉回相邻块补全语义；它不参与 embedding。
    """
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            rows = list(
                session.scalars(
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.source_type == source_type,
                        KnowledgeChunk.source_id == source_id,
                        KnowledgeChunk.vector_status != VECTOR_STATUS_SUPERSEDED,
                    )
                    .order_by(KnowledgeChunk.chunk_index.asc())
                )
            )
            for index, chunk in enumerate(rows):
                chunk.prev_chunk_id = rows[index - 1].id if index > 0 else None
                chunk.next_chunk_id = rows[index + 1].id if index + 1 < len(rows) else None
        return len(rows)


def list_pending_chunks(limit: int | None = None) -> list[PendingChunk]:
    """按写入顺序读取待向量化的块，供补齐命令和正常导入共用。

    只读取 `pending`：`need_manual_review` 需要人工拆分，`vectorized` 已经写过 Milvus，
    `superseded` 已作废。
    """
    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.vector_status == VECTOR_STATUS_PENDING)
            .order_by(KnowledgeChunk.source_type.asc(), KnowledgeChunk.source_id.asc(), KnowledgeChunk.chunk_index.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list(session.scalars(stmt))
        return [
            PendingChunk(
                id=chunk.id,
                category=chunk.category,
                questions=list(chunk.questions or []),
                answer=chunk.answer,
                embedding_text=chunk.embedding_text,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                source_path=chunk.source_path,
                section_path=chunk.section_path,
                content_type=chunk.content_type,
                is_key_clause=bool(chunk.is_key_clause),
                chunk_index=chunk.chunk_index,
                prev_chunk_id=chunk.prev_chunk_id,
                next_chunk_id=chunk.next_chunk_id,
            )
            for chunk in rows
        ]


def mark_chunks_vectorized(chunk_ids: list[str]) -> int:
    """把向量写入成功的块回填 vector_id 并标为已向量化。

    向量库主键就是 knowledge_chunks 主键，因此 `vector_id` 就是块 id 本身；单独保存它是为了
    显式记录“这块确实进过 Milvus”，便于扫描校验。
    """
    if not chunk_ids:
        return 0
    factory = get_session_factory()
    now = utc_now_naive()
    with factory() as session:
        with session.begin():
            result = session.execute(
                update(KnowledgeChunk)
                .where(KnowledgeChunk.id.in_(chunk_ids))
                .values(vector_status=VECTOR_STATUS_VECTORIZED, vector_id=KnowledgeChunk.id, vectorized_at=now)
            )
        return result.rowcount or 0


def count_chunks_by_status() -> dict[str, int]:
    """统计各向量状态的块数，供导入结束后的自检输出。"""
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(KnowledgeChunk.vector_status, func.count()).group_by(KnowledgeChunk.vector_status)
        )
        return {status: count for status, count in rows}


def list_active_faq_rows(limit: int | None = None) -> list[dict[str, str | None]]:
    """读取启用中的 FAQ 行，供建库导入使用。

    只读、不修改任何 FAQ 记录；返回普通字典，调用方在查询结束后不再持有 Session。
    按创建时间和 id 排序，保证同一份数据每次导入顺序一致。
    """
    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(FAQ.id, FAQ.category, FAQ.question, FAQ.answer)
            .where(FAQ.is_active.is_(True))
            .order_by(FAQ.created_at.asc(), FAQ.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            {"id": row.id, "category": row.category, "question": row.question, "answer": row.answer}
            for row in session.execute(stmt)
        ]


def get_chunks_by_ids(chunk_ids: list[str]) -> list[KnowledgeChunkRow]:
    """按主键读取知识块，供后续在线检索按向量命中结果回表取权威原文。"""
    if not chunk_ids:
        return []
    factory = get_session_factory()
    with factory() as session:
        rows = session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(chunk_ids)))
        by_id = {chunk.id: _row_to_chunk(chunk) for chunk in rows}
    return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def list_knowledge_chunks(
    *, category: str | None = None, limit: int = 20, offset: int = 0
) -> list[KnowledgeChunkRow]:
    """分页读取处于 active 的知识块（已向量化或待人工处理），作为后续在线检索的读取接口。

    仅返回没有被 superseded 的块；`category` 可选，用于按分类浏览。后续在线语义检索在此接口
    之上叠加 Milvus 召回，本阶段不涉及检索逻辑本身。
    """
    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.vector_status.in_(
                    [VECTOR_STATUS_VECTORIZED, VECTOR_STATUS_PENDING, VECTOR_STATUS_NEED_MANUAL_REVIEW]
                )
            )
            .order_by(KnowledgeChunk.category.asc(), KnowledgeChunk.chunk_index.asc())
            .limit(limit)
            .offset(offset)
        )
        if category:
            stmt = stmt.where(KnowledgeChunk.category == category)
        return [_row_to_chunk(chunk) for chunk in session.scalars(stmt)]
