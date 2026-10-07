import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.state import StateNode

from app.agent.messages import _content_as_text, _valid_identifier
from app.agent.order_selection import _order_lookup_result
from app.agent.prompt import _TOOL_REJECTED_REPLY
from app.agent.refund import _refund_order_result, _refund_policy_result
from app.agent.state import SupportState
from app.services.tools.registry import SUPPORT_TOOLS_BY_NAME


# 工具注册和意图权限由服务端静态定义；模型不能提交工具名或用户身份。
_TOOLS_BY_NAME = SUPPORT_TOOLS_BY_NAME

_TOOL_NODE_BY_NAME = {name: f"run_{name}" for name in _TOOLS_BY_NAME}


def make_dispatch_tool_call(evaluation: dict[str, Any] | None) -> StateNode[SupportState, None]:
    """绑定评估写入闸门；未经授权或未隔离的业务写工具不得派发。"""
    async def dispatch_tool_call(state: SupportState) -> dict[str, Any]:
        """由服务端构造已批准的工具调用，避免模型漏调工具或改写订单参数。"""
        allowed_names = state.get("allowed_tools", [])
        if len(allowed_names) != 1 or allowed_names[0] not in _TOOLS_BY_NAME:
            return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}

        name = allowed_names[0]
        if evaluation is not None and name in {"submit_after_sale", "create_ticket"}:
            if not evaluation.get("allow_writes"):
                evaluation.setdefault("blocked_tools", []).append(name)
                return {"allowed_tools": [], "direct_reply": "评估安全边界：业务写入未授权。"}
            if (not evaluation.get("isolated_environment")
                    or state["user_id"] != evaluation.get("isolated_account")
                    or not state["user_id"].startswith("eval_")):
                raise ValueError("写评估必须使用明确隔离的环境和 eval_ 账户")
        args: dict[str, Any] = {}
        semantic = state.get("semantic_result", {})
        entities = semantic.get("entities", {}) if isinstance(semantic, dict) else {}
        service = semantic.get("service", {}) if isinstance(semantic, dict) else {}
        classification = semantic.get("classification", {}) if isinstance(semantic, dict) else {}

        if name == "search_faq":
            args["question"] = state.get("completed_question", "").strip()
        elif name == "query_refund_policy":
            # 政策只查本人已核验订单当前生效的商家配置，不接受模型传入的订单号。
            order_no = state.get("refund_order_no")
            if state.get("refund_phase") != "policy" or not _valid_identifier(order_no):
                return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}
            args["order_no"] = order_no
        elif name == "query_order":
            # 列候选或核验列表选择时只查当前用户的近期订单，不接受模型提供的订单号。
            if not state.get("order_lookup_pending"):
                order_no = state.get("authorized_order_no")
                if not order_no and isinstance(entities, dict):
                    order_no = entities.get("order_no")
                if isinstance(order_no, str) and order_no.strip():
                    args["order_no"] = order_no.strip()
        elif name == "query_logistics":
            # 物流工具只使用服务端在路由或订单核验阶段授权的目标。
            order_no = state.get("authorized_order_no")
            if not isinstance(order_no, str) or not order_no.strip():
                return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}
            args["order_no"] = order_no.strip()
        elif name == "query_after_sale":
            for key in ("request_no", "order_no"):
                value = service.get(key) if isinstance(service, dict) else None
                if isinstance(value, str) and value.strip():
                    args[key] = value.strip()
        elif name == "query_ticket":
            ticket_no = service.get("ticket_no") if isinstance(service, dict) else None
            if isinstance(ticket_no, str) and ticket_no.strip():
                args["ticket_no"] = ticket_no.strip()
        elif name == "submit_after_sale":
            if not state.get("refund_authorized") or state.get("refund_phase") != "confirm":
                return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}
            policy_id = state.get("refund_policy_id")
            if not _valid_identifier(policy_id):
                return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}
            args["order_no"] = state["refund_order_no"]
            args["reason"] = state["refund_reason"]
            args["policy_id"] = policy_id
            args["policy_snapshot"] = state["refund_policy_snapshot"]
        elif name == "create_ticket":
            issue_type = classification.get("intent") if isinstance(classification, dict) else None
            if issue_type not in {"after_sales", "complaint", "human"}:
                return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}
            args["issue_type"] = issue_type
            request_index = state.get("request_index", 0)
            args["description"] = state["requests"][request_index]["action_quote"]

        call = {
            "name": name,
            "args": args,
            "id": str(uuid4()),
            "type": "tool_call",
        }
        cache_key = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
        for cached in state.get("query_cache", []):
            if cached["key"] == cache_key:
                return {
                    "messages": [AIMessage(content="", tool_calls=[call]), ToolMessage(
                        content=cached["content"], name=name, tool_call_id=call["id"],
                    )],
                    "cached_tool_hit": True,
                    "pending_cache_key": cache_key,
                }
        return {
            "messages": [AIMessage(content="", tool_calls=[call])],
            "cached_tool_hit": False,
            "pending_cache_key": cache_key,
        }

    return dispatch_tool_call


def after_tools(state: SupportState) -> dict[str, Any]:
    """工具返回后关闭本轮权限；物流前置查单只有唯一目标才开放物流工具。"""
    updates: dict[str, Any] = {}
    latest = state.get("messages", [])[-1]
    cache_key = state.get("pending_cache_key")
    if (
        isinstance(latest, ToolMessage) and cache_key and not state.get("cached_tool_hit")
    ):
        updates["query_cache"] = [*state.get("query_cache", []), {
            "key": cache_key, "name": latest.name, "content": _content_as_text(latest.content),
        }]
    if (
        state.get("order_lookup_pending")
        and state.get("messages")
        and isinstance(state["messages"][-1], ToolMessage)
        and state["messages"][-1].name == "query_order"
    ):
        return {**updates, **_order_lookup_result(state)}
    if (state.get("refund_phase") == "order" and isinstance(latest, ToolMessage)
            and latest.name == "query_order"):
        return {**updates, **_refund_order_result(state)}
    if (state.get("refund_phase") == "policy" and isinstance(latest, ToolMessage)
            and latest.name == "query_refund_policy"):
        return {**updates, **_refund_policy_result(state)}
    # 查询工具一旦执行完毕就收回白名单，防止模型在同一意图下重复或扩展查询。
    return {**updates, "allowed_tools": [], "order_lookup_pending": False}
