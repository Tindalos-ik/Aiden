"""关系数据读取查询，统一将用户归属和结果条数限制写入 SQL。

订单与物流查询把所有者 ID 和资源标识一同交给数据库过滤；客服工具只接收这些
查询返回的 ORM 数据，不会自行拼接 SQL 或在调用方过滤其他用户的记录。

FAQ 的在线检索不在这里：它走 Milvus 的向量召回，原文由 knowledge_chunks 提供，
见 `app.persistence.mysql.knowledge` 与 `app.services.rag.retrieval`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, load_only, selectinload

from .models import AfterSaleRequest, Conversation, Message, Order, Policy, Shipment, TrackingEvent

MAX_QUERY_LIMIT = 500


class AfterSaleSummary(NamedTuple):
    """只保留客服回答售后进度所需的业务字段，不带用户或数据库主键。"""

    request_no: str
    order_no: str
    request_type: str
    status: str
    reason: str
    requested_amount: Decimal | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


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


def list_user_orders(
    session: Session,
    user_id: str,
    *,
    limit: int = 100,
    order_no: str | None = None,
) -> list[Order]:
    """读取指定用户的订单及其成交快照。

    user_id 是必需参数，过滤直接放在 SQL WHERE 中，不能只在调用方查询后再过滤。
    selectinload 将订单行作为第二条批量查询加载，避免逐订单访问 items 时产生 N+1 查询。
    结果按下单时间倒序，时间相同时用 UUID 保证稳定顺序。
    """
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.ordered_at.desc(), Order.id.desc())
        .limit(_checked_limit(limit))
    )
    if order_no is not None:
        stmt = stmt.where(Order.order_no == order_no)
    stmt = stmt.options(selectinload(Order.items))
    return list(session.scalars(stmt))


def list_user_after_sale_requests(
    session: Session,
    user_id: str,
    *,
    request_no: str | None = None,
    order_no: str | None = None,
) -> list[AfterSaleSummary]:
    """精确查本人申请号或订单号；无编号时按申请时间返回最近至多五条。

    两个编号同时传入时同时匹配。申请及关联订单都在 SQL 中限制归属，
    不存在的编号与其他用户的编号得到相同的空结果。
    """
    stmt = (
        select(
            AfterSaleRequest.request_no,
            Order.order_no,
            AfterSaleRequest.request_type,
            AfterSaleRequest.status,
            AfterSaleRequest.reason,
            AfterSaleRequest.requested_amount,
            AfterSaleRequest.created_at,
            AfterSaleRequest.updated_at,
            AfterSaleRequest.resolved_at,
        )
        .join(Order, AfterSaleRequest.order_id == Order.id)
        .where(AfterSaleRequest.user_id == user_id, Order.user_id == user_id)
        .order_by(AfterSaleRequest.created_at.desc(), AfterSaleRequest.request_no.desc())
        .limit(5)
    )
    if request_no is not None:
        stmt = stmt.where(AfterSaleRequest.request_no == request_no)
    if order_no is not None:
        stmt = stmt.where(Order.order_no == order_no)
    return [AfterSaleSummary(*row) for row in session.execute(stmt)]


def get_owned_order_with_shipments(
    session: Session, user_id: str, order_no: str
) -> Order | None:
    """读取当前用户指定订单的包裹和物流节点。

    订单号先与 user_id 一同匹配，包裹和轨迹再沿该订单的 ORM 关系预载；不存在或
    属于其他用户的订单都返回 None。仅加载工具回答物流问题需要的字段。
    """
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.order_no == order_no)
        .options(
            load_only(Order.id, Order.order_no),
            selectinload(Order.shipments).load_only(
                Shipment.order_id,
                Shipment.id,
                Shipment.shipment_no,
                Shipment.carrier_name,
                Shipment.tracking_no,
                Shipment.status,
                Shipment.shipped_at,
                Shipment.delivered_at,
            ),
            selectinload(Order.shipments)
            .selectinload(Shipment.tracking_events)
            .load_only(
                TrackingEvent.shipment_id,
                TrackingEvent.status,
                TrackingEvent.description,
                TrackingEvent.location,
                TrackingEvent.occurred_at,
            ),
        )
    )
    return session.scalar(stmt)


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


def list_matching_effective_policies(
    session: Session, topics: tuple[str, ...], *, at: datetime | None = None, limit: int = 5
) -> list[Policy]:
    """按政策主题读取当前有效版本，匹配文本、时间和条数都在 SQL 中限定。

    ``contains(autoescape=True)`` 会绑定并转义用户问题识别出的主题词，避免把
    ``%``、``_`` 当作 LIKE 通配符。日期沿用本项目的 UTC DATETIME 约定。
    """
    if not topics:
        return []

    instant = _utc_naive(at)
    topic_filters = [
        or_(
            Policy.policy_key.contains(topic, autoescape=True),
            Policy.name.contains(topic, autoescape=True),
            Policy.content.contains(topic, autoescape=True),
        )
        for topic in topics
    ]
    stmt = (
        select(Policy)
        .where(
            Policy.is_active.is_(True),
            Policy.effective_from <= instant,
            or_(Policy.effective_until.is_(None), Policy.effective_until > instant),
            or_(*topic_filters),
        )
        .order_by(Policy.effective_from.desc(), Policy.created_at.desc(), Policy.version.desc())
        .limit(_checked_limit(limit))
    )
    return list(session.scalars(stmt))


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
