import json
import re
from typing import Any
from urllib.parse import urlencode

from langchain_core.messages import BaseMessage

from app.agent.intent import IntentClassification, SemanticExtraction, SupportRequest
from app.agent.messages import _content_as_text, _latest_user_text, _valid_identifier
from app.agent.order_routing import _semantic_route
from app.agent.order_selection import _order_items_summary
from app.agent.prompt import _REFUND_REASON_PROMPT
from app.agent.state import SupportState


def _verified_refund_reason(request: SupportRequest, messages: list[BaseMessage]) -> str:
    """原因必须逐字来自本轮用户，规范化后才能放入固定确认话术。"""
    quote = (request.refund_reason_quote or "").strip()
    if not quote or quote not in _latest_user_text(messages):
        return ""
    return " ".join(quote.replace("；", "，").split())[:160]

def _refund_order_result(state: SupportState) -> dict[str, Any]:
    """订单必须由本轮本人查询证实；原因齐备后才查询本人订单可用的有效商家政策。"""
    latest = state.get("messages", [])[-1]
    try:
        payload = json.loads(_content_as_text(latest.content))
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {"allowed_tools": [], "refund_phase": "", "direct_reply": "暂时无法核对这笔订单，请稍后重试。"}
    semantic = state.get("semantic_result", {})
    entities = semantic.get("entities", {}) if isinstance(semantic, dict) else {}
    requested = state.get("refund_order_no") or state.get("authorized_order_no") or entities.get("order_no")
    orders = payload.get("orders") if isinstance(payload.get("orders"), list) else []
    matched = next((row for row in orders if isinstance(row, dict)
                    and row.get("order_no") == requested), None)
    if not _valid_identifier(requested) or matched is None:
        return {"allowed_tools": [], "refund_phase": "", "direct_reply": "当前账号下没有核对到这笔订单，请提供本人订单号或从近期订单中选择。"}
    kind = state.get("refund_request_type", "refund")
    if kind in {"return", "exchange"} or len(matched.get("items") or []) != 1:
        params = {"orderNo": requested, "requestType": kind}
        if state.get("refund_reason"):
            params["reason"] = state["refund_reason"]
        return {"allowed_tools": [], "refund_phase": "", "refund_policy_id": "",
                "direct_reply": f"已核验本人订单 {requested}。请[前往售后页面](/app/service?{urlencode(params)})，"
                                "下一步选择具体商品、填写或核对原因、核对当前政策后确认提交。"}
    reason = state.get("refund_reason", "")
    if not reason:
        return {"allowed_tools": [], "refund_phase": "", "refund_order_no": requested,
                "refund_order_facts": _refund_order_facts(matched),
                "direct_reply": _REFUND_REASON_PROMPT.format(order_no=requested)}
    # 是否可提交只依据本人订单可用的有效商家政策；检索到的 FAQ 只是说明材料。
    return {
        "allowed_tools": ["query_refund_policy"], "refund_phase": "policy",
        "refund_order_no": requested,
        "refund_order_facts": _refund_order_facts(matched),
        "direct_reply": "",
    }

def _refund_order_facts(order: dict[str, Any]) -> str:
    """把已核验订单压缩成一行事实；无法提交时只引用这里已核实的内容。"""
    parts = [f"订单号 {order.get('order_no')}"]
    status = order.get("status")
    if isinstance(status, str) and status.strip():
        parts.append(f"状态 {status}")
    items = order.get("items")
    if isinstance(items, list) and items:
        parts.append(f"商品 {_order_items_summary(order)}")
    return "；".join(parts)

def _refund_manual_reply(facts: str, reason: str, order_no: str) -> str:
    """页面只用于重新核对政策，无有效政策时两种入口均不能正式提交。"""
    target = urlencode({"orderNo": order_no, "requestType": "refund", "reason": reason})
    return (f"{facts}。关于退款原因“{reason}”，目前没有可核验且覆盖该原因的有效商家退款政策，"
            f"我不会据此提交退款申请。可[前往售后与工单页面](/app/service?{target})选择商品、"
            "核对原因和当前政策；没有有效政策时页面也不能正式提交。"
            "您可以咨询政策，或明确要求人工协助核对、审核；这不表示已经创建售后申请或已接入人工。")

def _normalize_policy_text(text: str) -> str:
    """去空白与标点后做字面比对，避免把标点差异当成条款差异。"""
    return re.sub(r"[\s，。；：、,.!?！？()（）「」【】“”\"'·:;]", "", text)

def _applicable_refund_policy(policies: Any, reason: str) -> dict[str, Any] | None:
    """挑出正文明确覆盖本次退款原因的有效商家政策。

    商家政策行已由持久化层按订单类型过滤出当前生效版本，这里只再判断原因适用性：
    要求政策正文出现用户陈述的原因字样。同义表述（例如“商品与描述不符”对“货不对板”）
    视为无法核验，宁可转人工，也不让泛化条款成为提交依据。
    """
    target = _normalize_policy_text(reason)
    if not target or not isinstance(policies, list):
        return None
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        policy_id = policy.get("id")
        content = policy.get("content")
        if (isinstance(policy_id, str) and _valid_identifier(policy_id)
                and isinstance(content, str) and isinstance(policy.get("snapshot"), str)
                and target in _normalize_policy_text(content)):
            return policy
    return None

def _current_request_question(state: SupportState) -> str:
    """取当前诉求的规范问题用于低置信记录；缺失时退回本轮用户原话。"""
    requests = state.get("requests", [])
    index = state.get("request_index", 0)
    if 0 <= index < len(requests):
        question = requests[index].get("completed_question")
        if isinstance(question, str) and question.strip():
            return question.strip()
    return _latest_user_text(state.get("messages", []))

def _refund_policy_result(state: SupportState) -> dict[str, Any]:
    """政策正文覆盖本次原因才进入确认阶段；否则给出人工办理路径并记录低置信。"""
    latest = state.get("messages", [])[-1]
    try:
        payload = json.loads(_content_as_text(latest.content))
    except (TypeError, ValueError):
        payload = {}
    order_no = state.get("refund_order_no", "")
    reason = state.get("refund_reason", "")
    policy = _applicable_refund_policy(
        payload.get("policies") if isinstance(payload, dict) else None, reason,
    )
    if policy is None:
        facts = state.get("refund_order_facts") or f"订单号 {order_no}"
        return {
            "allowed_tools": [], "refund_phase": "", "refund_policy_id": "",
            "direct_reply": _refund_manual_reply(facts, reason, order_no),
            "request_low_confidence": {
                "original_question": _current_request_question(state),
                "entrypoint": "refund_policy_unavailable",
                "reason": "没有可核验且覆盖本次退款原因的有效商家退款政策",
            },
        }
    return {
        "allowed_tools": [], "refund_phase": "confirm",
        "refund_policy_id": str(policy["id"]), "direct_reply": "",
        "refund_policy_snapshot": str(policy["snapshot"]),
    }

def _route_refund_request(
    request: SupportRequest, messages: list[BaseMessage],
    same_turn_order_options: list[str] | None, context: dict[str, str],
) -> dict[str, Any]:
    """退款办理先核单和商家政策；只有上一轮已核验政策并给出固定确认请求才可能写入。"""
    kind = request.application_type if request.application_type != "none" else context.get("request_type", "none")
    if kind == "none" and context.get("stage") not in {"reason", "confirm", "order"}:
        return {"allowed_tools": [], "direct_reply": "请说明要申请退款、退货还是换货；也可以在“售后与工单”页面选择申请类型。",
                "refund_phase": "", "refund_authorized": False, "refund_policy_id": ""}
    if kind == "refund" and request.refund_consent == "decline" and context.get("stage") in {"reason", "confirm"}:
        return {"allowed_tools": [], "direct_reply": "好的，我不会提交这项退款申请。",
                "refund_phase": "", "refund_authorized": False, "refund_policy_id": ""}
    if kind == "refund" and request.refund_consent == "agree":
        quote = (request.action_quote or "").strip()
        policy_id = context.get("policy_id", "")
        # 确认原话、订单、原因和上一轮核验过的政策 ID 必须同时成立才能发起写入。
        if (context.get("stage") == "confirm" and quote
                and quote in _latest_user_text(messages) and _valid_identifier(policy_id)
                and context.get("policy_snapshot")):
            order_no, reason = context["order_no"], context["reason"]
            return {
                "allowed_tools": ["submit_after_sale"], "direct_reply": "",
                "refund_phase": "confirm", "refund_order_no": order_no,
                "refund_reason": reason, "refund_policy_id": policy_id,
                "refund_policy_snapshot": context["policy_snapshot"],
                "refund_request_type": "refund",
                "refund_authorized": True,
                "semantic_result": {"classification": {"intent": "refund_return"}},
            }
        return {"allowed_tools": [], "direct_reply": "请先确定订单和退款原因，待我核对政策后再确认是否提交申请。",
                "refund_phase": "", "refund_authorized": False, "refund_policy_id": ""}

    reason = _verified_refund_reason(request, messages)
    if not reason and context.get("stage") == "order":
        reason = context.get("reason", "")
    if context.get("stage") == "reason" or (kind in {"return", "exchange"} and context.get("stage") == "confirm"):
        order_no = context["order_no"]
        return {
            "allowed_tools": ["query_order"], "direct_reply": "",
            "refund_phase": "order", "refund_order_no": order_no,
            "refund_reason": reason or context.get("reason", ""), "refund_authorized": False,
            "refund_request_type": kind,
            "authorized_order_no": order_no, "order_lookup_pending": False,
        }

    extraction = SemanticExtraction(
        completed_question=request.completed_question,
        entities=request.entities, needs_clarification=False,
        clarification_question=None,
    )
    routed = _semantic_route(
        IntentClassification(intent="order", cofidence=request.cofidence),
        extraction, messages, same_turn_order_options=same_turn_order_options,
    )
    routed.update({"refund_phase": "order", "refund_reason": reason,
                   "refund_authorized": False, "refund_order_no": "",
                   "refund_policy_id": ""})
    routed["refund_request_type"] = kind
    if routed.get("order_lookup_mode") == "order_list":
        routed["order_lookup_mode"] = "refund_list"
    elif request.entities.order_reference == "latest" and routed.get("allowed_tools") == ["query_order"]:
        routed["order_lookup_pending"] = True
        routed["order_lookup_mode"] = "refund_latest"
    if routed.get("allowed_tools") != ["query_order"]:
        routed["refund_phase"] = ""
    return routed
