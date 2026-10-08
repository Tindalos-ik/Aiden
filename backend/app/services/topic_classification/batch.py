"""真实本地分类器的有限批量入口与脱敏来源导出。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.persistence.mysql import topic_classification as repo
from .data import prepare_input
from .model import EncoderPredictor
from .taxonomy import TAXONOMY_VERSION


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def export_source_batch(
    *, start_at: datetime | None = None, end_at: datetime | None = None,
    after: tuple[datetime, str] | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """导出限量、已做主题专用脱敏的原始问题输入及来源关系；不导出原始PII。"""
    return [
        {
            "source_id": row["source_id"],
            "text": prepare_input(row["raw_question"]),
            "input_hash": row["input_hash"],
            "created_at": _iso(row["created_at"]),
            "conversation_id": row["conversation_id"],
            "user_message_id": row["user_message_id"],
            "user_message_created_at": _iso(row["user_message_created_at"]),
            "matched_review_id": row["matched_review_id"],
        }
        for row in repo.list_source_batch(
            start_at=start_at, end_at=end_at, after=after, limit=limit,
        )
    ]


def _run_with_predictor(
    predictor, *, start_at: datetime | None, end_at: datetime | None,
    after: tuple[datetime, str] | None, limit: int, batch_size: int,
    on_outcome: Callable[[dict[str, Any], dict[str, str]], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000")
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    if predictor.taxonomy_version != TAXONOMY_VERSION:
        raise ValueError("predictor taxonomy version mismatch")
    if not predictor.model_version:
        raise ValueError("predictor model version is missing")

    output = []
    cursor = after
    while len(output) < limit:
        page_size = min(batch_size, limit - len(output))
        # 持久化层已在返回普通数据行之前关闭短读取Session。
        if on_stage is not None:
            on_stage("source_read")
        rows = repo.list_source_batch(
            start_at=start_at, end_at=end_at, after=cursor, limit=page_size,
        )
        if not rows:
            break
        if on_stage is not None:
            on_stage("inference")
        predictions = predictor.predict_batch([row["raw_question"] for row in rows])
        if len(predictions) != len(rows):
            raise ValueError("predictor returned a mismatched batch length")
        for source, prediction in zip(rows, predictions, strict=True):
            if on_stage is not None:
                on_stage("persistence")
            persistence_outcome = repo.persist_prediction(
                source_id=source["source_id"], input_hash_value=source["input_hash"],
                scores=prediction["scores"], predicted_labels=prediction["predicted_labels"],
                status=prediction["status"], model_version=predictor.model_version,
                taxonomy_version=predictor.taxonomy_version,
            )
            output.append({
                "source_id": source["source_id"],
                "input_hash": source["input_hash"],
                "scores": prediction["scores"],
                "predicted_labels": prediction["predicted_labels"],
                "status": prediction["status"],
                "model_version": predictor.model_version,
                "taxonomy_version": predictor.taxonomy_version,
                "persistence_outcome": persistence_outcome,
            })
            if on_outcome is not None:
                on_outcome(output[-1], {"created_at": source["created_at"].isoformat(),
                                       "source_id": source["source_id"]})
        last = rows[-1]
        # 按时间戳和来源ID续读，避免分页期间新增来源导致偏移漂移。
        cursor = (last["created_at"], last["source_id"])
    return output


def run_batch(
    *, artifact_dir: str | Path, device: str = "cpu",
    start_at: datetime | None = None, end_at: datetime | None = None,
    after: tuple[datetime, str] | None = None, limit: int = 100,
    batch_size: int = 32,
    on_outcome: Callable[[dict[str, Any], dict[str, str]], None] | None = None,
    expected_model_version: str | None = None,
    on_stage: Callable[[str], None] | None = None,
    predictor: EncoderPredictor | None = None,
) -> list[dict[str, Any]]:
    """默认真实加载；控制台可复用已校验EncoderPredictor，避免同时驻留两份GPU权重。"""
    if predictor is None:
        predictor = EncoderPredictor(artifact_dir, device=device)
    if expected_model_version is not None and predictor.model_version != expected_model_version:
        raise ValueError("artifact changed after job acceptance")
    return _run_with_predictor(
        predictor, start_at=start_at, end_at=end_at, after=after,
        limit=limit, batch_size=batch_size, on_outcome=on_outcome, on_stage=on_stage,
    )


def statistics(*, model_version: str, start_at: datetime | None = None,
                end_at: datetime | None = None) -> dict[str, Any]:
    return repo.statistics(model_version=model_version, start_at=start_at, end_at=end_at)


def record_human_review(
    *, source_id: str, input_hash: str, staff_id: str, labels: list[str],
    status: str, note: str | None = None,
) -> dict[str, Any]:
    """由显式人工操作提交最终标签；不提供脚本自动伪标注身份。"""
    return repo.record_human_review(
        source_id=source_id, input_hash_value=input_hash, staff_id=staff_id,
        labels=labels, status=status, note=note,
    )
