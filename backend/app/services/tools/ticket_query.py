"""当前登录用户的工单状态只读查询工具；可独立接入 Agent 工具列表。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.queries import list_user_tickets

_TOOL_ERROR = "工单查询暂时无法完成，请稍后重试。"


def _json_result(data: dict[str, Any]) -> str:
    """统一返回 JSON 字符串，避免向模型传递数据库对象或内部异常。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _iso_utc(value: datetime | None) -> str | None:
    """项目的 MySQL DATETIME 按 UTC 存储，输出时显式标注时区。"""
    return f"{value.isoformat()}Z" if value is not None else None


@tool
def query_ticket(
    user_id: Annotated[str, InjectedState("user_id")],
    ticket_no: str | None = None,
) -> str:
    """查询当前用户的投诉、人工及售后工单；可按工单号精确查或查看最近五条。

    用户身份由已认证的图状态注入。返回公开工单号、问题类型、描述、状态、
    创建时间和可选解决时间；查不到他人工单与查不到编号的结果相同。
    """
    normalized_ticket_no = ticket_no.strip() if ticket_no is not None else None
    if normalized_ticket_no is not None and not normalized_ticket_no:
        return _json_result(
            {"status": "invalid", "found": False, "tickets": [], "message": "请提供有效的工单号。"}
        )
    if normalized_ticket_no is not None and len(normalized_ticket_no) > 64:
        return _json_result(
            {"status": "invalid", "found": False, "tickets": [], "message": "工单号过长，请检查后重试。"}
        )

    try:
        # 在短 Session 内完成读取和字段提取，避免模型调用期间持有数据库连接。
        with get_session_factory()() as session:
            rows = list_user_tickets(session, user_id, ticket_no=normalized_ticket_no)
            tickets = [
                {
                    "ticket_no": row.ticket_no,
                    "issue_type": row.issue_type,
                    "description": row.description,
                    "status": row.status,
                    "created_at": _iso_utc(row.created_at),
                    "resolved_at": _iso_utc(row.resolved_at),
                }
                for row in rows
            ]
        return _json_result(
            {
                "status": "ok",
                "found": bool(tickets),
                "tickets": tickets,
                "message": "" if tickets else "当前账号下没有找到匹配的工单。",
            }
        )
    except Exception:
        return _json_result(
            {"status": "error", "found": False, "tickets": [], "message": _TOOL_ERROR}
        )
