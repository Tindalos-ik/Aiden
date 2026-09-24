"""面向客服 Agent 的只读工具。

每个工具从 LangGraph 已认证状态注入 user_id，模型参数中不包含身份字段；数据库
Session 只在同步查询期间打开，返回给模型的内容是经过筛选的普通 Python 数据。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.queries import (
    get_owned_order_with_shipments,
    list_user_orders,
    search_active_faq,
)

_TOOL_ERROR = "查询暂时无法完成，请稍后重试。"
_ORDER_LIMIT = 5
_FAQ_LIMIT = 5


def _json_result(data: dict[str, Any]) -> str:
    """以稳定的 UTF-8 JSON 交给模型，避免把 ORM 对象或异常文本泄露到对话中。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _iso_utc(value: datetime | None) -> str | None:
    """将 MySQL 中按 UTC 约定保存的无时区时间标为 UTC。"""
    return f"{value.isoformat()}Z" if value is not None else None


@tool
def query_order(
    user_id: Annotated[str, InjectedState("user_id")],
    order_no: str | None = None,
) -> str:
    """查询当前登录用户的订单和商品；订单号可选，省略时返回最近五笔。"""
    normalized_order_no = (order_no or "").strip() or None
    if normalized_order_no is not None and len(normalized_order_no) > 64:
        return _json_result(
            {"status": "invalid", "found": False, "orders": [], "message": "订单号过长，请检查后重试。"}
        )

    try:
        with get_session_factory()() as session:
            orders = list_user_orders(
                session,
                user_id,
                order_no=normalized_order_no,
                limit=_ORDER_LIMIT,
            )
            result_orders = [
                {
                    "order_no": order.order_no,
                    "status": order.status,
                    "total_amount": str(order.total_amount),
                    "currency": order.currency,
                    "ordered_at": _iso_utc(order.ordered_at),
                    "items": [
                        {
                            "product_name": item.product_name_snapshot,
                            "sku_name": item.sku_name_snapshot,
                            "quantity": item.quantity,
                            "unit_price": str(item.unit_price),
                            "line_total": str(item.line_total),
                        }
                        for item in order.items
                    ],
                }
                for order in orders
            ]
        return _json_result(
            {
                "status": "ok",
                "found": bool(result_orders),
                "orders": result_orders,
                "message": "" if result_orders else "当前账号下没有找到匹配的订单。",
            }
        )
    except Exception:
        return _json_result({"status": "error", "message": _TOOL_ERROR, "orders": []})


@tool
def query_logistics(
    user_id: Annotated[str, InjectedState("user_id")],
    order_no: str,
) -> str:
    """查询当前登录用户指定订单的包裹、承运信息和物流节点。"""
    normalized_order_no = order_no.strip()
    if not normalized_order_no:
        return _json_result(
            {"status": "invalid", "found": False, "shipments": [], "message": "请提供订单号。"}
        )
    if len(normalized_order_no) > 64:
        return _json_result(
            {"status": "invalid", "found": False, "shipments": [], "message": "订单号过长，请检查后重试。"}
        )

    try:
        with get_session_factory()() as session:
            order = get_owned_order_with_shipments(session, user_id, normalized_order_no)
            if order is None:
                return _json_result(
                    {
                        "status": "ok",
                        "found": False,
                        "shipments": [],
                        "message": "当前账号下没有找到该订单。",
                    }
                )

            shipments = []
            for shipment in sorted(
                order.shipments,
                key=lambda item: (item.shipped_at or datetime.min, item.id),
            ):
                events = sorted(
                    shipment.tracking_events,
                    key=lambda item: (item.occurred_at, item.id),
                )
                shipments.append(
                    {
                        "shipment_no": shipment.shipment_no,
                        "carrier_name": shipment.carrier_name,
                        "tracking_no": shipment.tracking_no,
                        "status": shipment.status,
                        "shipped_at": _iso_utc(shipment.shipped_at),
                        "delivered_at": _iso_utc(shipment.delivered_at),
                        "events": [
                            {
                                "status": event.status,
                                "description": event.description,
                                "location": event.location,
                                "occurred_at": _iso_utc(event.occurred_at),
                            }
                            for event in events
                        ],
                    }
                )
        return _json_result(
            {
                "status": "ok",
                "found": True,
                "order_no": order.order_no,
                "shipments": shipments,
                "message": "" if shipments else "该订单目前没有包裹记录。",
            }
        )
    except Exception:
        return _json_result({"status": "error", "message": _TOOL_ERROR, "shipments": []})


@tool
def search_faq(question: str) -> str:
    """根据用户的问题检索启用中的 FAQ，返回匹配的问题、答案和分类。"""
    normalized_question = question.strip()
    if not normalized_question:
        return _json_result(
            {"status": "ok", "results": [], "message": "请提供需要检索的问题。"}
        )
    if len(normalized_question) > 500:
        return _json_result(
            {"status": "invalid", "results": [], "message": "问题过长，请简要描述后重试。"}
        )

    try:
        with get_session_factory()() as session:
            results = search_active_faq(
                session,
                normalized_question,
                limit=_FAQ_LIMIT,
            )
        return _json_result(
            {
                "status": "ok",
                "results": results,
                "message": "" if results else "FAQ 中没有找到匹配条目。",
            }
        )
    except Exception:
        return _json_result({"status": "error", "message": _TOOL_ERROR, "results": []})


READ_ONLY_CUSTOMER_TOOLS = [query_order, query_logistics, search_faq]
