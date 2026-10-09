"""网页评估的每模式最后一次恢复记录；只存本地 artifact，不建表或启动后台任务。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.config.rag import BACKEND_DIR, EvaluationConfigurationError, rag_settings
from app.config.settings import settings

MODES = {"evaluate": "offline", "evaluate-online": "online"}


class EvaluationCancelled(Exception):
    """完整执行单元已保存后协作退出，不终止模型或 API 进程。"""


def checkpoint_path(mode: str) -> Path:
    return BACKEND_DIR / ".tmp" / "rag-evaluations" / f"{mode}.json"


def report_path(mode: str) -> Path:
    """与网页两种评估各自的正式报告路径保持一致。"""
    return BACKEND_DIR / "evals" / "reports" / (
        "customer_workflow_v2.json" if mode == "online" else "customer_rag_v2.json")


def read_checkpoint(mode: str) -> dict[str, Any] | None:
    """仅读固定模式文件；损坏记录不可恢复，也不能静默冒充没有进度。"""
    path = checkpoint_path(mode)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("mode") != mode or value.get("schema_version") != 1
            or not isinstance(value.get("completed_units"), dict)
            or not isinstance(value.get("evaluationProgress"), dict)
            or not value.get("runId") or not value.get("updatedAt")
            or value.get("status") not in {"queued", "running", "stopping", "cancelled", "interrupted", "failed", "completed"}
            or not value.get("configuration_version")):
            raise ValueError("checkpoint schema")
        # 报告原子替换是提交事实；随后 terminal checkpoint 失写不能让已提交 run 再恢复。
        # 仅读固定模式的报告，且须同时匹配 runId 与 mode；不补写或修改任何文件。
        if value["status"] != "completed" and report_path(mode).exists():
            report = json.loads(report_path(mode).read_text(encoding="utf-8"))
            if (isinstance(report, dict)
                and report.get("metadata", {}).get("evaluation_run_id") == value["runId"]
                and report.get("evaluation_mode") == ("online_workflow" if mode == "online" else "offline_retrieval")):
                value["status"] = "completed"
        return value
    except (ValueError, OSError) as exc:
        raise EvaluationConfigurationError("上次评估进度无法读取，请选择重新开始。") from exc


def save_checkpoint(checkpoint: dict[str, Any]) -> None:
    """复用报告原子替换；调用方持有控制台锁，只有写成功才可公开完成计数。"""
    from evals.run_customer_rag import write_report
    write_report(checkpoint, checkpoint_path(checkpoint["mode"]))


def configuration_version(mode: str, *, dataset: Path | None = None,
                          options: dict[str, Any] | None = None) -> str:
    """便宜兼容检查：只哈希题集、有效非密钥配置及评估相关源码，不访问数据库/模型。

    地址仅参与哈希，不保存地址或 API key；源码变化拒绝混用旧执行单元。
    严格 MySQL/Milvus 快照校验由实际恢复线程执行。
    """
    dataset = dataset or BACKEND_DIR / "evals" / ("customer_workflow_v2.jsonl" if mode == "online" else "customer_rag_v1.jsonl")
    rag_names = ("embedding_base_url", "embedding_model", "embedding_dimension", "embedding_max_seq_tokens",
                 "milvus_uri", "milvus_collection", "online_result_limit", "online_candidate_limit",
                 "online_strategy", "reranker_model", "online_min_rerank_score", "rerank_limit")
    values = {name: getattr(rag_settings, name) for name in rag_names}
    values.update({name: getattr(settings, name) for name in (
        "openai_base_url", "openai_model", "openai_thinking_mode", "max_history_messages",
        "max_message_chars", "max_context_chars")})
    values.update({name: os.getenv(name, "") for name in (
        "EVAL_JUDGE_MODEL", "EVAL_JUDGE_BASE_URL", "EVAL_ACCOUNT_ID", "EVAL_CORPUS_VERSION")})
    # 只记录是否整组复用客服模型，不记录任何 key 或其哈希。
    values["shared_judge_configuration"] = not any(os.getenv(name, "").strip() for name in (
        "EVAL_JUDGE_MODEL", "EVAL_JUDGE_BASE_URL", "EVAL_JUDGE_API_KEY"))
    source_paths = [BACKEND_DIR / "evals" / "run_customer_rag.py"]
    if mode == "online":
        source_paths.extend((BACKEND_DIR / "app" / "agent").rglob("*.py"))
        source_paths.extend((BACKEND_DIR / "app" / "services" / "tools").rglob("*.py"))
        source_paths.extend(BACKEND_DIR / "app" / "persistence" / "mysql" / f"{name}.py"
                            for name in ("queries", "service_workflow"))
    source_paths.extend(BACKEND_DIR / "app" / "persistence" / "mysql" / f"{name}.py"
                        for name in ("models", "knowledge"))
    source_paths.extend(BACKEND_DIR / "app" / "services" / "rag" / f"{name}.py" for name in (
        "retrieval", "knowledge", "embedding", "length", "evaluation_checkpoint"))
    payload = {"mode": mode, "dataset": hashlib.sha256(dataset.read_bytes()).hexdigest(),
               "configuration": values, "options": options or {},
               "source": {str(path.relative_to(BACKEND_DIR)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(source_paths)}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
