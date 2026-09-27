"""员工建库控制台的进程内任务与本地向量服务管理。

HTTP 只负责排队和读取快照；导入、模型调用及双写都在后台线程执行。任务锁跨本机
API 进程生效，但任务历史只保存在当前 API 进程内，重启后应从 MySQL 状态恢复判断。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4

from app.config.rag import BACKEND_DIR, rag_settings
from app.persistence.mysql import conversation_mining as mining_repo
from app.persistence.mysql import knowledge as knowledge_repo
from app.services.rag import indexing
from app.services.rag.runtime_control import AlreadyRunningError, MiningRuntimeControl, default_lock_path

_JOB_TYPES = {"import-markdown", "import-markdown-all", "import-faq", "mine", "vectorize", "cleanup", "evaluate"}
_lock = Lock()
_jobs: list[dict[str, Any]] = []
_process: subprocess.Popen[bytes] | None = None
_MAX_HISTORY = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    """异常可能携带带凭据的 URL，只向浏览器给出安全的故障分类。"""
    if isinstance(exc, AlreadyRunningError):
        return str(exc)
    return f"{type(exc).__name__}：操作失败，请检查后端服务、配置和依赖。"


def list_documents() -> list[str]:
    """只列出配置知识目录中的 Markdown 相对路径，不创建或修改源文件。"""
    root = rag_settings.knowledge_directory.resolve()
    return [
        path.relative_to(root).as_posix()
        for path in indexing.discover_markdown_files(root)
        if path.resolve().is_relative_to(root)
    ]


def _document_path(relative_name: str) -> Path:
    root = rag_settings.knowledge_directory.resolve()
    path = (root / relative_name).resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != ".md" or not path.is_file():
        raise ValueError("文件必须是知识目录内现有的 Markdown 文档")
    return path


def preview_document(relative_name: str) -> dict[str, Any]:
    """与正式导入共用解析和切块函数；整个预览不访问 MySQL 或 Milvus。"""
    from app.services.rag.knowledge import build_document_chunks, parse_markdown_document

    path = _document_path(relative_name)
    parsed = parse_markdown_document(path.read_text(encoding="utf-8"), fallback_title=path.stem)
    if not parsed.sections:
        raise ValueError("Markdown 文档缺少标题，无法按章节切分")
    chunks = build_document_chunks(parsed, source_title=parsed.fallback_title)
    return {
        "file": relative_name,
        "title": parsed.fallback_title,
        "chunks": [
            {
                "index": index,
                "sectionPath": chunk.section_path,
                "category": chunk.category,
                "questions": list(chunk.questions),
                "content": chunk.content,
                "contentType": chunk.content_type,
                "isKeyClause": chunk.is_key_clause,
                "needsManualReview": chunk.needs_manual_review,
                "reviewReason": chunk.review_reason,
            }
            for index, chunk in enumerate(chunks)
        ],
    }


def _health_url() -> tuple[str, int] | None:
    parsed = urlparse(rag_settings.embedding_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}/health", parsed.port or (443 if parsed.scheme == "https" else 80)


def embedding_status() -> dict[str, Any]:
    """健康检查只探测已配置地址；外部进程即使占用端口也不会获得本应用的控制权。"""
    target = _health_url()
    running = _process is not None and _process.poll() is None
    exit_code = _process.poll() if _process is not None and not running else None
    healthy = False
    model = None
    dimension = None
    dimension_pending = False
    health_error = None
    if target:
        try:
            import json

            with urlopen(target[0], timeout=1.5) as response:
                payload = json.load(response)
            if isinstance(payload, dict):
                model = payload.get("model")
                dimension = payload.get("dimension")
                status_ok = payload.get("status") == "ok"
                model_matches = model == rag_settings.embedding_model
                dimension_pending = status_ok and model_matches and dimension is None
                healthy = (
                    status_ok
                    and model_matches
                    and (dimension_pending or dimension == rag_settings.embedding_dimension)
                )
                if status_ok and not model_matches:
                    health_error = f"模型不一致：服务为 {model}，后端配置为 {rag_settings.embedding_model}"
                elif status_ok and dimension is not None and dimension != rag_settings.embedding_dimension:
                    health_error = (
                        f"向量维度不一致：服务为 {dimension}，后端配置为 {rag_settings.embedding_dimension}"
                    )
        except (OSError, ValueError, URLError):
            pass
    return {
        "healthy": healthy,
        "managed": running,
        "processState": "running" if running else "failed" if exit_code not in {None, 0} else "stopped",
        "exitCode": exit_code,
        "model": model,
        "dimension": dimension,
        "dimensionPending": dimension_pending,
        "healthError": health_error,
        "canStart": bool(target and urlparse(rag_settings.embedding_base_url).hostname in {"127.0.0.1", "localhost"}),
    }


def start_embedding() -> dict[str, Any]:
    """只启动仓库内固定脚本；解释器仅从服务器环境读取，浏览器不能传命令。"""
    global _process
    target = _health_url()
    parsed = urlparse(rag_settings.embedding_base_url)
    if not target or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.scheme != "http" or parsed.path.rstrip("/") != "/v1":
        raise ValueError("仅支持启动配置为本机 http://127.0.0.1:<端口>/v1 的向量服务")
    with _lock:
        if _process is not None and _process.poll() is None:
            raise AlreadyRunningError("本应用启动的向量服务已经在运行")
        if embedding_status()["healthy"]:
            raise AlreadyRunningError("配置端口已有运行中的外部向量服务，请直接使用；控制台不会接管它")
        try:
            with socket.create_connection(("127.0.0.1", target[1]), timeout=0.5):
                raise AlreadyRunningError("配置端口已有其他服务占用；控制台不会接管或停止它")
        except OSError:
            pass
        python = os.getenv("AIDEN_EMBEDDING_PYTHON", "").strip() or (
            r"D:\bge-m3-env\Scripts\python.exe" if os.name == "nt" else sys.executable
        )
        if not Path(python).is_file():
            raise FileNotFoundError("本地向量服务 Python 环境不存在，请在服务器配置 AIDEN_EMBEDDING_PYTHON")
        script = BACKEND_DIR / "scripts" / "embedding_server.py"
        _process = subprocess.Popen(
            [python, str(script), "--model", rag_settings.embedding_model, "--host", "127.0.0.1", "--port", str(target[1])],
            cwd=BACKEND_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"state": "starting", "managed": True}


def stop_embedding() -> dict[str, Any]:
    """仅终止当前 API 进程保存的 Popen 句柄，绝不根据端口或 PID 搜索外部服务。"""
    global _process
    with _lock:
        process = _process
        if process is None or process.poll() is not None:
            _process = None
            raise ValueError("没有由本应用启动且仍在运行的向量服务")
        process.terminate()
        _process = None
    return {"state": "stopping", "managed": False}


def stop_owned_embedding_on_shutdown() -> None:
    """API 退出时清理当前进程创建的子进程，不触碰外部服务。"""
    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
        _process = None


def overview() -> dict[str, Any]:
    """数据库统计与配置分开返回，配置白名单不包含连接凭据。"""
    database_error = None
    try:
        chunks = knowledge_repo.count_chunks_by_status()
        batches = mining_repo.count_batches_by_status()
        candidates = mining_repo.count_candidates_by_status()
    except Exception as exc:
        chunks, batches, candidates = {}, {}, {}
        database_error = _safe_error(exc)
    documents_error = None
    try:
        documents = list_documents()
    except (OSError, ValueError) as exc:
        documents = []
        documents_error = _safe_error(exc)
    return {
        "embedding": embedding_status(),
        "chunksByStatus": chunks,
        "batchesByStatus": batches,
        "candidatesByStatus": candidates,
        "milvus": {
            "collection": rag_settings.milvus_collection,
            "dimension": rag_settings.embedding_dimension,
            "metric": "COSINE",
            "index": "HNSW",
        },
        "documents": documents,
        "databaseError": database_error,
        "documentsError": documents_error,
    }


def milvus_snapshot(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """员工只读核对 Milvus 实际数据；回表状态只用于解释样本，不用于在线检索。"""
    from app.persistence.milvus.knowledge_store import KnowledgeVectorStore

    try:
        snapshot = KnowledgeVectorStore().inspect(limit=limit, offset=offset)
    except Exception as exc:
        return {
            "collection": rag_settings.milvus_collection,
            "exists": None,
            "dimension": None,
            "count": None,
            "items": [],
            "error": _safe_error(exc),
            "mysqlError": None,
        }

    rows = snapshot.pop("rows")
    mysql_error = None
    try:
        by_id = {
            row.id: row
            for row in knowledge_repo.get_chunks_by_ids([str(item["chunk_id"]) for item in rows])
        }
    except Exception as exc:
        by_id = {}
        mysql_error = _safe_error(exc)
    return {
        "collection": rag_settings.milvus_collection,
        **snapshot,
        "items": [
            {
                "chunkId": str(item["chunk_id"]),
                "sourceType": item.get("source_type") or "",
                "sourcePath": item.get("source_path") or "",
                "category": item.get("category") or "",
                "sectionPath": item.get("section_path") or "",
                "contentType": item.get("content_type") or "",
                "mysqlStatus": "unavailable" if mysql_error else (
                    by_id[str(item["chunk_id"])].vector_status
                    if str(item["chunk_id"]) in by_id else "missing"
                ),
            }
            for item in rows
        ],
        "error": None,
        "mysqlError": mysql_error,
    }


def mining_snapshot(limit: int = 30) -> dict[str, Any]:
    """批次与候选的只读摘要，不回传源消息、会话原文或模型凭据。"""
    from sqlalchemy import select
    from app.persistence.mysql.database import get_session_factory
    from app.persistence.mysql.models import ConversationMiningBatch, ConversationQaCandidate

    with get_session_factory()() as session:
        batches = list(session.scalars(
            select(ConversationMiningBatch).order_by(ConversationMiningBatch.updated_at.desc()).limit(limit)
        ))
        items = [
            {
                "id": row.id,
                "runId": row.run_id,
                "status": row.status,
                "turnCount": row.turn_count,
                "candidateCount": row.candidate_count,
                # 持久化错误可能来自外部模型响应；不回传可能包含网关凭据的原始异常文本。
                "error": "此批次处理失败，请检查任务状态后重试" if row.status == "failed" else row.error_message,
                "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in batches
        ]
        candidates = {}
        for status in ("staged", "promoted", "rejected"):
            rows = session.scalars(
                select(ConversationQaCandidate)
                .where(ConversationQaCandidate.status == status)
                .order_by(ConversationQaCandidate.created_at.desc(), ConversationQaCandidate.id.desc())
                .limit(limit)
            )
            candidates[status] = [
                {
                    "candidate_id": row.id,
                    "question": row.question,
                    "answer": row.answer,
                    "category": row.category,
                    "status": row.status,
                    "rejection_reason": row.rejection_reason,
                }
                for row in rows
            ]
    return {
        "batches": items,
        "candidates": candidates,
    }


def list_chunks(limit: int, offset: int) -> list[dict[str, Any]]:
    """MySQL 是块状态与正文的权威来源，返回当前有效块供员工核对。"""
    rows = knowledge_repo.list_knowledge_chunks(limit=limit, offset=offset)
    return [
        {
            "id": row.id,
            "sourceType": row.source_type,
            "sourcePath": row.source_path,
            "category": row.category,
            "sectionPath": row.section_path,
            "questions": row.questions,
            "content": row.answer,
            "contentType": row.content_type,
            "isKeyClause": row.is_key_clause,
            "vectorStatus": row.vector_status,
            "vectorId": row.vector_id,
        }
        for row in rows
    ]


def _execute_job(kind: str, job: dict[str, Any], control: MiningRuntimeControl, relative_name: str | None) -> None:
    try:
        if kind == "import-markdown":
            # 文件路径已在 HTTP 接入时校验；执行前再解析一次，防止预览后路径被替换。
            path = _document_path(relative_name) if relative_name else None
            if path is None:
                raise ValueError("请选择知识目录中的 Markdown 文件")
            result = indexing.index_markdown_file(path, relative_path=relative_name)
        elif kind == "import-markdown-all":
            imported = []
            for name in list_documents():
                _set_progress(job, f"导入文档：{name}")
                imported.append(indexing.index_markdown_file(_document_path(name), relative_path=name))
            result = {
                "sources": len(imported),
                "chunks": sum(item.chunks for item in imported),
                "inserted": sum(item.inserted for item in imported),
                "updated": sum(item.updated for item in imported),
                "needManualReview": sum(item.need_manual_review for item in imported),
                "superseded": sum(item.superseded for item in imported),
            }
        elif kind == "import-faq":
            imported = indexing.index_faq_rows(knowledge_repo.list_active_faq_rows())
            result = {
                "sources": len(imported),
                "chunks": sum(item.chunks for item in imported),
                "inserted": sum(item.inserted for item in imported),
                "updated": sum(item.updated for item in imported),
                "needManualReview": sum(item.need_manual_review for item in imported),
                "superseded": sum(item.superseded for item in imported),
            }
        elif kind == "mine":
            from app.services.rag.conversation_mining import run_pipeline
            from app.services.rag.extraction import ConversationExtractionClient

            mining_control = MiningRuntimeControl(default_lock_path())
            mining_control.acquire()
            try:
                # 向量补齐由独立可观察任务触发；挖掘一轮只写候选与正式知识。
                result = run_pipeline(client=ConversationExtractionClient(), vectorize=False,
                                      on_progress=lambda note: _set_progress(job, note))
            finally:
                mining_control.release()
        elif kind == "vectorize":
            result = indexing.vectorize_pending()
        elif kind == "evaluate":
            # 与建库任务共用锁，避免控制台在评估期间改动知识块和向量。
            from evals.run_customer_rag import run, write_report

            report = run(
                collection=rag_settings.milvus_collection,
                on_progress=lambda done, total, strategy, case_id: _set_progress(
                    job, f"已完成 {done}/{total}：{strategy} · {case_id}"
                ),
            )
            write_report(report, BACKEND_DIR / "evals" / "reports" / "customer_rag_v1.json")
            strategy_count = len(report["strategies"])
            result = {"questions": len(report["cases"]) // strategy_count,
                      "strategies": strategy_count, "generatedAt": report["generated_at"]}
        else:
            result = {"removed": indexing.cleanup_superseded_vectors()}
        with _lock:
            job["status"] = "completed"
            job["result"] = [asdict(item) for item in result] if isinstance(result, list) else (
                asdict(result) if hasattr(result, "__dataclass_fields__") else result
            )
    except Exception as exc:
        with _lock:
            job["status"] = "failed"
            job["error"] = _safe_error(exc)
    finally:
        with _lock:
            job["finishedAt"] = _now()
        control.release()


def _set_progress(job: dict[str, Any], note: str) -> None:
    with _lock:
        job["progress"] = note[:200]


def start_job(kind: str, relative_name: str | None = None) -> dict[str, Any]:
    """全控制台同一时刻只运行一项写任务，跨 API 进程用同一把文件锁拦截。"""
    if kind not in _JOB_TYPES:
        raise ValueError("不支持的任务类型")
    if kind == "import-markdown":
        if not relative_name:
            raise ValueError("请选择知识目录中的 Markdown 文件")
        _document_path(relative_name)
    control = MiningRuntimeControl(default_lock_path().with_name("aiden-rag-console.lock"))
    with _lock:
        if any(item["status"] in {"queued", "running"} for item in _jobs):
            raise AlreadyRunningError("已有建库任务正在运行，请等待完成")
        control.acquire()
        job: dict[str, Any] = {
            "id": str(uuid4()), "kind": kind, "status": "queued", "progress": None,
            "createdAt": _now(), "startedAt": None, "finishedAt": None,
            "error": None, "result": None,
        }
        _jobs.insert(0, job)
        del _jobs[_MAX_HISTORY:]

    def worker() -> None:
        with _lock:
            job["status"] = "running"
            job["startedAt"] = _now()
        _execute_job(kind, job, control, relative_name)

    try:
        Thread(target=worker, name=f"rag-{kind}", daemon=True).start()
    except RuntimeError:
        with _lock:
            _jobs.remove(job)
        control.release()
        raise
    return dict(job)


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        return [dict(job) for job in _jobs]
