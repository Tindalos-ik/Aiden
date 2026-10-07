import json
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.messages import _content_as_text
from app.agent.prompt import _FALLBACK_REPLY
from app.agent.state import SupportState


_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号、尾号或列表序号选择要查询的订单："

_REFUND_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号、尾号或列表序号选择要办理售后的订单，并说明申请原因："

_LEGACY_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号或序号选择要查询的订单："

_ORDER_LIST_ENTRY_PATTERN = re.compile(r"([1-9]\d*)\. (.+)")

_ORDER_LIST_PRODUCT_LIMIT = 3

def _parse_order_list(content: str) -> list[str] | None:
    """只接受本服务端固定格式的最近订单列表，避免把任意助手文字当成选择授权。"""
    lines = content.splitlines()
    embedded = next((
        index for index, line in enumerate(lines)
        if any(re.fullmatch(r"【[1-4]】\s*" + re.escape(header), line)
               for header in (_ORDER_LIST_HEADER, _REFUND_ORDER_LIST_HEADER))
    ), None)
    if embedded is not None:
        lines = lines[embedded:]
        lines[0] = next(header for header in (_ORDER_LIST_HEADER, _REFUND_ORDER_LIST_HEADER)
                        if header in lines[0])
        lines = lines[:next(
            (index for index, line in enumerate(lines[1:], 1) if re.match(r"【[1-4]】\s*", line)),
            len(lines),
        )]
    if (
        not lines
        or lines[0] not in {_ORDER_LIST_HEADER, _REFUND_ORDER_LIST_HEADER, _LEGACY_ORDER_LIST_HEADER}
        or len(lines) < 2
    ):
        return None

    has_product_details = lines[0] in {_ORDER_LIST_HEADER, _REFUND_ORDER_LIST_HEADER}
    order_nos: list[str] = []
    for position, line in enumerate(lines[1:], start=1):
        match = _ORDER_LIST_ENTRY_PATTERN.fullmatch(line)
        if match is None or int(match.group(1)) != position:
            return None
        entry = match.group(2)
        if has_product_details:
            order_no, separator, _ = entry.partition(" — ")
            if not separator:
                return None
        else:
            order_no = entry
        if (
            not order_no
            or len(order_no) > 64
            or order_no.strip() != order_no
            or any(ord(char) < 32 or char in "\u2028\u2029" for char in order_no)
            or order_no in order_nos
        ):
            return None
        order_nos.append(order_no)
    return order_nos

def _previous_order_list(messages: list[BaseMessage]) -> list[str] | None:
    """从当前对话历史中取最近一份规范客服列表，供语义选择做服务端复核。"""
    conversation_messages = [
        message for message in messages if not isinstance(message, SystemMessage)
    ]
    if (
        len(conversation_messages) < 2
        or not isinstance(conversation_messages[-1], HumanMessage)
    ):
        return None
    for message in reversed(conversation_messages[:-1]):
        if not isinstance(message, AIMessage):
            continue
        order_nos = _parse_order_list(_content_as_text(message.content))
        if order_nos is not None:
            return order_nos
    return None

def _orders_matching_suffix(order_nos: list[str], suffix: str) -> list[str]:
    """按不区分大小写的编号末尾片段筛选候选，不从助手文字生成编号。"""
    normalized_suffix = suffix.casefold()
    return [
        order_no
        for order_no in order_nos
        if order_no.casefold().endswith(normalized_suffix)
    ]

def _order_lookup_result(state: SupportState) -> dict[str, Any]:
    """只从本人近期订单结果生成列表，或核验选择后开放指定订单的下一步查询。"""
    tool_message = next(
        (
            message
            for message in reversed(state.get("messages", []))
            if isinstance(message, ToolMessage) and message.name == "query_order"
        ),
        None,
    )
    try:
        payload = json.loads(_content_as_text(tool_message.content)) if tool_message else {}
    except (TypeError, ValueError):
        payload = {}

    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {
            "allowed_tools": [],
            "order_lookup_pending": False,
            "direct_reply": "我暂时无法查询当前账号的订单，请稍后重试。",
        }

    orders = payload.get("orders")
    orders = orders if isinstance(orders, list) else []
    valid_orders = []
    seen_order_nos: set[str] = set()
    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get("order_no"), str):
            continue
        order_no = order["order_no"]
        if (
            not order_no
            or len(order_no) > 64
            or order_no.strip() != order_no
            or any(ord(char) < 32 or char in "\u2028\u2029" for char in order_no)
            or order_no in seen_order_nos
        ):
            continue
        seen_order_nos.add(order_no)
        valid_orders.append(order)
    mode = state.get("order_lookup_mode")

    if not valid_orders:
        return {
            "allowed_tools": [],
            "order_lookup_pending": False,
            "direct_reply": "当前账号下没有找到近期订单，请提供要查询的订单号。",
        }

    if mode in {"latest", "refund_latest"}:
        # SQL 已按下单时间倒序；仅在用户明确指定最近一笔时才采用第一条。
        selected = valid_orders[0]
    elif mode in {"order_list", "logistics_list", "refund_list"}:
        return {
            "allowed_tools": [],
            "order_lookup_pending": False,
            "direct_reply": _order_list_reply(valid_orders, refund=mode == "refund_list"),
        }
    elif mode in {"selection_order", "selection_logistics"}:
        requested_order_no = state.get("selection_order_no")
        selected = next(
            (
                order
                for order in valid_orders
                if order["order_no"] == requested_order_no
            ),
            None,
        )
        if selected is None:
            return {
                "allowed_tools": [],
                "order_lookup_pending": False,
                "direct_reply": _order_list_reply(valid_orders),
            }

        semantic = state.get("semantic_result")
        if isinstance(semantic, dict):
            semantic = dict(semantic)
            entities = semantic.get("entities")
            if isinstance(entities, dict):
                entities = dict(entities)
                entities["order_no"] = selected["order_no"]
                entities["order_reference"] = "explicit"
                semantic["entities"] = entities
        next_tool = "query_order" if mode == "selection_order" else "query_logistics"
        return {
            "semantic_result": semantic,
            "allowed_tools": [next_tool],
            "order_lookup_pending": False,
            "authorized_order_no": selected["order_no"],
            "direct_reply": "",
        }
    else:
        return {
            "allowed_tools": [],
            "order_lookup_pending": False,
            "direct_reply": _FALLBACK_REPLY,
        }

    return {
        "allowed_tools": ["query_order" if mode == "refund_latest" else "query_logistics"],
        "order_lookup_pending": False,
        "authorized_order_no": selected["order_no"].strip(),
        "direct_reply": "",
    }

def _order_list_reply(orders: list[dict[str, Any]], *, refund: bool = False) -> str:
    """展示本人订单的商品快照，并保留只从服务端生成的可解析订单号。"""
    options = [
        f"{index}. {order['order_no']} — {_order_items_summary(order)}"
        for index, order in enumerate(orders, start=1)
    ]
    header = _REFUND_ORDER_LIST_HEADER if refund else _ORDER_LIST_HEADER
    return header + "\n" + "\n".join(options)

def _order_items_summary(order: dict[str, Any]) -> str:
    """按工具快照列出前三项商品；省略项按数量汇总，不根据商品名推断内容。"""
    items = order.get("items")
    if not isinstance(items, list):
        return "商品明细暂缺"

    displayed: list[str] = []
    omitted_count = 0
    omitted_quantity = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        product_name = item.get("product_name")
        quantity = item.get("quantity")
        if (
            not isinstance(product_name, str)
            or not product_name.strip()
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
        ):
            continue
        product_name = " ".join(product_name.split())
        if len(displayed) < _ORDER_LIST_PRODUCT_LIMIT:
            displayed.append(f"{product_name} ×{quantity}")
        else:
            omitted_count += 1
            omitted_quantity += quantity

    if omitted_count:
        if omitted_quantity:
            displayed.append(f"另有 {omitted_quantity} 件商品")
        else:
            displayed.append(f"另有 {omitted_count} 项商品")
    return "、".join(displayed) if displayed else "商品明细暂缺"
