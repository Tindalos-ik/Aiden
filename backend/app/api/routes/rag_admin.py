"""员工专用 RAG 建库管理接口；沿用登录 Cookie，所有端点先过员工鉴权。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from typing import Literal

from app.api.deps import current_staff
from app.services.rag import admin_console
from app.persistence.mysql import review_queue as review_repo
from app.services.rag import review_queue as review_service
from app.services.rag.runtime_control import AlreadyRunningError

router = APIRouter(prefix="/api/rag", tags=["rag-admin"], dependencies=[Depends(current_staff)])
_EVAL_REPORT = Path(__file__).resolve().parents[3] / "evals" / "reports" / "customer_rag_v1.json"


class StartJobRequest(BaseModel):
    """file 为知识目录相对路径；两种评估必须显式选择 evaluationAction，其余任务禁止该字段。"""

    file: str | None = Field(default=None, max_length=500)
    evaluationAction: Literal["restart", "resume"] | None = None

class ApproveReviewRequest(BaseModel):
    approved_answer: str = Field(min_length=1, max_length=10000)
    category: str | None = Field(default=None, max_length=100)
    review_note: str | None = Field(default=None, max_length=4000)


class RejectReviewRequest(BaseModel):
    rejection_reason: Literal["existing_knowledge_not_retrieved", "not_reusable", "outdated", "other"]
    review_note: str | None = Field(default=None, max_length=4000)


def _review_error(exc: LookupError | review_repo.ReviewConflict) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 409, detail=str(exc))


@router.get("/review-queue")
def list_review_queue(
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: Literal["recent", "heat"] = "recent",
    category: str | None = Query(default=None, min_length=1, max_length=100),
    uncategorized: bool = False,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    issue_type: Literal["knowledge_gap", "retrieval_miss", "policy_gap", "service_failure", "unclassified"] | None = None,
) -> dict:
    # 数据库时间为 UTC naive；浏览器显式时区先换算，未带时区约定为 UTC。
    def utc(value: datetime | None) -> datetime | None:
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

    start_at, end_at = utc(start_at), utc(end_at)
    if category is not None and uncategorized:
        raise HTTPException(status_code=422, detail="类目与未分类筛选不能同时选择")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    return review_repo.list_reviews(status, limit, offset, sort=sort, category=category,
                                    uncategorized=uncategorized, start_at=start_at,
                                    end_at=end_at, issue_type=issue_type)


@router.get("/review-queue/{review_id}")
def review_detail(review_id: str) -> dict:
    try:
        return review_repo.review_detail(review_id)
    except LookupError as exc:
        raise _review_error(exc) from exc


@router.post("/review-queue/{review_id}/approve")
def approve_review(review_id: str, body: ApproveReviewRequest,
                   staff: dict = Depends(current_staff)) -> dict:
    answer = body.approved_answer.strip()
    if not answer:
        raise HTTPException(status_code=422, detail="核准答案不能为空")
    category = body.category.strip() or None if body.category is not None else None
    note = body.review_note.strip() or None if body.review_note is not None else None
    try:
        review_repo.approve(review_id, staff["id"], answer, category, note)
        return review_service.sync_approved(review_id)
    except (LookupError, review_repo.ReviewConflict) as exc:
        raise _review_error(exc) from exc


@router.post("/review-queue/{review_id}/reject")
def reject_review(review_id: str, body: RejectReviewRequest,
                  staff: dict = Depends(current_staff)) -> dict:
    note = body.review_note.strip() or None if body.review_note is not None else None
    try:
        review_repo.reject(review_id, staff["id"], body.rejection_reason, note)
        return review_repo.review_detail(review_id)
    except (LookupError, review_repo.ReviewConflict) as exc:
        raise _review_error(exc) from exc


@router.post("/review-queue/{review_id}/retry-ingestion")
def retry_ingestion(review_id: str) -> dict:
    try:
        return review_service.sync_approved(review_id)
    except (LookupError, review_repo.ReviewConflict) as exc:
        raise _review_error(exc) from exc



@router.get("/evals/customer-rag-v1")
def customer_rag_v1_report(response: Response) -> dict:
    """优先新离线报告；旧文件仅加历史标记，不能冒充当前题集基线。"""
    current = _EVAL_REPORT.with_name("customer_rag_v2.json")
    path = current if current.is_file() else _EVAL_REPORT
    return _read_eval_report(path, response)

def _read_eval_report(path: Path, response: Response) -> dict:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="评估报告不存在")
    response.headers["Cache-Control"] = "no-store"
    with path.open(encoding="utf-8") as report_file:
        report = json.load(report_file)
    if "schema_version" not in report:
        report.update(schema_version=1, legacy=True, evaluation_mode="offline_retrieval",
                      execution_environment="historical_unverified")
    return report


@router.get("/evals/customer-workflow-v2")
def customer_workflow_report(response: Response) -> dict:
    return _read_eval_report(_EVAL_REPORT.with_name("customer_workflow_v2.json"), response)


@router.get("/overview")
def overview() -> dict:
    return admin_console.overview()


@router.get("/milvus")
def milvus(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict:
    return admin_console.milvus_snapshot(limit=limit, offset=offset)


@router.get("/preview")
def preview(file: str = Query(min_length=1, max_length=500)) -> dict:
    try:
        return admin_console.preview_document(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chunks")
def chunks(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict:
    return {"items": admin_console.list_chunks(limit=limit, offset=offset)}


@router.get("/mining")
def mining(limit: int = Query(default=30, ge=1, le=100)) -> dict:
    return admin_console.mining_snapshot(limit=limit)


@router.get("/jobs")
def jobs() -> dict:
    return {"items": admin_console.list_jobs()}


@router.get("/evals/checkpoints")
def evaluation_checkpoints() -> dict:
    """离线/在线各自的最后进度摘要；不回传执行单元和业务数据。"""
    return admin_console.evaluation_checkpoints()


@router.post("/jobs/{job_id}/stop", status_code=202)
def stop_evaluation(job_id: str) -> dict:
    """仅请求评估安全退出；202/stopping 不代表模型请求或后台线程已经退出。"""
    try:
        return admin_console.stop_evaluation(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{kind}", status_code=202)
def start_job(kind: str, body: StartJobRequest) -> dict:
    try:
        return admin_console.start_job(kind, body.file, body.evaluationAction)
    except AlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/embedding/start", status_code=202)
def start_embedding() -> dict:
    try:
        return admin_console.start_embedding()
    except AlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"本地向量进程启动失败（{type(exc).__name__}）") from exc


@router.post("/embedding/stop", status_code=202)
def stop_embedding() -> dict:
    try:
        return admin_console.stop_embedding()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
