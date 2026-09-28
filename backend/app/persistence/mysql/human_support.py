"""人工客服会话的状态转换与消息写入。

所有写入先锁定会话行，令转接、接单、留言和结束服务按同一顺序串行。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .chat import _conversation_from_model, _message_from_model
from .database import get_session_factory
from .models import Conversation, Message, User, utc_now_naive


def _system_message(session: Any, conversation: Conversation, content: str) -> None:
    now = utc_now_naive()
    session.add(Message(conversation_id=conversation.id, sender_role="system", content=content,
                        status="complete", created_at=now))
    conversation.updated_at = now
    conversation.last_message_preview = content[:120]


def request_handoff(conversation_id: str, user_id: str) -> dict[str, Any]:
    """只允许所属用户从 bot 转入 waiting；重复申请返回现态，不重复写消息。"""
    with get_session_factory().begin() as session:
        conversation = session.scalar(select(Conversation).options(selectinload(Conversation.assigned_staff))
                                      .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
                                      .with_for_update())
        if conversation is None:
            raise LookupError("会话不存在")
        if conversation.status == "closed":
            raise ValueError("会话已结束，请新建会话")
        if conversation.status == "bot":
            streaming = session.scalar(select(Message.id).where(
                Message.conversation_id == conversation_id, Message.sender_role == "assistant",
                Message.status == "streaming").limit(1))
            if streaming:
                raise ValueError("智能客服仍在生成回答，请稍后再转人工")
            conversation.status = "waiting"
            _system_message(session, conversation, "已进入人工客服队列，请稍候。")
        session.flush()
        return _conversation_from_model(conversation)


def list_staff_queue(staff_id: str) -> list[dict[str, Any]]:
    """员工仅看待接入会话、自己正在处理和已结束的会话。"""
    with get_session_factory()() as session:
        rows = session.scalars(select(Conversation)
            .options(selectinload(Conversation.assigned_staff), selectinload(Conversation.user))
            .where((Conversation.status == "waiting") | ((Conversation.status.in_(["staff", "closed"])) &
                   (Conversation.assigned_staff_id == staff_id)))
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())).all()
        result = []
        for row in rows:
            item = _conversation_from_model(row)
            item["userName"] = row.user.name
            result.append(item)
        return result


def list_staff_messages(conversation_id: str, staff_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """待接入会话供员工查看，已接管和已结束记录仅供原接管员工查看。"""
    with get_session_factory().begin() as session:
        row = session.scalar(select(Conversation).where(
            Conversation.id == conversation_id,
            (Conversation.status == "waiting") | (Conversation.assigned_staff_id == staff_id)).with_for_update())
        if row is None:
            return False, []
        messages = session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                                   .order_by(Message.created_at, Message.id)).all()
        return True, [_message_from_model(item) for item in messages]


def accept_conversation(conversation_id: str, staff_id: str) -> dict[str, Any]:
    """锁住会话并只从 waiting 接单；并发员工仅首位能提交。"""
    with get_session_factory().begin() as session:
        row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
        if row is None:
            raise LookupError("会话不存在")
        if row.status != "waiting":
            raise ValueError("会话已由其他客服接入或不在等待队列")
        staff = session.get(User, staff_id)
        if staff is None or staff.role != "staff":
            raise LookupError("客服账号不存在")
        row.status = "staff"
        row.assigned_staff_id = staff_id
        _system_message(session, row, f"{staff.name}已接入会话。")
        session.flush()
        result = _conversation_from_model(row)
        result["assignedStaffName"] = staff.name
        return result


def send_human_message(conversation_id: str, actor_id: str, role: str, content: str) -> dict[str, Any]:
    """用户可在 waiting/staff 留言；员工只能在自己接管的 staff 会话回复。"""
    if not content:
        raise ValueError("消息内容不能为空")
    with get_session_factory().begin() as session:
        row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
        if row is None or (role == "user" and row.user_id != actor_id) or (
            role == "staff" and row.assigned_staff_id != actor_id):
            raise LookupError("会话不存在")
        if (role == "user" and row.status not in {"waiting", "staff"}) or (
            role == "staff" and row.status != "staff"):
            raise ValueError("此会话当前不能发送消息")
        now = utc_now_naive()
        message = Message(conversation_id=conversation_id, sender_role=role, content=content,
                          status="complete", created_at=now)
        session.add(message)
        row.updated_at = now
        row.last_message_preview = content[:120]
        session.flush()
        return _message_from_model(message)


def close_conversation(conversation_id: str, staff_id: str) -> dict[str, Any]:
    """只有接管员工可结束服务，结束后历史仍按原归属可读。"""
    with get_session_factory().begin() as session:
        row = session.scalar(select(Conversation).options(selectinload(Conversation.assigned_staff))
                             .where(Conversation.id == conversation_id).with_for_update())
        if row is None or row.assigned_staff_id != staff_id:
            raise LookupError("会话不存在")
        if row.status != "staff":
            raise ValueError("此会话当前不能结束服务")
        row.status = "closed"
        row.ended_at = utc_now_naive()
        _system_message(session, row, "本次人工服务已结束，双方仍可查看历史消息。")
        session.flush()
        return _conversation_from_model(row)
