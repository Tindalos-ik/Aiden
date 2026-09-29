"""售后申请与工单的事务边界；所有状态变更在持有行锁时完成。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.persistence.mysql.models import (
    AfterSaleRequest, Conversation, Message, Order, OrderItem, Policy, Ticket, User,
    utc_now_naive,
)

REQUEST_TYPES = {"refund": "退款", "return": "退货", "exchange": "换货"}
REQUEST_TRANSITIONS = {
    "pending": {"approved", "rejected", "cancelled"},
    "approved": {"processing", "awaiting_external_refund", "cancelled"},
    "processing": {"completed"},
}
TICKET_TRANSITIONS = {"open": {"in_progress"}, "in_progress": {"resolved", "closed"}, "resolved": {"closed"}}


def _iso(value: Any) -> str | None:
    return f"{value.isoformat()}Z" if value is not None else None


def request_data(row: AfterSaleRequest, order_no: str | None = None) -> dict[str, Any]:
    return {
        "id": row.id, "requestNo": row.request_no, "userId": row.user_id,
        "orderId": row.order_id, "orderNo": order_no, "orderItemId": row.order_item_id,
        "requestType": row.request_type, "reason": row.reason, "status": row.status,
        "requestedAmount": str(row.requested_amount) if row.requested_amount is not None else None,
        "policyReference": row.policy_reference, "reviewedById": row.reviewed_by_id,
        "reviewedAt": _iso(row.reviewed_at), "processingAt": _iso(row.processing_at),
        "processingById": row.processing_by_id, "resolvedById": row.resolved_by_id,
        "resolvedAt": _iso(row.resolved_at), "decisionNote": row.decision_note,
        "createdAt": _iso(row.created_at), "updatedAt": _iso(row.updated_at),
    }


def ticket_data(row: Ticket) -> dict[str, Any]:
    return {
        "id": row.id, "ticketNo": row.ticket_no, "userId": row.user_id,
        "conversationId": row.conversation_id, "issueType": row.issue_type,
        "description": row.description, "status": row.status,
        "assignedStaffId": row.assigned_staff_id,
        "afterSaleRequestId": row.after_sale_request_id,
        "acceptedAt": _iso(row.accepted_at), "resolvedAt": _iso(row.resolved_at),
        "closedAt": _iso(row.closed_at), "closedById": row.closed_by_id,
        "resolutionNote": row.resolution_note, "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def _active_policies(session: Session, kind: str) -> list[Policy]:
    now = utc_now_naive()
    name = REQUEST_TYPES[kind]
    return list(session.scalars(select(Policy).where(
        Policy.is_active.is_(True), Policy.effective_from <= now,
        or_(Policy.effective_until.is_(None), Policy.effective_until > now),
        or_(Policy.name.contains(name, autoescape=True),
            Policy.policy_key.contains(kind, autoescape=True)),
    ).order_by(Policy.effective_from.desc(), Policy.created_at.desc(), Policy.id.desc()).limit(5)))


def _valid_policy(session: Session, policy_id: str, kind: str) -> bool:
    now = utc_now_naive()
    name = REQUEST_TYPES[kind]
    return session.scalar(select(Policy.id).where(
        Policy.id == policy_id, Policy.is_active.is_(True),
        Policy.effective_from <= now,
        or_(Policy.effective_until.is_(None), Policy.effective_until > now),
        or_(Policy.name.contains(name, autoescape=True),
            Policy.policy_key.contains(kind, autoescape=True)),
    )) is not None


def _link_ticket(session: Session, user_id: str, ticket_no: str, request_id: str) -> None:
    ticket = session.scalar(select(Ticket).where(
        Ticket.ticket_no == ticket_no, Ticket.user_id == user_id).with_for_update())
    if ticket is None:
        raise LookupError("工单不存在")
    if ticket.after_sale_request_id not in (None, request_id):
        raise ValueError("工单已关联其他申请")
    ticket.after_sale_request_id = request_id


def preview_request(session: Session, user_id: str, order_no: str, kind: str) -> dict[str, Any]:
    """返回本人订单行与适用政策；提交时重新核验，预览本身不授权写入。"""
    if kind not in REQUEST_TYPES:
        raise ValueError("不支持的申请类型")
    order = session.scalar(select(Order).where(Order.user_id == user_id, Order.order_no == order_no))
    if order is None:
        raise LookupError("订单不存在")
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == order.id)))
    policies = _active_policies(session, kind)
    return {
        "orderId": order.id, "orderNo": order.order_no, "orderStatus": order.status,
        "items": [{"id": item.id, "name": item.product_name_snapshot,
                   "quantity": item.quantity, "lineTotal": str(item.line_total)} for item in items],
        "policies": [{"id": policy.id, "name": policy.name, "version": policy.version,
                      "content": policy.content} for policy in policies],
    }


def submit_request(
    session: Session, *, user_id: str, order_no: str, order_item_id: str,
    request_type: str, reason: str, policy_reference: str, submission_key: str,
    confirmed: bool, source_ticket_no: str | None = None,
    confirmation_message_id: str | None = None,
) -> tuple[AfterSaleRequest, bool]:
    """服务端复核订单、商品、政策与确认，并锁定用户行防止并发重复申请。"""
    if not confirmed or request_type not in REQUEST_TYPES:
        raise ValueError("请先确认申请类型及提交意愿")
    if not reason.strip() or len(reason.strip()) > 2000:
        raise ValueError("请填写不超过 2000 字的申请原因")
    if not submission_key or len(submission_key) > 100:
        raise ValueError("缺少有效幂等键")
    session.execute(select(User.id).where(User.id == user_id).with_for_update()).scalar_one()
    duplicate = session.scalar(select(AfterSaleRequest).where(
        AfterSaleRequest.user_id == user_id, AfterSaleRequest.submission_key == submission_key))
    if duplicate is not None:
        original_order_no = session.scalar(select(Order.order_no).where(Order.id == duplicate.order_id))
        if (duplicate.request_type, duplicate.reason, duplicate.policy_reference,
            duplicate.order_item_id, original_order_no) != (
            request_type, reason.strip(), policy_reference, order_item_id, order_no):
            raise ValueError("幂等键已用于另一项申请")
        if source_ticket_no:
            _link_ticket(session, user_id, source_ticket_no, duplicate.id)
        return duplicate, True
    order = session.scalar(select(Order).where(Order.user_id == user_id, Order.order_no == order_no))
    if order is None:
        raise LookupError("订单不存在")
    item = session.scalar(select(OrderItem).where(OrderItem.id == order_item_id, OrderItem.order_id == order.id))
    if item is None:
        raise LookupError("订单商品不存在")
    if order.status in {"pending", "cancelled"}:
        raise ValueError("当前订单状态不能提交售后申请")
    if policy_reference.startswith("rag:"):
        # 兼容既有知识库引用；引用和确认都必须来自本人会话的上一轮助手回答。
        previous = session.scalar(select(Message).join(Conversation).where(
            Conversation.user_id == user_id, Message.id == policy_reference[4:],
            Message.sender_role == "assistant", Message.status == "complete",
        ))
        context = (previous.workflow_state or {}).get("refund") if previous else None
        if not previous or not previous.citations or not isinstance(context, dict) or (
            context.get("stage"), context.get("order_no"), context.get("reason")) != (
                "confirm", order_no, reason.strip()):
            raise ValueError("缺少已核对的政策与申请确认")
    elif not _valid_policy(session, policy_reference, request_type):
        raise ValueError("政策已失效，请重新查询后确认")
    if confirmation_message_id is not None or policy_reference.startswith("rag:"):
        current = session.scalar(select(Message).join(Conversation).where(
            Conversation.user_id == user_id,
            Message.id == confirmation_message_id,
            Message.sender_role == "assistant", Message.status == "streaming",
        ))
        if current is None:
            raise ValueError("确认消息不存在")
        if not policy_reference.startswith("rag:"):
            previous = session.scalar(select(Message).where(
                Message.conversation_id == current.conversation_id,
                Message.sender_role == "assistant", Message.status == "complete",
                Message.created_at < current.created_at,
            ).order_by(Message.created_at.desc()).limit(1))
            context = (previous.workflow_state or {}).get("refund") if previous else None
            if not isinstance(context, dict) or (
                context.get("stage"), context.get("order_no"), context.get("reason"),
                context.get("policy_id")) != ("confirm", order_no, reason.strip(), policy_reference):
                raise ValueError("缺少已核对的政策与申请确认")
        elif current.conversation_id != previous.conversation_id:
            raise ValueError("确认消息不存在")
        user_message = session.scalar(select(Message).where(
            Message.conversation_id == current.conversation_id,
            Message.sender_role == "user", Message.created_at < current.created_at,
        ).order_by(Message.created_at.desc()).limit(1))
        if user_message is None or user_message.content.strip() != "确认提交":
            raise ValueError("请按上一轮提示明确回复“确认提交”")
    existing = session.scalar(select(AfterSaleRequest).where(
        AfterSaleRequest.user_id == user_id, AfterSaleRequest.order_id == order.id,
        or_(AfterSaleRequest.order_item_id == item.id, AfterSaleRequest.order_item_id.is_(None)),
        AfterSaleRequest.status.in_(("pending", "approved", "processing", "awaiting_external_refund")),
    ).with_for_update())
    if existing is not None:
        if existing.request_type != request_type:
            raise ValueError("该商品已有其他类型的未结束售后申请")
        if source_ticket_no:
            _link_ticket(session, user_id, source_ticket_no, existing.id)
        return existing, True
    digest = sha256(f"{user_id}:{submission_key}".encode()).hexdigest()[:32].upper()
    row = AfterSaleRequest(
        request_no="AS" + digest, user_id=user_id, order_id=order.id,
        order_item_id=item.id, request_type=request_type, reason=reason.strip(),
        status="pending", submission_key=submission_key, policy_reference=policy_reference,
    )
    session.add(row)
    session.flush()
    if source_ticket_no:
        _link_ticket(session, user_id, source_ticket_no, row.id)
    return row, False


def change_request(session: Session, request_id: str, actor_id: str, status: str,
                   note: str = "", *, user_cancel: bool = False) -> AfterSaleRequest:
    row = session.scalar(select(AfterSaleRequest).where(AfterSaleRequest.id == request_id).with_for_update())
    if row is None or (user_cancel and row.user_id != actor_id):
        raise LookupError("申请不存在")
    if status not in REQUEST_TRANSITIONS.get(row.status, set()) or (user_cancel and status != "cancelled") or (not user_cancel and status == "cancelled"):
        raise ValueError("非法的申请状态转换")
    if row.request_type == "refund" and status in {"processing", "completed"}:
        raise ValueError("退款需由外部支付系统执行，当前不能标记为已完成")
    if row.request_type != "refund" and status == "awaiting_external_refund":
        raise ValueError("只有退款申请可进入待外部退款状态")
    if status in {"rejected", "processing", "completed"} and not note.strip():
        raise ValueError("请填写处理说明")
    now = utc_now_naive()
    row.status = status
    row.decision_note = note.strip() or row.decision_note
    if status in {"approved", "rejected"}:
        row.reviewed_by_id, row.reviewed_at = actor_id, now
    if status in {"processing", "awaiting_external_refund"}:
        row.processing_at, row.processing_by_id = now, actor_id
    if status in {"completed", "rejected", "cancelled"}:
        row.resolved_at, row.resolved_by_id = now, actor_id
    return row


def change_ticket(session: Session, ticket_id: str, actor_id: str, status: str,
                  note: str = "", after_sale_request_id: str | None = None) -> Ticket:
    row = session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    if row is None:
        raise LookupError("工单不存在")
    if status not in TICKET_TRANSITIONS.get(row.status, set()):
        raise ValueError("非法的工单状态转换")
    if after_sale_request_id:
        request = session.scalar(select(AfterSaleRequest).where(
            AfterSaleRequest.id == after_sale_request_id,
            AfterSaleRequest.user_id == row.user_id))
        if request is None:
            raise LookupError("关联申请不存在")
        if row.after_sale_request_id and row.after_sale_request_id != request.id:
            raise ValueError("工单已关联其他申请")
    if row.status == "open":
        if status != "in_progress" or row.assigned_staff_id not in (None, actor_id):
            raise ValueError("工单已由其他员工接手")
        row.assigned_staff_id, row.accepted_at = actor_id, utc_now_naive()
    elif row.assigned_staff_id != actor_id:
        raise ValueError("仅接手员工可处理工单")
    if status in {"resolved", "closed"} and not note.strip() and not row.resolution_note:
        raise ValueError("请填写处理结果")
    if after_sale_request_id:
        row.after_sale_request_id = request.id
    row.status = status
    row.resolution_note = note.strip() or row.resolution_note
    if status == "resolved":
        row.resolved_at = utc_now_naive()
    if status == "closed":
        row.closed_at, row.closed_by_id = utc_now_naive(), actor_id
    return row
