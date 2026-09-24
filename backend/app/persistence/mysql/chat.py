"""MySQL 鉴权与对话数据访问层。

每个函数都自行创建短生命周期的 SQLAlchemy Session，并在返回前关闭它。尤其是
Agent 的流式模型调用只会拿到这里返回的普通 Python 数据，不会持有同步 Session
跨越任何模型 await。写操作通过单个事务提交，出错时由上下文管理器自动回滚。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.config.settings import settings

from .database import get_session_factory
from .models import Conversation, LoginSession, Message, User, utc_now_naive

MessageStatus = Literal["complete", "streaming", "error", "stopped"]


def _utc_naive(value: datetime) -> datetime:
    """将有时区时间先归一到 UTC，再去掉时区以适配 MySQL DATETIME。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_utc(value: datetime | None) -> str | None:
    """在 API 边界把 MySQL 的 UTC DATETIME 序列化为含 Z 的 ISO 8601 字符串。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _actor_from_user(user: User) -> dict[str, Any]:
    """映射为前端 Actor 字段；不把密码盐、密码哈希或内部关系返回给客户端。"""
    return {"id": user.id, "name": user.name, "role": user.role, "email": user.email}


def _conversation_from_model(conversation: Conversation) -> dict[str, Any]:
    """统一映射会话响应字段，并把 ORM 下划线列名转换成前端驼峰字段名。"""
    staff = conversation.assigned_staff
    return {
        "id": conversation.id,
        "userId": conversation.user_id,
        "subject": conversation.subject,
        "status": conversation.status,
        "createdAt": _iso_utc(conversation.created_at),
        "updatedAt": _iso_utc(conversation.updated_at),
        "assignedStaffId": conversation.assigned_staff_id,
        "assignedStaffName": staff.name if staff else None,
        "lastMessagePreview": conversation.last_message_preview,
    }


def _message_from_model(message: Message) -> dict[str, Any]:
    """将 MySQL sender_role 转成 remote.ts 使用的 role，时间也在此处统一格式化。"""
    return {
        "id": message.id,
        "conversationId": message.conversation_id,
        "role": message.sender_role,
        "content": message.content,
        "createdAt": _iso_utc(message.created_at),
        "status": message.status,
    }


def _password_hash(password: str, salt: str) -> str:
    """计算与本地演示数据库兼容的 PBKDF2-SHA256 密码摘要。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000
    ).hex()


def verify_user(account: str, password: str, role: str) -> dict[str, Any] | None:
    """按邮箱和角色取出用户凭据，关闭数据库连接后再执行 PBKDF2 校验。"""
    with get_session_factory()() as session:
        user = session.scalar(
            select(User).where(func.lower(User.email) == account.strip().lower(), User.role == role)
        )
        if user is None or not user.password_salt or not user.password_hash:
            return None
        user_data = (user.id, user.name, user.role, user.email, user.password_salt, user.password_hash)

    # PBKDF2 有意放在 Session 关闭之后，避免密码校验期间占用数据库连接。
    user_id, name, user_role, email, salt, expected = user_data
    try:
        actual = _password_hash(password, salt)
    except ValueError:
        return None
    if not secrets.compare_digest(actual, expected):
        return None
    return {"id": user_id, "name": name, "role": user_role, "email": email}


def create_session(user_id: str) -> tuple[str, str]:
    """生成一次性 Cookie 令牌，只把其 SHA-256 哈希和过期时间写入 MySQL。"""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
    record = LoginSession(
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        user_id=user_id,
        created_at=_utc_naive(now),
        expires_at=_utc_naive(expires_at),
    )
    with get_session_factory().begin() as session:
        session.execute(delete(LoginSession).where(LoginSession.expires_at <= _utc_naive(now)))
        session.add(record)
    return token, _iso_utc(expires_at) or ""


def get_user_for_session(token: str | None) -> dict[str, Any] | None:
    """校验 Cookie 哈希和过期时间，并只返回前端所需的当前用户字段。"""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = utc_now_naive()
    with get_session_factory()() as session:
        user = session.scalar(
            select(User)
            .join(LoginSession, LoginSession.user_id == User.id)
            .where(LoginSession.token_hash == token_hash, LoginSession.expires_at > now)
        )
        return _actor_from_user(user) if user else None


def delete_session(token: str | None) -> None:
    """注销时按令牌哈希删除记录，数据库中从不出现 Cookie 明文。"""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_session_factory().begin() as session:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash))


def list_user_conversations(user_id: str) -> list[dict[str, Any]]:
    """按当前用户筛选会话，并预加载客服关系以填充前端契约字段。"""
    with get_session_factory()() as session:
        conversations = session.scalars(
            select(Conversation)
            .options(selectinload(Conversation.assigned_staff))
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        ).all()
        return [_conversation_from_model(item) for item in conversations]


def create_conversation(user_id: str) -> dict[str, Any]:
    """创建属于当前用户的 Bot 会话，并同步返回前端使用的驼峰字段。"""
    now = utc_now_naive()
    conversation = Conversation(
        id=str(uuid4()),
        user_id=user_id,
        subject="新建会话",
        status="bot",
        created_at=now,
        updated_at=now,
        started_at=now,
        last_message_preview="",
    )
    with get_session_factory().begin() as session:
        session.add(conversation)
        session.flush()
        result = _conversation_from_model(conversation)
    return result


def get_owned_conversation(conversation_id: str, user_id: str) -> dict[str, Any] | None:
    """单条会话查询同时约束会话 ID 和所有者 ID，避免越权探测。"""
    with get_session_factory()() as session:
        conversation = session.scalar(
            select(Conversation)
            .options(selectinload(Conversation.assigned_staff))
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        )
        return _conversation_from_model(conversation) if conversation else None


def list_owned_messages(
    conversation_id: str, user_id: str
) -> tuple[bool, list[dict[str, Any]]]:
    """在一个 Session 中先验证归属，再返回消息；不存在或不属于用户都不泄露内容。"""
    with get_session_factory()() as session:
        conversation_exists = session.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
        if conversation_exists is None:
            return False, []
        messages = session.scalars(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        ).all()
        return True, [_message_from_model(item) for item in messages]


@dataclass(frozen=True)
class MessagePair:
    """原子创建/重试后的结果；完整重试用 replay_content 重放已有回答。"""
    user_message_id: str
    assistant_message_id: str
    should_generate: bool = True
    replay_content: str = ""


def create_message_pair(
    conversation_id: str,
    user_id: str,
    content: str,
    client_message_id: str | None,
) -> MessagePair:
    """原子保存用户消息和流式助手行，并实现 clientMessageId 幂等重试。

    对会话行加锁，使同一会话并发发送时先后串行；首次请求在同一事务中插入用户
    消息和助手 streaming 行。助手行复用相同 client_message_id，以便重试查回原回答。
    已完成请求只重放原答案；失败或断流记录可重新生成，但不会再插入用户消息。
    """
    now = utc_now_naive()
    with get_session_factory().begin() as session:
        conversation = session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .with_for_update()
        )
        if conversation is None:
            raise LookupError("会话不存在")
        if conversation.status != "bot":
            raise ValueError("此会话当前不能发送智能客服消息")

        if client_message_id:
            # 同一个客户端键必须对应同一段内容，否则拒绝覆盖已有用户消息。
            existing_user = session.scalar(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.sender_role == "user",
                    Message.client_message_id == client_message_id,
                )
            )
            if existing_user:
                if existing_user.content != content:
                    raise ValueError("clientMessageId 已用于另一条消息，请刷新会话后重试")
                assistant = session.scalar(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.sender_role == "assistant",
                        Message.client_message_id == client_message_id,
                    )
                )
                if assistant is None:
                    raise RuntimeError("已提交的消息没有对应的回答记录")
                if assistant.status == "complete":
                    return MessagePair(
                        existing_user.id, assistant.id, should_generate=False,
                        replay_content=assistant.content,
                    )
                if assistant.status == "streaming":
                    raise ValueError("这条消息仍在生成回答，请稍后刷新会话")
                other_stream = session.scalar(
                    select(Message.id).where(
                        Message.conversation_id == conversation_id,
                        Message.sender_role == "assistant",
                        Message.status == "streaming",
                        Message.id != assistant.id,
                    ).limit(1)
                )
                if other_stream:
                    raise ValueError("当前会话正在生成回答，请稍后再发送")
                assistant.content = ""
                assistant.status = "streaming"
                assistant.created_at = now + timedelta(microseconds=1)
                conversation.updated_at = assistant.created_at
                conversation.last_message_preview = content[:120]
                return MessagePair(existing_user.id, assistant.id)

        active_stream = session.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == conversation_id,
                Message.sender_role == "assistant",
                Message.status == "streaming",
            )
            .limit(1)
        )
        if active_stream:
            raise ValueError("当前会话正在生成回答，请稍后再发送")

        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        # 前后相差 1 微秒，避免 UUID 排序导致同一时刻助手行排在用户行前面。
        user_message = Message(
            id=user_message_id,
            conversation_id=conversation_id,
            sender_role="user",
            content=content,
            client_message_id=client_message_id,
            status="complete",
            created_at=now,
        )
        assistant_message = Message(
            id=assistant_message_id,
            conversation_id=conversation_id,
            sender_role="assistant",
            content="",
            client_message_id=client_message_id,
            status="streaming",
            created_at=now + timedelta(microseconds=1),
        )
        session.add_all([user_message, assistant_message])
        conversation.subject = content[:200] if conversation.subject == "新建会话" else conversation.subject
        conversation.updated_at = assistant_message.created_at
        conversation.last_message_preview = content[:120]
        session.flush()
        return MessagePair(user_message_id, assistant_message_id)


def recent_messages(
    conversation_id: str, user_id: str, excluded_message_id: str
) -> list[dict[str, str]]:
    """按归属加载最近上下文，关闭 Session 后再交给 LangGraph/模型调用。

    同时限制历史条数、单条消息长度和总字符数；本轮未完成的助手占位行会被排除。
    """
    with get_session_factory()() as session:
        rows = session.execute(
            select(Message.sender_role, Message.content)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Message.id != excluded_message_id,
                Message.status != "streaming",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(settings.max_history_messages)
        ).all()

    selected: list[dict[str, str]] = []
    remaining_chars = settings.max_context_chars
    for role, content in rows:
        if remaining_chars <= 0:
            break
        text = content[: min(settings.max_message_chars, remaining_chars)]
        if text:
            selected.append({"role": role, "content": text})
            remaining_chars -= len(text)
    return list(reversed(selected))


def finish_assistant_message(
    message_id: str,
    conversation_id: str,
    user_id: str,
    content: str,
    status: MessageStatus,
) -> None:
    """仅把仍在生成的助手行从 streaming 转为终态，并校验会话归属。"""
    now = utc_now_naive()
    with get_session_factory().begin() as session:
        message = session.scalar(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.sender_role == "assistant",
                Conversation.user_id == user_id,
            )
            .with_for_update()
        )
        if message is None:
            raise LookupError("会话不存在")
        # Graph 保存完成答案后，SSE 取消收尾可能稍后尝试写 stopped；终态只能由
        # create_message_pair 的显式重试重置，收尾写入不能覆盖已经落库的结果。
        if message.status != "streaming":
            return
        message.content = content
        message.status = status
        conversation = session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise LookupError("会话不存在")
        conversation.updated_at = now
        conversation.last_message_preview = content[:120]
