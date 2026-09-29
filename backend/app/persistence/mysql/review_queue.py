"""待审队列的短事务读写；模型与向量服务一律在这些事务之外运行。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import get_engine, get_session_factory
from .models import FAQ, KnowledgeChunk, LowConfidenceQuestion, ReviewQueue, utc_now_naive


class ReviewConflict(ValueError):
    """人工审核状态、并发归并版本或输入发生冲突。"""


def _row(row: ReviewQueue) -> dict[str, Any]:
    return {
        "id": row.id, "normalized_question": row.normalized_question,
        "example_answer": row.example_answer, "occurrence_count": row.occurrence_count,
        "review_status": row.review_status, "approved_answer": row.approved_answer,
        "category": row.category, "review_note": row.review_note,
        "rejection_reason": row.rejection_reason, "ingestion_status": row.ingestion_status,
        "ingestion_error": row.ingestion_error, "faq_id": row.faq_id,
        "reviewed_by_id": row.reviewed_by_id, "reviewed_at": row.reviewed_at,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def list_reviews(status: str, limit: int, offset: int) -> dict[str, Any]:
    with get_session_factory()() as session:
        where = ReviewQueue.review_status == status
        total = session.scalar(select(func.count()).select_from(ReviewQueue).where(where)) or 0
        rows = session.scalars(select(ReviewQueue).where(where).order_by(
            ReviewQueue.created_at.desc(), ReviewQueue.id.desc(),
        ).limit(limit).offset(offset)).all()
        return {"items": [_row(row) for row in rows], "total": total}


def review_detail(review_id: str) -> dict[str, Any]:
    with get_session_factory()() as session:
        row = session.get(ReviewQueue, review_id)
        if row is None:
            raise LookupError("待审问题不存在")
        originals = session.scalars(select(LowConfidenceQuestion).where(
            LowConfidenceQuestion.matched_review_id == review_id,
        ).order_by(LowConfidenceQuestion.created_at, LowConfidenceQuestion.id)).all()
        return {**_row(row), "originals": [{
            "id": original.id, "raw_question": original.original_question,
            "created_at": original.created_at, "source": original.entrypoint,
            "reason": original.reason, "retrieval_snapshot": original.retrieval_snapshot,
            "retrieval_status": original.retrieval_status,
            "retrieval_query": original.retrieval_query,
            "processing_error": original.processing_error,
        } for original in originals]}


def pending_originals(batch_size: int = 30, after: tuple[Any, str] | None = None) -> list[tuple[Any, str, str]]:
    """单次只取稳定批量；游标确保失败记录不阻断同轮下一批。"""
    with get_session_factory()() as session:
        stmt = select(LowConfidenceQuestion.created_at, LowConfidenceQuestion.id,
                      LowConfidenceQuestion.original_question).where(
            LowConfidenceQuestion.matched_review_id.is_(None),
        )
        if after is not None:
            stmt = stmt.where(
                (LowConfidenceQuestion.created_at > after[0])
                | ((LowConfidenceQuestion.created_at == after[0]) & (LowConfidenceQuestion.id > after[1]))
            )
        return list(session.execute(stmt.order_by(
            LowConfidenceQuestion.created_at, LowConfidenceQuestion.id,
        ).limit(batch_size)).all())


def record_processing_error(original_id: str, reason: str) -> None:
    with get_session_factory().begin() as session:
        row = session.get(LowConfidenceQuestion, original_id)
        if row is not None and row.matched_review_id is None:
            row.processing_error = reason[:255]


def pending_candidates() -> list[tuple[str, str]]:
    """返回待审候选轻量字段；调用方只向模型提供有限个重排候选。"""
    with get_session_factory()() as session:
        return list(session.execute(select(ReviewQueue.id, ReviewQueue.normalized_question).where(
            ReviewQueue.review_status == "pending",
        ).order_by(ReviewQueue.id)).all())


def merge_original(original_id: str, question: str, answer: str,
                   candidate_id: str | None, observed: list[tuple[str, str]]) -> str:
    """锁绑定同一 MySQL 连接直到提交后释放；候选变化须在事务外重做模型判断。"""
    with get_engine().connect() as connection:
        acquired = connection.scalar(select(func.get_lock("aiden-review-merge", 10)))
        connection.commit()
        if acquired != 1:
            raise ReviewConflict("归并锁暂时不可用，请重试")
        try:
            # Session 绑定固定连接，commit 后也不会把持有命名锁的连接还给连接池。
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                original = session.scalar(select(LowConfidenceQuestion).where(
                    LowConfidenceQuestion.id == original_id,
                ).with_for_update())
                if original is None:
                    raise LookupError("原始问题不存在")
                if original.matched_review_id is not None:
                    return "already"
                current = list(session.execute(select(ReviewQueue.id, ReviewQueue.normalized_question).where(
                    ReviewQueue.review_status == "pending",
                ).order_by(ReviewQueue.id).with_for_update()).all())
                if current != observed:
                    return "changed"
                if candidate_id is None:
                    review = ReviewQueue(normalized_question=question, example_answer=answer, occurrence_count=1)
                    session.add(review)
                    session.flush()
                    original.matched_review_id = review.id
                    outcome = "created"
                else:
                    review = session.get(ReviewQueue, candidate_id)
                    if review is None or review.review_status != "pending":
                        return "changed"
                    review.occurrence_count += 1
                    original.matched_review_id = candidate_id
                    outcome = "merged"
                original.processing_error = None
                session.commit()
                return outcome
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.scalar(select(func.release_lock("aiden-review-merge")))
            connection.commit()


def approve(review_id: str, staff_id: str, answer: str, category: str | None,
            note: str | None) -> dict[str, Any]:
    """审核与 FAQ 插入同事务；重试只复用本行关联的 FAQ。"""
    with get_session_factory().begin() as session:
        row = session.scalar(select(ReviewQueue).where(ReviewQueue.id == review_id).with_for_update())
        if row is None:
            raise LookupError("待审问题不存在")
        if row.review_status == "approved":
            if (row.approved_answer, row.category, row.review_note) != (answer, category, note):
                raise ReviewConflict("问题已经通过审核，不能修改已入库答案")
            return _row(row)
        if row.review_status != "pending":
            raise ReviewConflict("已驳回的问题不能通过审核")
        faq = FAQ(question=row.normalized_question, answer=answer, category=category, is_active=True)
        session.add(faq)
        session.flush()
        row.faq_id = faq.id
        row.approved_answer = answer
        row.category = category
        row.review_note = note
        row.review_status = "approved"
        row.reviewed_by_id = staff_id
        row.reviewed_at = utc_now_naive()
        row.ingestion_status = "pending"
        session.flush()
        return _row(row)


def reject(review_id: str, staff_id: str, reason: str, note: str | None) -> dict[str, Any]:
    with get_session_factory().begin() as session:
        row = session.scalar(select(ReviewQueue).where(ReviewQueue.id == review_id).with_for_update())
        if row is None:
            raise LookupError("待审问题不存在")
        if row.review_status == "rejected" and (row.rejection_reason, row.review_note) == (reason, note):
            return _row(row)
        if row.review_status != "pending":
            raise ReviewConflict("该问题已审核，不能重复驳回")
        row.review_status = "rejected"
        row.rejection_reason = reason
        row.review_note = note
        row.reviewed_by_id = staff_id
        row.reviewed_at = utc_now_naive()
        session.flush()
        return _row(row)


def claim_sync(review_id: str) -> tuple[str, dict[str, str | None]] | None:
    """原子声明同步归属；进程中断的租约到期后可重试。"""
    with get_session_factory().begin() as session:
        row = session.scalar(select(ReviewQueue).where(ReviewQueue.id == review_id).with_for_update())
        if row is None:
            raise LookupError("待审问题不存在")
        if row.review_status != "approved" or row.faq_id is None:
            raise ReviewConflict("只有已通过审核的问题可同步")
        if row.ingestion_status == "ready":
            return None
        now = utc_now_naive()
        if row.sync_token and row.sync_started_at and now - row.sync_started_at < timedelta(minutes=10):
            return None
        faq = session.get(FAQ, row.faq_id)
        if faq is None:
            raise ReviewConflict("已关联的 FAQ 不存在")
        row.sync_token = str(uuid4())
        row.sync_started_at = now
        row.ingestion_status = "pending"
        row.ingestion_error = None
        return row.sync_token, {"id": faq.id, "question": faq.question,
                                "answer": faq.answer, "category": faq.category}


def sync_result(review_id: str, token: str, status: str, error: str | None = None) -> None:
    with get_session_factory().begin() as session:
        row = session.scalar(select(ReviewQueue).where(ReviewQueue.id == review_id).with_for_update())
        if row is None or row.sync_token != token:
            return
        row.ingestion_status = status
        row.ingestion_error = error
        row.sync_token = None
        row.sync_started_at = None


def faq_vector_status(faq_id: str) -> str:
    """只根据本 FAQ 当前有效块的实际状态报告线上是否可检索。"""
    with get_session_factory()() as session:
        statuses = list(session.scalars(select(KnowledgeChunk.vector_status).where(
            KnowledgeChunk.source_type == "faq", KnowledgeChunk.source_id == f"faq:{faq_id}",
            KnowledgeChunk.vector_status != "superseded",
        )))
    if not statuses:
        return "pending"
    if "need_manual_review" in statuses:
        return "manual_review"
    return "ready" if all(status == "vectorized" for status in statuses) else "pending"
