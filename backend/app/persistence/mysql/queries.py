"""关系数据读取查询，统一将用户归属和结果条数限制写入 SQL。

会话、订单及物流查询不会信任调用方已经做过的权限检查，而是让所有者 ID 与资源 ID
一同进入数据库过滤条件。订单、政策等查询供后续数据接入使用；当前客服 Agent 并未
调用这些查询，也不会据此声称已经查询订单或政策。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from .models import Conversation, Message, Order, Policy, Shipment, TrackingEvent

MAX_QUERY_LIMIT = 500


def _checked_limit(limit: int) -> int:
    """统一校验读取条数，避免调用方误传超大值造成无界列表查询。"""
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    return limit


def _utc_naive(value: datetime | None) -> datetime:
    """将带时区输入转换为 UTC naive；未传时间时取当前 UTC 时刻。

    ORM 的 MySQL DATETIME 列不存储时区，因此比较双方都必须使用同一 UTC 约定。
    """
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def list_user_orders(session: Session, user_id: str, *, limit: int = 100) -> list[Order]:
    """读取指定用户的订单及其成交快照。

    user_id 是必需参数，过滤直接放在 SQL WHERE 中，不能只在调用方查询后再过滤。
    selectinload 将订单行作为第二条批量查询加载，避免逐订单访问 items 时产生 N+1 查询。
    结果按下单时间倒序，时间相同时用 UUID 保证稳定顺序。
    """
    stmt = (
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.user_id == user_id)
        .order_by(Order.ordered_at.desc(), Order.id.desc())
        .limit(_checked_limit(limit))
    )
    return list(session.scalars(stmt))


def list_order_tracking_events(
    session: Session, user_id: str, order_id: str
) -> list[TrackingEvent]:
    """读取属于指定用户的订单物流节点。

    查询路径为轨迹节点 -> 包裹 -> 订单；同时限制订单 ID 和订单所有者 ID。
    即使调用方传入别人的 order_id，也只会得到空列表。joinedload 预载包裹信息，
    以便调用方读取运单号和承运商时不再逐节点查询数据库。
    """
    stmt = (
        select(TrackingEvent)
        # 通过 ORM 外键关系依次连接包裹和订单，确保 user_id 条件落在真实订单记录上。
        .join(TrackingEvent.shipment)
        .join(Shipment.order)
        # 所有权约束必须与订单 ID 一起进入 SQL，而不是依赖上层先做一次检查。
        .where(Order.user_id == user_id, Order.id == order_id)
        .options(joinedload(TrackingEvent.shipment))
        .order_by(TrackingEvent.occurred_at.asc(), TrackingEvent.id.asc())
    )
    return list(session.scalars(stmt))


def get_effective_policy(
    session: Session, policy_key: str, *, at: datetime | None = None
) -> Policy | None:
    """读取在指定时刻生效的最新政策版本。

    生效区间采用左闭右开定义：起始时刻包含在内，结束时刻不包含在内；
    is_active=False 的历史版本即使日期匹配也不会返回。重叠版本时优先取最近生效者。
    """
    instant = _utc_naive(at)
    stmt = (
        select(Policy)
        .where(
            Policy.policy_key == policy_key,
            Policy.is_active.is_(True),
            Policy.effective_from <= instant,
            or_(Policy.effective_until.is_(None), Policy.effective_until > instant),
        )
        .order_by(Policy.effective_from.desc(), Policy.created_at.desc(), Policy.version.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def list_conversation_messages(
    session: Session, user_id: str, conversation_id: str, *, limit: int = 200
) -> list[Message]:
    """读取指定用户会话中的消息。

    必须同时匹配 conversation_id 和会话所有者 user_id；会话不存在或不属于该用户时
    都返回空列表。升序结果适合直接构造对话历史，limit 防止一次加载无限消息。
    """
    stmt = (
        select(Message)
        .join(Message.conversation)
        # 通过会话外键过滤所有者，从 SQL 层阻断跨用户读取。
        .where(Conversation.user_id == user_id, Conversation.id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(_checked_limit(limit))
    )
    return list(session.scalars(stmt))
