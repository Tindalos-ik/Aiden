"""当前登录用户的售后申请只读查询工具；可独立接入 Agent 工具列表。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.queries import list_user_after_sale_requests

_TOOL_ERROR = "查询暂时无法完成，请稍后重试。"


def _json_result(data: dict[str, Any]) -> str:
    """只向模型返回筛选后的 JSON，不传递 ORM 对象或数据库异常。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _iso_utc(value: datetime | None) -> str | None:
    """MySQL DATETIME 在项目内按 UTC 存储，输出时显式标注时区。"""
    return f"{value.isoformat()}Z" if value is not None else None


@tool
def query_after_sale(
    user_id: Annotated[str, InjectedState("user_id")],
    request_no: str | None = None,
    order_no: str | None = None,
) -> str:
    """查询当前登录用户的售后申请；可按申请号、订单号精确查，省略时返回最近五条。

    两个编号同时提供时取交集。返回申请号、订单号、类型、状态、原因、申请金额及
    申请/更新时间和可选完成时间；不返回身份字段或数据库主键。
    """
    normalized_request_no = (request_no or "").strip() or None
    normalized_order_no = (order_no or "").strip() or None
    if normalized_request_no is not None and len(normalized_request_no) > 64:
        return _json_result(
            {"status": "invalid", "found": False, "requests": [], "message": "申请号过长，请检查后重试。"}
        )
    if normalized_order_no is not None and len(normalized_order_no) > 64:
        return _json_result(
            {"status": "invalid", "found": False, "requests": [], "message": "订单号过长，请检查后重试。"}
        )

    try:
        # 查询及字段提取都在短 Session 内完成；模型调用期间不持有数据库连接。
        with get_session_factory()() as session:
            rows = list_user_after_sale_requests(
                session,
                user_id,
                request_no=normalized_request_no,
                order_no=normalized_order_no,
            )
            requests = [
                {
                    "request_no": row.request_no,
                    "order_no": row.order_no,
                    "request_type": row.request_type,
                    "status": row.status,
                    "reason": row.reason,
                    "requested_amount": (
                        str(row.requested_amount) if row.requested_amount is not None else None
                    ),
                    "created_at": _iso_utc(row.created_at),
                    "updated_at": _iso_utc(row.updated_at),
                    "resolved_at": _iso_utc(row.resolved_at),
                }
                for row in rows
            ]
        return _json_result(
            {
                "status": "ok",
                "found": bool(requests),
                "requests": requests,
                "message": "" if requests else "当前账号下没有找到匹配的售后申请。",
            }
        )
    except Exception:
        return _json_result(
            {"status": "error", "found": False, "message": _TOOL_ERROR, "requests": []}
        )
