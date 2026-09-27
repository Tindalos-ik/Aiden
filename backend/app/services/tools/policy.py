"""从 MySQL 政策表查询当前有效的退款、退货和售后规则。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain_core.tools import tool

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.queries import list_matching_effective_policies

_TOOL_ERROR = "查询暂时无法完成，请稍后重试。"
_POLICY_LIMIT = 5

# 只用问题中的业务主题选择查询词；不认识的主题返回空，避免把其他政策说成答案。
# 查询词同时匹配 policy_key、name 和 content，以兼容未向量化的示例政策。
_TOPICS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("退货", "退换", "退回", "七天", "无理由", "不想要", "不喜欢", "拒收", "return"),
        ("退货", "退换", "无理由", "return"),
    ),
    (
        ("退款", "退钱", "退费", "返款", "到账", "退到", "refund"),
        ("退款", "退费", "refund"),
    ),
    (
        ("换货", "换新", "换一个", "调换", "exchange"),
        ("换货", "换新", "退换", "exchange"),
    ),
    (
        ("运费险", "退货运费", "寄回运费", "邮费", "运费谁", "运费由谁", "shipping insurance"),
        ("运费险", "运费", "邮费", "shipping_insurance"),
    ),
    (
        ("售后", "维修", "保修", "质保", "三包", "维权"),
        ("售后", "维修", "保修", "质保", "三包", "退货", "退款", "换货", "运费险"),
    ),
)


def _matching_topics(question: str) -> tuple[str, ...]:
    """把自然语言问法映射为有限的政策主题词，去重并保持稳定顺序。"""
    normalized = question.casefold()
    return tuple(
        dict.fromkeys(
            topic
            for aliases, topics in _TOPICS
            if any(alias in normalized for alias in aliases)
            for topic in topics
        )
    )


def _iso_utc(value: datetime | None) -> str | None:
    """MySQL DATETIME 按项目约定存 UTC；输出时显式标注时区。"""
    return f"{value.isoformat()}Z" if value is not None else None


def _json_result(data: dict[str, Any]) -> str:
    """只返回稳定的 JSON 契约，不泄露 ORM 对象或数据库异常。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@tool
def query_policy(question: str) -> str:
    """根据用户问题查询当前 UTC 时间生效的退款、退货及售后政策。

    返回最多五条政策的主题键、名称、版本、正文和 UTC 生效区间；无相关政策时
    返回空列表。仅独立定义工具，不自动加入 Agent 图的可调用工具列表。
    """
    normalized_question = question.strip()
    if not normalized_question:
        return _json_result(
            {"status": "invalid", "found": False, "policies": [], "message": "请提供需要查询的政策问题。"}
        )
    if len(normalized_question) > 500:
        return _json_result(
            {"status": "invalid", "found": False, "policies": [], "message": "问题过长，请简要描述后重试。"}
        )

    topics = _matching_topics(normalized_question)
    if not topics:
        return _json_result(
            {"status": "ok", "found": False, "policies": [], "message": "没有找到与问题相关的现行政策。"}
        )

    try:
        # 在短 Session 内完成查询和字段提取，模型后续生成答案时不持有连接。
        with get_session_factory()() as session:
            rows = list_matching_effective_policies(session, topics, limit=_POLICY_LIMIT)
            policies = [
                {
                    "policy_key": row.policy_key,
                    "name": row.name,
                    "version": row.version,
                    "content": row.content,
                    "effective_from": _iso_utc(row.effective_from),
                    "effective_until": _iso_utc(row.effective_until),
                }
                for row in rows
            ]
        return _json_result(
            {
                "status": "ok",
                "found": bool(policies),
                "policies": policies,
                "message": "" if policies else "没有找到与问题相关的现行政策。",
            }
        )
    except Exception:
        return _json_result(
            {"status": "error", "found": False, "policies": [], "message": _TOOL_ERROR}
        )
