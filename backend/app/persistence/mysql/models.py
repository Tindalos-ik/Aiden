"""Aiden 关系型数据的 SQLAlchemy 2.x ORM 模型。

字段和约束与 Alembic 管理的 MySQL 表结构对应。修改模型不会自动改动数据库，
结构变化必须通过新增迁移完成；所有 MySQL DATETIME 按 UTC 约定存储。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    """生成应用侧 UUID 字符串，避免依赖数据库自增序列或数据库往返。"""
    return str(uuid4())


def utc_now_naive() -> datetime:
    """以 UTC 取当前时间，并去掉时区标记以适配 MySQL DATETIME。

    MySQL DATETIME 本身不存时区；本项目约定写入值始终表示 UTC。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有 ORM 实体共用的声明式基类和约束命名规则。

    统一命名能让 Alembic 生成稳定的迁移名称，也便于后续定位数据库报错。
    """

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class UUIDPrimaryKey:
    """需要 UUID 主键的表可复用此 mixin。

    主键在 INSERT 时由 Python 生成，格式为标准的 36 字符 UUID 字符串。
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    """为常规业务表提供创建时间和更新时间。

    Python 默认值与数据库默认值同时设置，使 ORM 插入和直接 SQL 插入都有创建时间。
    updated_at 的 onupdate 由 SQLAlchemy 在 ORM UPDATE 时刷新；时间统一按 UTC 表示。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utc_now_naive,
        onupdate=utc_now_naive,
        server_default=func.current_timestamp(),
    )


class User(UUIDPrimaryKey, TimestampMixin, Base):
    """用户和客服账号。

    email 必填且唯一，phone 可空但非空值唯一；role 由检查约束限制为 user/staff。
    password_salt 和 password_hash 为可空字段，以支持由外部身份系统校验凭据的账号；
    无论哪种登录方式，都不应在这些列中保存明文密码。
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'staff')", name="role_valid"),
        UniqueConstraint("email"),
        UniqueConstraint("phone"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default=text("'user'"))
    password_salt: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str | None] = mapped_column(String(255))

    orders: Mapped[list[Order]] = relationship(back_populates="user", foreign_keys="Order.user_id")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", foreign_keys="Conversation.user_id"
    )
    assigned_conversations: Mapped[list[Conversation]] = relationship(
        back_populates="assigned_staff", foreign_keys="Conversation.assigned_staff_id"
    )


class LoginSession(Base):
    """浏览器登录会话。

    主键保存令牌的哈希而不是可直接使用的 bearer token；过期时间索引支持清理任务
    快速定位失效会话。用户删除时级联删除其登录会话。
    """

    __tablename__ = "login_sessions"
    __table_args__ = (Index("ix_login_sessions_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )


class Product(UUIDPrimaryKey, TimestampMixin, Base):
    """商品目录主记录。

    name 是面向用户的商品名称；category 和 description 用于分类展示与检索。
    下架通过 is_active 标记，避免删除仍被历史订单引用的商品。
    """

    __tablename__ = "products"
    __table_args__ = (Index("ix_products_category_active", "category", "is_active"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )

    skus: Mapped[list[ProductSKU]] = relationship(back_populates="product")


class ProductSKU(UUIDPrimaryKey, TimestampMixin, Base):
    """商品的可售规格。

    sku_code 对外唯一；specification 用 JSON 保存颜色、容量等可变规格属性。
    price 和 stock_quantity 表示当前目录状态，历史订单使用订单行快照。
    """

    __tablename__ = "product_skus"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_nonnegative"),
        CheckConstraint("stock_quantity >= 0", name="stock_nonnegative"),
        Index("ix_product_skus_product_active", "product_id", "is_active"),
    )

    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    sku_code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    specification: Mapped[dict | None] = mapped_column(JSON)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )

    product: Mapped[Product] = relationship(back_populates="skus")


class Order(UUIDPrimaryKey, TimestampMixin, Base):
    """用户订单主记录。

    用户外键限制删除仍有关联订单的账号；(id, user_id) 复合唯一键供售后申请
    校验订单归属。订单号单独唯一；total_amount 使用 Decimal 对应的 DECIMAL 列，
    currency 默认 CNY。ordered_at 表示业务下单时间，status 保留可扩展的订单状态字符串。
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="total_amount_nonnegative"),
        UniqueConstraint("id", "user_id"),
        UniqueConstraint("order_no"),
        Index("ix_orders_user_ordered", "user_id", "ordered_at"),
    )

    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY", server_default=text("'CNY'"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )

    user: Mapped[User] = relationship(back_populates="orders", foreign_keys=[user_id])
    items: Mapped[list[OrderItem]] = relationship(back_populates="order")
    shipments: Mapped[list[Shipment]] = relationship(back_populates="order")


class OrderItem(UUIDPrimaryKey, Base):
    """订单中的商品行，价格和商品信息均为下单时的不可变快照。

    sku_id 仅用于回查当前目录；即使 SKU 被删除而外键置空，快照仍可还原成交内容。
    unit_price 是单件成交价，line_total 是该行成交金额，quantity 必须为正数；
    specification_snapshot 复制下单时的规格 JSON，避免商品目录更新改写订单历史。
    """

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_nonnegative"),
        CheckConstraint("line_total >= 0", name="line_total_nonnegative"),
        UniqueConstraint("id", "order_id"),
        ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["sku_id"], ["product_skus.id"], ondelete="SET NULL"),
        Index("ix_order_items_order", "order_id"),
        Index("ix_order_items_sku", "sku_id"),
    )

    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sku_id: Mapped[str | None] = mapped_column(String(36))
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    sku_code_snapshot: Mapped[str | None] = mapped_column(String(80))
    sku_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    specification_snapshot: Mapped[dict | None] = mapped_column(JSON)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    sku: Mapped[ProductSKU | None] = relationship()


class Shipment(UUIDPrimaryKey, TimestampMixin, Base):
    """一个订单的包裹记录；一个订单允许拆成多个包裹发货。

    shipment_no 是系统内部包裹号；carrier_code 与 tracking_no 组合唯一，
    shipped_at 和 delivered_at 分别记录发出与签收时刻。
    """

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("carrier_code", "tracking_no"),
        Index("ix_shipments_order", "order_id"),
    )

    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    shipment_no: Mapped[str | None] = mapped_column(String(64), unique=True)
    carrier_code: Mapped[str | None] = mapped_column(String(40))
    carrier_name: Mapped[str | None] = mapped_column(String(100))
    tracking_no: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)

    order: Mapped[Order] = relationship(back_populates="shipments")
    tracking_events: Mapped[list[TrackingEvent]] = relationship(back_populates="shipment")


class TrackingEvent(UUIDPrimaryKey, Base):
    """承运商轨迹节点。

    status 保存标准化节点状态，description/location 保留承运商提供的说明和地点；
    occurred_at 是物流事件实际发生时间，created_at 是本系统接收该事件的时间。
    """

    __tablename__ = "tracking_events"
    __table_args__ = (Index("ix_tracking_events_shipment_occurred", "shipment_id", "occurred_at"),)

    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    event_code: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )

    shipment: Mapped[Shipment] = relationship(back_populates="tracking_events")


class AfterSaleRequest(UUIDPrimaryKey, TimestampMixin, Base):
    """退货、退款或换货申请。

    两条复合外键分别保证申请用户拥有该订单、可选订单行属于该订单，避免关联错单。
    request_type 区分退货、退款和换货；reason 记录申请原因，requested_amount 为
    可选的申请金额，status 与 resolved_at 记录处理进度和完成时间。
    """

    __tablename__ = "after_sale_requests"
    __table_args__ = (
        CheckConstraint("request_type IN ('return', 'refund', 'exchange')", name="request_type_valid"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'processing', 'completed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "requested_amount IS NULL OR requested_amount >= 0",
            name="requested_amount_nonnegative",
        ),
        ForeignKeyConstraint(
            ["order_id", "user_id"], ["orders.id", "orders.user_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["order_item_id", "order_id"],
            ["order_items.id", "order_items.order_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_after_sale_requests_user_created", "user_id", "created_at"),
        Index("ix_after_sale_requests_order", "order_id"),
    )

    request_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_item_id: Mapped[str | None] = mapped_column(String(36))
    request_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default=text("'pending'"))
    requested_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Policy(UUIDPrimaryKey, TimestampMixin, Base):
    """带版本和生效区间的客服政策。

    一个 policy_key 可保留多个版本；查询按 [effective_from, effective_until) 判断
    生效，effective_until 为空表示没有结束时间。policy_key 标识政策主题，
    version 标识该主题下的版本；content 保存可供客服检索的正式政策正文。
    """

    __tablename__ = "policies"
    __table_args__ = (
        CheckConstraint(
            "effective_until IS NULL OR effective_until > effective_from",
            name="effective_interval_valid",
        ),
        UniqueConstraint("policy_key", "version"),
        Index("ix_policies_key_effective", "policy_key", "is_active", "effective_from", "effective_until"),
    )

    policy_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )


class FAQ(UUIDPrimaryKey, TimestampMixin, Base):
    """人工维护的常见问题条目。

    category 可为空，question 和 answer 保存问答正文；停用时通过 is_active 保留记录，
    便于审核追溯而不影响当前检索。
    """

    __tablename__ = "faq"
    __table_args__ = (Index("ix_faq_category_active", "category", "is_active"),)

    category: Mapped[str | None] = mapped_column(String(100))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )


class Conversation(UUIDPrimaryKey, TimestampMixin, Base):
    """用户与客服的会话主记录。

    user_id 表示会话归属，assigned_staff_id 表示可选的接管客服；
    (id, user_id) 复合唯一键供工单外键校验会话所有者。subject 和 status 用于会话列表，
    last_message_preview 是列表展示用的短预览，不替代 messages 中的完整消息。
    """

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('bot', 'waiting', 'staff', 'closed')", name="status_valid"),
        UniqueConstraint("id", "user_id"),
        Index("ix_conversations_user_updated", "user_id", "updated_at"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(200), nullable=False, default="新建会话", server_default=text("'新建会话'"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="bot", server_default=text("'bot'"))
    assigned_staff_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_preview: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default=text("''")
    )

    user: Mapped[User] = relationship(back_populates="conversations", foreign_keys=[user_id])
    assigned_staff: Mapped[User | None] = relationship(
        back_populates="assigned_conversations", foreign_keys=[assigned_staff_id]
    )
    messages: Mapped[list[Message]] = relationship(back_populates="conversation")


class Message(UUIDPrimaryKey, Base):
    """会话消息流水。

    client_message_id 用于识别客户端重试，同一会话和发送者下不可重复；
    按会话、微秒时间和主键建立索引，避免同秒内用户消息与助手回复顺序不定。sender_role 区分用户、
    助手、客服和系统消息；status 表示流式生成状态，client_message_id 可选且用于幂等重试。
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("sender_role IN ('user', 'assistant', 'staff', 'system')", name="sender_role_valid"),
        CheckConstraint(
            "status IN ('complete', 'streaming', 'error', 'stopped')", name="status_valid"
        ),
        UniqueConstraint("conversation_id", "sender_role", "client_message_id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at", "id"),
    )

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_message_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="complete", server_default=text("'complete'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, server_default=func.current_timestamp(6)
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Ticket(UUIDPrimaryKey, TimestampMixin, Base):
    """由会话转人工创建的工单。

    conversation_id 与 user_id 组成复合外键，数据库会验证工单归属与会话归属一致。
    ticket_no 对外唯一；issue_type 与 description 描述问题，assigned_staff_id 可选，
    resolved_at 记录解决时刻。
    """

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'in_progress', 'resolved', 'closed')", name="status_valid"),
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_tickets_user_status_created", "user_id", "status", "created_at"),
    )

    ticket_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", server_default=text("'open'"))
    assigned_staff_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class UnansweredQuestion(UUIDPrimaryKey, Base):
    """等待人工审核的低置信度或未解决问题。

    confidence 使用 0 到 1 的 Decimal 分值；review_status 表示待审、已审、已补入 FAQ
    或忽略，reviewer_id、review_note 和 reviewed_at 记录人工审核信息。
    """

    __tablename__ = "unanswered_questions"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        CheckConstraint(
            "review_status IN ('pending', 'reviewed', 'added_to_faq', 'dismissed')",
            name="review_status_valid",
        ),
        Index("ix_unanswered_questions_status_created", "review_status", "created_at"),
        Index("ix_unanswered_questions_conversation", "conversation_id"),
    )

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default=text("'pending'")
    )
    reviewer_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now_naive, server_default=func.current_timestamp()
    )
