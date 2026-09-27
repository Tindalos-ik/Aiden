"""将当前用户的客服诉求登记为待处理工单。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.persistence.mysql.ticket_write import create_user_ticket

_ISSUE_TYPES = frozenset({"refund_return", "after_sales", "complaint", "human"})
_TOOL_ERROR = "工单登记暂时无法完成，请稍后重试。"


def _json_result(data: dict[str, Any]) -> str:
    """只向模型返回公开业务字段，不传递 ORM 对象或内部异常。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@tool
def create_ticket(
    issue_type: str,
    description: str,
    user_id: Annotated[str, InjectedState("user_id")],
    conversation_id: Annotated[str, InjectedState("conversation_id")],
    assistant_message_id: Annotated[str, InjectedState("assistant_message_id")],
) -> str:
    """登记退款退货办理、售后、投诉或人工请求，返回工单号、状态和类型。

    身份与本轮助手消息由已认证的图状态注入，不接受模型填写。登记只产生一条
    待处理工单，不代表退款申请已提交或人工客服已接入。
    """
    if issue_type not in _ISSUE_TYPES:
        return _json_result({"status": "invalid", "message": "不支持该工单类型。"})
    normalized_description = description.strip()
    if not normalized_description or len(normalized_description) > 4000:
        return _json_result({"status": "invalid", "message": "请提供不超过 4000 字的诉求描述。"})

    try:
        ticket = create_user_ticket(
            user_id=user_id,
            conversation_id=conversation_id,
            assistant_message_id=assistant_message_id,
            issue_type=issue_type,
            description=normalized_description,
        )
        return _json_result(
            {
                "status": "ok",
                "ticket_no": ticket.ticket_no,
                "ticket_status": ticket.status,
                "issue_type": ticket.issue_type,
            }
        )
    except Exception:
        return _json_result({"status": "error", "message": _TOOL_ERROR})
