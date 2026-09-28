"""退款对话的正式申请写入工具；参数由服务端图状态构造。"""

import json
from hashlib import sha256
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from sqlalchemy import select

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import Message, Order, OrderItem
from app.persistence.mysql.service_workflow import submit_request


@tool
def submit_after_sale(
    order_no: str,
    reason: str,
    user_id: Annotated[str, InjectedState("user_id")],
    conversation_id: Annotated[str, InjectedState("conversation_id")],
    assistant_message_id: Annotated[str, InjectedState("assistant_message_id")],
    refund_authorized: Annotated[bool, InjectedState("refund_authorized")],
) -> str:
    """只在上一轮政策证据和本轮明确确认均被服务端核验后提交退款申请。"""
    if refund_authorized is not True:
        return json.dumps({"status": "invalid", "message": "尚未确认提交申请"}, ensure_ascii=False)
    try:
        with get_session_factory().begin() as session:
            order = session.scalar(select(Order).where(Order.user_id == user_id, Order.order_no == order_no))
            if order is None:
                raise LookupError("订单不存在")
            items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)))
            if len(items) != 1:
                raise ValueError("该订单包含多件商品，请在售后申请页面选择具体商品后提交")
            previous = session.scalar(select(Message).where(
                Message.conversation_id == conversation_id,
                Message.sender_role == "assistant", Message.status == "complete",
            ).order_by(Message.created_at.desc()).limit(1))
            if previous is None:
                raise ValueError("未找到已核对的政策说明")
            key = "dialogue:" + sha256(f"{conversation_id}:{assistant_message_id}".encode()).hexdigest()
            row, repeated = submit_request(
                session, user_id=user_id, order_no=order_no, order_item_id=items[0].id,
                request_type="refund", reason=reason, policy_reference=f"rag:{previous.id}",
                submission_key=key, confirmed=True,
                confirmation_message_id=assistant_message_id,
            )
            return json.dumps({"status": "ok", "request_no": row.request_no,
                               "request_status": row.status, "already_exists": repeated}, ensure_ascii=False)
    except (LookupError, ValueError) as exc:
        return json.dumps({"status": "invalid", "message": str(exc)}, ensure_ascii=False)
    except Exception:
        return json.dumps({"status": "error", "message": "申请提交暂时失败，请稍后重试"}, ensure_ascii=False)
