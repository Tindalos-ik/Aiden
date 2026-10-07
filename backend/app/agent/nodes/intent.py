import json
import logging
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import ValidationError
from langgraph.graph.state import StateNode

from app.agent.intent import (
    IntentClassification,
    MULTI_REQUEST_PROMPT,
    SemanticExtraction,
    SupportRequest,
    SupportRequests,
)
from app.agent.messages import _contains_exact_identifier, _content_as_text, _latest_user_text
from app.agent.order_routing import _semantic_route
from app.agent.order_selection import _parse_order_list, _previous_order_list
from app.agent.prompt import _FALLBACK_REPLY, _INTENT_FORMAT_REPLY, _TOO_MANY_REQUESTS
from app.agent.refund import _route_refund_request
from app.agent.service_intent import ServiceExtraction
from app.agent.state import SupportState
from app.config.settings import settings


logger = logging.getLogger(__name__)

_SERVICE_TOOL_BY_GOAL = {
    "policy": "search_faq",
    "after_sale_status": "query_after_sale",
    "ticket_status": "query_ticket",
    "create_ticket": "create_ticket",
}

_MIN_INTENT_CONFIDENCE = 0.55

_REQUEST_GOALS = {
    "order": {"order"},
    "logistics": {"logistics"},
    "product": {"product"},
    "refund_return": {"policy", "after_sale_status", "ticket_status", "create_ticket"},
    "after_sales": {"policy", "after_sale_status", "ticket_status", "create_ticket"},
    "complaint": {"ticket_status", "create_ticket"},
    "human": {"ticket_status", "other"},
    "smalltalk": {"smalltalk"},
    "other": {"other"},
}

def _service_route(
    classification: IntentClassification, result: ServiceExtraction, messages: list[BaseMessage]
) -> dict[str, Any]:
    """把服务目标映射到固定工具，并核对编号确实来自本轮用户原文。"""
    current_text = _latest_user_text(messages)
    data = result.model_dump()
    goal = data["goal"]
    for key in ("order_no", "request_no", "ticket_no"):
        value = data.get(key)
        if not isinstance(value, str) or not _contains_exact_identifier(current_text, value):
            data[key] = None
    data["goal"] = goal
    return {
        "semantic_result": {
            "classification": classification.model_dump(),
            "service": data,
        },
        "completed_question": data["completed_question"].strip(),
        "allowed_tools": [_SERVICE_TOOL_BY_GOAL[goal]],
        "direct_reply": "",
        "order_lookup_pending": False,
    }

def _route_request(
    request: SupportRequest, messages: list[BaseMessage],
    same_turn_order_options: list[str] | None = None,
    refund_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """将已核对的意图目标映射为唯一工具权限。"""
    if request.goal == "other":
        return {"allowed_tools": [], "direct_reply": _FALLBACK_REPLY}
    if request.intent == "refund_return" and request.goal == "create_ticket":
        return _route_refund_request(request, messages, same_turn_order_options,
                                     refund_context or {})
    classification = IntentClassification(intent=request.intent, cofidence=request.cofidence)
    if request.goal in {"order", "logistics", "product", "smalltalk"}:
        extraction = SemanticExtraction.model_validate(request.model_dump(include={
            "completed_question", "entities", "needs_clarification", "clarification_question",
        }))
        return _semantic_route(
            classification, extraction, messages,
            same_turn_order_options=same_turn_order_options,
        )
    service = ServiceExtraction(
        goal=request.goal,
        completed_question=request.completed_question,
        order_no=request.entities.order_no,
        request_no=request.request_no,
        ticket_no=request.ticket_no,
    )
    routed = _service_route(classification, service, messages)
    if request.needs_clarification and request.goal in {"after_sale_status", "create_ticket"}:
        routed["allowed_tools"] = []
        routed["direct_reply"] = "请补充这项请求所需的完整业务编号或具体问题。"
    return routed




def _structured_messages(
    messages: list[BaseMessage], prompt: str, schema: type[SupportRequests],
) -> list[BaseMessage]:
    """结构化节点只接收会话历史和本阶段 schema，不接收业务工具定义。"""
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    instruction = (
        prompt
        + "\n\n只输出一个符合以下 JSON Schema 的 JSON 对象，不要添加 Markdown 或说明：\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    return [SystemMessage(content=instruction), *history]

def make_recognize_intent(json_model: Runnable[LanguageModelInput, BaseMessage]) -> StateNode[SupportState, None]:
    """绑定非流式 JSON 模型，识别阶段只发送受控进度事件。"""
    async def recognize_intent(state: SupportState) -> dict[str, Any]:
        """先按本轮原话识别；格式异常重试一次，指代不明时再借助历史。"""
        current_message = HumanMessage(content=_latest_user_text(state["messages"]))
        current_only = _structured_messages([current_message], MULTI_REQUEST_PROMPT, SupportRequests)
        await adispatch_custom_event(
            "support_progress", {"stage": "recognition", "message": "正在识别您的服务诉求"}
        )
        requests: SupportRequests | None = None
        for attempt in range(2):
            messages = current_only
            if attempt:
                messages = [
                    SystemMessage(content=(
                        str(current_only[0].content)
                        + "\n上次响应不是合法 JSON。请立即输出完整 JSON 对象，以 { 开头、以 } 结尾。"
                    )),
                    current_message,
                ]
            response = await json_model.ainvoke(
                messages,
                config=(
                    {
                        "metadata": {"cost_bucket": "intent_classification"},
                        "run_name": "cost_bucket_intent_classification",
                    }
                    if settings.langfuse_enabled else None
                ),
            )
            content = _content_as_text(response.content)
            try:
                requests = SupportRequests.model_validate_json(content)
                break
            except (TypeError, ValueError) as exc:
                # 仅记录错误类型和字段位置，不把用户消息或模型原文写入服务日志。
                issues = (
                    [f"{'.'.join(map(str, issue['loc']))}:{issue['type']}"
                     for issue in exc.errors(include_input=False)[:5]]
                    if isinstance(exc, ValidationError) else [type(exc).__name__]
                )
                logger.warning("Intent result rejected (attempt %d): %s", attempt + 1, issues)
                try:
                    raw = json.loads(content)
                    too_many = isinstance(raw, dict) and isinstance(raw.get("requests"), list) and len(raw["requests"]) > 4
                except (TypeError, ValueError):
                    too_many = False
                if too_many:
                    return {
                        "requests": [], "request_index": 0, "request_results": [],
                        "direct_reply": _TOO_MANY_REQUESTS,
                    }
        if requests is None:
            return {
                "requests": [], "request_index": 0, "request_results": [],
                "direct_reply": _INTENT_FORMAT_REPLY,
            }

        # 只有当前表达无法独立定位目标时才带历史重试，避免旧的政策问答覆盖本轮办理诉求。
        if len(state["messages"]) > 2 and (
            bool(state.get("refund_context"))
            or _previous_order_list(state["messages"]) is not None
            or any(item.intent == "other" or item.needs_clarification for item in requests.requests)
        ):
            try:
                context_response = await json_model.ainvoke(
                    _structured_messages(state["messages"], MULTI_REQUEST_PROMPT, SupportRequests),
                    config=(
                        {
                            "metadata": {"cost_bucket": "intent_classification"},
                            "run_name": "cost_bucket_intent_classification",
                        }
                        if settings.langfuse_enabled else None
                    ),
                )
                requests = SupportRequests.model_validate_json(
                    _content_as_text(context_response.content)
                )
            except (TypeError, ValueError):
                logger.warning("Contextual intent result rejected; using current-message result")
        current_text = _latest_user_text(state["messages"])
        if len(requests.requests) > 1 and any(
            not item.original_question_quote or item.original_question_quote.strip() not in current_text
            for item in requests.requests
        ):
            # 多诉求没有可信的原话片段时不能把某项证据错误地挂在整轮话或另一项原话上。
            return {
                "requests": [], "request_index": 0, "request_results": [],
                "direct_reply": _INTENT_FORMAT_REPLY,
            }
        recognized_intents = [
            {"intent": item.intent, "goal": item.goal} for item in requests.requests
        ]
        if settings.langfuse_enabled:
            from langfuse import get_client

            get_client().update_current_trace(metadata={"recognized_intents": recognized_intents})
        return {
            "requests": [item.model_dump() for item in requests.requests],
            "request_index": 0, "request_results": [], "query_cache": [],
            "direct_reply": "",
        }

    return recognize_intent


def route_intent(state: SupportState) -> dict[str, Any]:
    """按模型给出的目标路由；写操作只核对动作原话确实来自本轮用户。"""
    requests = state.get("requests", [])
    index = state.get("request_index", 0)
    if index >= len(requests):
        return {"allowed_tools": []}
    try:
        request = SupportRequest.model_validate(requests[index])
    except (TypeError, ValidationError):
        return {"allowed_tools": [], "direct_reply": _FALLBACK_REPLY}
    if request.goal not in _REQUEST_GOALS[request.intent] or (
        request.cofidence < _MIN_INTENT_CONFIDENCE and request.intent != "smalltalk"
    ):
        logger.warning(
            "Intent route rejected: intent=%s goal=%s confidence=%.2f",
            request.intent, request.goal, request.cofidence,
        )
        return {
            "allowed_tools": [], "direct_reply": _FALLBACK_REPLY,
            "handoff_requested": False,
            "request_tool_start": len(state.get("messages", [])), "citations": [],
        }
    if request.intent == "other":
        logger.warning("Intent classified as other: goal=%s confidence=%.2f", request.goal, request.cofidence)
    if (request.goal == "create_ticket" and request.intent != "refund_return") or (request.intent == "human" and request.goal == "other"):
        quote = request.action_quote.strip() if request.action_quote else ""
        if not quote or quote not in _latest_user_text(state.get("messages", [])):
            return {
                "allowed_tools": [],
                "direct_reply": (
                    "请明确说明是否需要转接人工客服。" if request.intent == "human"
                    else "请明确说明是否需要我登记人工处理工单。"
                ),
                "handoff_requested": False,
                "request_tool_start": len(state.get("messages", [])),
                "citations": [],
            }
        if request.intent == "human" and request.goal == "other":
            # 真人接入只修改会话状态，事务在 save_answer 中完成。
            return {
                "allowed_tools": [],
                "direct_reply": "已进入人工客服队列，客服接单后会在本会话回复。",
                "handoff_requested": True,
                "request_tool_start": len(state.get("messages", [])),
                "citations": [],
            }
    if request.goal == "create_ticket" and any(
        cached.get("name") == "create_ticket" for cached in state.get("query_cache", [])
    ):
        return {
            "allowed_tools": [],
            "direct_reply": "本轮已尝试登记一项处理请求；其他办理诉求请另发一条消息。",
            "request_tool_start": len(state.get("messages", [])),
            "citations": [],
        }
    same_turn_order_options = next((
        options
        for result in reversed(state.get("request_results", []))
        if (options := _parse_order_list(result.get("direct_reply", ""))) is not None
    ), None)
    routed = _route_request(
        request, state.get("messages", []),
        same_turn_order_options=same_turn_order_options,
        refund_context=state.get("refund_context", {}),
    )
    routed["request_tool_start"] = len(state.get("messages", []))
    routed["cached_tool_hit"] = False
    routed["citations"] = []
    routed["handoff_requested"] = False
    if request.intent != "refund_return" or request.goal != "create_ticket":
        routed["refund_phase"] = ""
        routed["refund_authorized"] = False
        routed["refund_order_no"] = ""
        routed["refund_reason"] = ""
        routed["refund_policy_id"] = ""
    return routed
