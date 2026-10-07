"""主题分类旁路数据的短事务存取。模型推理始终在 Session 外执行。"""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import (
    LowConfidenceQuestion,
    Message,
    ReviewQueue,
    TopicClassificationHumanAudit,
    TopicClassificationHumanReview,
    TopicClassificationPrediction,
    User,
    utc_now_naive,
)
from app.services.topic_classification.data import input_hash
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION, validate_labels


HUMAN_STATUSES = frozenset({"confirmed", "uncertain", "insufficient_context"})


def _scope(start_at: datetime | None, end_at: datetime | None):
    filters = []
    if start_at is not None:
        filters.append(LowConfidenceQuestion.created_at >= start_at)
    if end_at is not None:
        filters.append(LowConfidenceQuestion.created_at < end_at)
    return filters


def list_source_batch(
    *, start_at: datetime | None = None, end_at: datetime | None = None,
    after: tuple[datetime, str] | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """限量游标读取来源；返回脱敏原话输入，不读归并 normalized_question。"""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    predicates = _scope(start_at, end_at)
    if after is not None:
        predicates.append(
            (LowConfidenceQuestion.created_at > after[0])
            | ((LowConfidenceQuestion.created_at == after[0]) & (LowConfidenceQuestion.id > after[1]))
        )
    stmt = (
        select(
            LowConfidenceQuestion.id,
            LowConfidenceQuestion.original_question,
            LowConfidenceQuestion.created_at,
            LowConfidenceQuestion.conversation_id,
            LowConfidenceQuestion.source_user_message_id,
            LowConfidenceQuestion.matched_review_id,
            Message.created_at.label("user_message_created_at"),
        )
        .outerjoin(Message, Message.id == LowConfidenceQuestion.source_user_message_id)
        .where(*predicates)
        .order_by(LowConfidenceQuestion.created_at, LowConfidenceQuestion.id)
        .limit(limit)
    )
    with get_session_factory()() as session:
        rows = session.execute(stmt).all()
    return [
        {
            "source_id": row.id,
            "raw_question": row.original_question,
            "input_hash": input_hash(row.original_question),
            "created_at": row.created_at,
            "conversation_id": row.conversation_id,
            "user_message_id": row.source_user_message_id,
            "user_message_created_at": row.user_message_created_at,
            "matched_review_id": row.matched_review_id,
        }
        for row in rows
    ]


def persist_prediction(
    *, source_id: str, input_hash_value: str, scores: dict[str, float],
    predicted_labels: list[str], status: str, model_version: str, taxonomy_version: str,
) -> str:
    """核对来源hash后按来源/输入/模型版本幂等写预测；旧输入绝不成为current。"""
    labels = validate_labels(predicted_labels)
    if status not in {"predicted", "uncertain"}:
        raise ValueError("invalid prediction status")
    if (status == "uncertain") != (not labels):
        raise ValueError("uncertain predictions must have empty labels")
    if set(scores) != set(LABEL_IDS) or any(
        not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1
        for score in scores.values()
    ):
        raise ValueError("scores must be finite probabilities for the complete taxonomy")
    if taxonomy_version != TAXONOMY_VERSION:
        raise ValueError("prediction taxonomy does not match the current taxonomy")
    with get_session_factory().begin() as session:
        source = session.get(LowConfidenceQuestion, source_id, with_for_update=True)
        if source is None or input_hash(source.original_question) != input_hash_value:
            return "stale_source"
        now = utc_now_naive()
        stmt = insert(TopicClassificationPrediction).values(
            id=str(uuid4()), source_id=source_id, input_hash=input_hash_value,
            scores=scores, predicted_labels=labels, status=status,
            model_version=model_version, taxonomy_version=taxonomy_version,
            created_at=now, updated_at=now,
        ).on_duplicate_key_update(
            scores=scores, predicted_labels=labels, status=status,
            taxonomy_version=taxonomy_version, updated_at=now,
        )
        session.execute(stmt)
        return "persisted"


def _current_predictions(session, *, model_version: str, start_at, end_at):
    rows = session.execute(
        select(
            LowConfidenceQuestion.id,
            LowConfidenceQuestion.original_question,
            TopicClassificationPrediction.input_hash,
            TopicClassificationPrediction.scores,
            TopicClassificationPrediction.predicted_labels,
            TopicClassificationPrediction.status,
            TopicClassificationPrediction.taxonomy_version,
        )
        .join(TopicClassificationPrediction, TopicClassificationPrediction.source_id == LowConfidenceQuestion.id)
        .where(
            TopicClassificationPrediction.model_version == model_version,
            *_scope(start_at, end_at),
        )
        .order_by(LowConfidenceQuestion.created_at, LowConfidenceQuestion.id)
    ).all()
    return [
        row for row in rows
        if row.taxonomy_version == TAXONOMY_VERSION
        and input_hash(row.original_question) == row.input_hash
    ]


def statistics(*, model_version: str, start_at: datetime | None = None,
                end_at: datetime | None = None) -> dict[str, Any]:
    """指定模型版本/current taxonomy下分别统计原始来源、模型、人工与归并唯一数。"""
    with get_session_factory()() as session:
        sources = session.execute(
            select(
                LowConfidenceQuestion.id,
                LowConfidenceQuestion.original_question,
                LowConfidenceQuestion.matched_review_id,
            ).where(*_scope(start_at, end_at))
        ).all()
        current_predictions = _current_predictions(
            session, model_version=model_version, start_at=start_at, end_at=end_at,
        )
        human_rows = session.execute(
            select(
                TopicClassificationHumanReview.source_id,
                TopicClassificationHumanReview.input_hash,
                TopicClassificationHumanReview.labels,
                TopicClassificationHumanReview.status,
            )
            .join(LowConfidenceQuestion, LowConfidenceQuestion.id == TopicClassificationHumanReview.source_id)
            .where(*_scope(start_at, end_at))
        ).all()
        current_hashes = {row.id: input_hash(row.original_question) for row in sources}
        current_human = [
            row for row in human_rows
            if current_hashes.get(row.source_id) == row.input_hash
        ]
        review_ids = {row.matched_review_id for row in sources if row.matched_review_id is not None}
        lifetime_occurrence_count = session.scalar(
            select(func.coalesce(func.sum(ReviewQueue.occurrence_count), 0)).where(ReviewQueue.id.in_(review_ids))
        ) if review_ids else 0

    source_count = len(sources)
    prediction_count = len(current_predictions)
    human_count = len(current_human)

    def tally(rows):
        label_counts = {label: 0 for label in LABEL_IDS}
        statuses: dict[str, int] = {}
        for row in rows:
            statuses[row.status] = statuses.get(row.status, 0) + 1
            labels = row.predicted_labels if hasattr(row, "predicted_labels") else row.labels
            for label in labels:
                label_counts[label] += 1
        return {"source_count": len(rows), "status_counts": statuses, "label_counts": label_counts}

    model_counts = tally(current_predictions)
    model_counts["scope_source_count"] = source_count
    model_counts["unclassified_count"] = source_count - prediction_count
    model_counts["status_counts"]["unclassified"] = source_count - prediction_count
    human_counts = tally(current_human)
    human_counts["scope_source_count"] = source_count
    human_counts["unreviewed_count"] = source_count - human_count
    human_counts["status_counts"]["unreviewed"] = source_count - human_count

    source_reviews = {row.id: row.matched_review_id for row in sources if row.matched_review_id is not None}

    def merged_label_counts(rows, label_field):
        labels_by_review: dict[str, set[str]] = {}
        for row in rows:
            source_id = row.source_id if hasattr(row, "source_id") else row.id
            review_id = source_reviews.get(source_id)
            if review_id is not None:
                labels_by_review.setdefault(review_id, set()).update(getattr(row, label_field))
        return {
            label: sum(label in labels for labels in labels_by_review.values())
            for label in LABEL_IDS
        }

    return {
        "model_version": model_version,
        "taxonomy_version": TAXONOMY_VERSION,
        "source_unique_count": source_count,
        "review_unique_count": len(review_ids),
        "linked_reviews_lifetime_occurrence_count": int(lifetime_occurrence_count),
        "model": model_counts,
        "human": human_counts,
        "merged_review_label_counts": {
            "model": merged_label_counts(current_predictions, "predicted_labels"),
            "human": merged_label_counts(current_human, "labels"),
        },
    }


def record_human_review(
    *, source_id: str, input_hash_value: str, staff_id: str, labels: list[str],
    status: str, note: str | None = None,
) -> dict[str, Any]:
    """人工最终标注只绑定当前脱敏输入，员工身份与变更审计在同一短事务核验。"""
    labels = validate_labels(labels)
    if status not in HUMAN_STATUSES:
        raise ValueError("invalid human review status")
    if status == "confirmed" and not labels:
        raise ValueError("confirmed review requires at least one label")
    if status != "confirmed" and labels:
        raise ValueError("uncertain/insufficient_context reviews must have empty labels")
    with get_session_factory().begin() as session:
        staff = session.get(User, staff_id)
        if staff is None or staff.role != "staff":
            raise LookupError("staff account not found")
        source = session.get(LowConfidenceQuestion, source_id, with_for_update=True)
        if source is None:
            raise LookupError("source not found")
        if input_hash(source.original_question) != input_hash_value:
            raise ValueError("source input changed; export its current hash before review")
        review = session.scalar(select(TopicClassificationHumanReview).where(
            TopicClassificationHumanReview.source_id == source_id,
            TopicClassificationHumanReview.input_hash == input_hash_value,
        ).with_for_update())
        previous_labels = review.labels if review is not None else None
        previous_status = review.status if review is not None else None
        if review is None:
            review = TopicClassificationHumanReview(
                source_id=source_id, input_hash=input_hash_value,
                labels=labels, status=status, note=note, reviewed_by_id=staff_id,
            )
            session.add(review)
            session.flush()
        else:
            review.labels = labels
            review.status = status
            review.note = note
            review.reviewed_by_id = staff_id
        session.add(TopicClassificationHumanAudit(
            human_review_id=review.id, staff_id=staff_id,
            previous_labels=previous_labels, previous_status=previous_status,
            labels=labels, status=status, note=note,
        ))
        return {"source_id": source_id, "input_hash": input_hash_value,
                "labels": labels, "status": status, "reviewed_by_id": staff_id}
