"""工单登记写入；只操作已有的 tickets 表。"""

from __future__ import annotations

from hashlib import sha256
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import Conversation, Message, Ticket, User


class TicketSummary(NamedTuple):
    """工具可公开的工单字段，不包含数据库主键或身份。"""

    ticket_no: str
    status: str
    issue_type: str
    already_exists: bool = False


def _ticket_no(assistant_message_id: str) -> str:
    """同一助手消息始终得到同一公开编号；摘要不暴露内部消息 ID。"""
    return "TK" + sha256(assistant_message_id.encode("utf-8")).hexdigest()[:32].upper()


def _owned_ticket(
    session: Session, ticket_no: str, conversation_id: str, user_id: str
) -> Ticket | None:
    return session.scalar(
        select(Ticket).where(
            Ticket.ticket_no == ticket_no,
            Ticket.conversation_id == conversation_id,
            Ticket.user_id == user_id,
        )
    )


def _register_ticket(
    session: Session,
    *,
    ticket_no: str,
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
    issue_type: str,
    description: str,
) -> TicketSummary:
    if issue_type == "refund_return":
        # 同一用户退款写入串行化，防止不同消息并发确认时各建一张开放工单。
        session.execute(select(User.id).where(User.id == user_id).with_for_update()).scalar_one()
    # 注入状态也必须在写入边界核验，防止错配状态把工单登记到其他会话。
    owned_assistant = session.scalar(
        select(Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Message.id == assistant_message_id,
            Message.conversation_id == conversation_id,
            Message.sender_role == "assistant",
        )
    )
    if owned_assistant is None:
        raise LookupError("会话或助手消息不存在")

    existing = _owned_ticket(session, ticket_no, conversation_id, user_id)
    if existing is not None:
        return TicketSummary(existing.ticket_no, existing.status, existing.issue_type, True)
    if issue_type == "refund_return":
        subject, separator, _ = description.rpartition("；原因：")
        if not separator:
            subject = description
        existing = session.scalar(select(Ticket).where(
            Ticket.user_id == user_id,
            Ticket.issue_type == issue_type,
            Ticket.description.startswith(subject + (separator or ""), autoescape=True),
            Ticket.status == "open",
        ).with_for_update())
        if existing is not None:
            return TicketSummary(existing.ticket_no, existing.status, existing.issue_type, True)

    ticket = Ticket(
        ticket_no=ticket_no,
        conversation_id=conversation_id,
        user_id=user_id,
        issue_type=issue_type,
        description=description,
        status="open",
    )
    session.add(ticket)
    session.flush()
    return TicketSummary(ticket.ticket_no, ticket.status, ticket.issue_type)


def create_user_ticket(
    *,
    user_id: str,
    conversation_id: str,
    assistant_message_id: str,
    issue_type: str,
    description: str,
) -> TicketSummary:
    """核验会话和助手消息归属后登记待处理工单。

    工单号由助手消息 ID 决定。相同消息再次执行返回已有工单；退款类型还锁定
    当前用户行，在同一事务中按订单主题复用已有开放工单。并发同消息由唯一键兜底。
    不更新会话状态，也不创建退款退货申请或人工接单记录。
    """
    ticket_no = _ticket_no(assistant_message_id)
    try:
        with get_session_factory().begin() as session:
            return _register_ticket(
                session,
                ticket_no=ticket_no,
                user_id=user_id,
                conversation_id=conversation_id,
                assistant_message_id=assistant_message_id,
                issue_type=issue_type,
                description=description,
            )
    except IntegrityError:
        # 并发重试可能同时通过预检查；唯一键失败后开启新事务读取赢家。
        with get_session_factory()() as session:
            existing = _owned_ticket(session, ticket_no, conversation_id, user_id)
            if existing is None:
                raise
            return TicketSummary(existing.ticket_no, existing.status, existing.issue_type, True)
