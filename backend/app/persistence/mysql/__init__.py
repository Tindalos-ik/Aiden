"""MySQL 数据层的公共入口。

服务层可以从这里导入常用 Session 管理器、ORM 实体和查询方法；
模块导入只加载 Python 定义，不会读取数据库 URL、连接 MySQL 或自动建表。
"""

from .database import get_engine, get_session, get_session_factory, session_scope
from .models import (
    AfterSaleRequest,
    Base,
    Conversation,
    FAQ,
    LoginSession,
    Message,
    Order,
    OrderItem,
    Policy,
    Product,
    ProductSKU,
    Shipment,
    Ticket,
    TrackingEvent,
    UnansweredQuestion,
    User,
)
from .queries import (
    get_effective_policy,
    get_owned_order_with_shipments,
    list_conversation_messages,
    list_order_tracking_events,
    list_user_orders,
    search_active_faq,
)

__all__ = [
    # ORM 实体：供服务层创建和读取关系型数据。
    "AfterSaleRequest",
    "Base",
    "Conversation",
    "FAQ",
    "LoginSession",
    "Message",
    "Order",
    "OrderItem",
    "Policy",
    "Product",
    "ProductSKU",
    "Shipment",
    "Ticket",
    "TrackingEvent",
    "UnansweredQuestion",
    "User",
    # 便捷导出：查询函数需传入 Session，不会隐式打开事务；连接工具按需读取配置。
    "get_effective_policy",
    "get_owned_order_with_shipments",
    "get_engine",
    "get_session",
    "get_session_factory",
    "list_conversation_messages",
    "list_order_tracking_events",
    "list_user_orders",
    "search_active_faq",
    "session_scope",
]
