"""员工专用 RAG 建库管理接口；沿用登录 Cookie，所有端点先过员工鉴权。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import current_staff
from app.services.rag import admin_console
from app.services.rag.runtime_control import AlreadyRunningError

router = APIRouter(prefix="/api/rag", tags=["rag-admin"], dependencies=[Depends(current_staff)])
_EVAL_REPORT = Path(__file__).resolve().parents[3] / "evals" / "reports" / "customer_rag_v1.json"


class StartJobRequest(BaseModel):
    """文件名是知识目录内的相对路径；其余任务不接受浏览器传入运行参数。"""

    file: str | None = Field(default=None, max_length=500)


@router.get("/evals/customer-rag-v1")
def customer_rag_v1_report(response: Response) -> dict:
    """读取最近一次完整报告；评估任务原子替换文件后立即可见。"""
    if not _EVAL_REPORT.is_file():
        raise HTTPException(status_code=404, detail="客服 RAG 评估报告不存在")
    response.headers["Cache-Control"] = "no-store"
    with _EVAL_REPORT.open(encoding="utf-8") as report_file:
        return json.load(report_file)


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


@router.post("/jobs/{kind}", status_code=202)
def start_job(kind: str, body: StartJobRequest) -> dict:
    try:
        return admin_console.start_job(kind, body.file)
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
