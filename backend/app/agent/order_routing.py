from typing import Any

from langchain_core.messages import BaseMessage

from app.agent.intent import IntentClassification, SemanticExtraction
from app.agent.messages import _contains_exact_identifier, _latest_user_text
from app.agent.order_selection import _orders_matching_suffix, _previous_order_list
from app.agent.prompt import _FALLBACK_REPLY


def _clarification_reply(result: dict[str, Any]) -> str:
    """使用受控追问模板，避免分类器把凭据或无关要求变成用户可见问题。"""
    intent = result.get("intent")

    if intent == "order":
        return "请告诉我订单号，或说明您想查看最近几笔订单。"
    if intent == "logistics":
        return "请提供要查询物流的订单号；如果您指最近一笔订单，也可以明确说明。"
    if intent == "product":
        # 知识检索的澄清只接受短问题，并屏蔽凭据类要求；其余情况使用固定追问。
        question = result.get("clarification_question")
        if isinstance(question, str):
            question = question.strip()
            sensitive_terms = ("密码", "验证码", "身份证", "手机号", "银行卡", "password", "token")
            if (
                question
                and len(question) <= 120
                and not any(term in question.casefold() for term in sensitive_terms)
            ):
                return question
        return "请再具体描述您想咨询的问题，我会根据已入库的知识检索。"
    return _FALLBACK_REPLY

def _semantic_route(
    classification: IntentClassification, result: SemanticExtraction, messages: list[BaseMessage],
    same_turn_order_options: list[str] | None = None,
) -> dict[str, Any]:
    """依模型的引用类型选工具；服务端只核对原文来源、列表归属和用户数据范围。"""
    data = result.model_dump()
    data["intent"] = classification.intent
    data["cofidence"] = classification.cofidence
    entities = data["entities"]
    order_no = (entities.get("order_no") or "").strip() or None
    reference = entities.get("order_reference", "none")
    current_text = _latest_user_text(messages)
    recent_options = same_turn_order_options or _previous_order_list(messages)
    reference_quote = (entities.get("reference_quote") or "").strip()
    quote_is_current = bool(reference_quote and reference_quote in current_text)
    list_index = entities.get("list_index")
    suffix_fragment = (entities.get("order_suffix") or "").strip()
    if order_no:
        if reference == "explicit" and _contains_exact_identifier(current_text, order_no):
            entities["order_no"] = order_no
        elif reference == "listed_selection" and quote_is_current:
            listed_matches = [
                candidate
                for candidate in (recent_options or [])
                if candidate.casefold() == order_no.casefold()
            ]
            if len(listed_matches) == 1 and (
                list_index is None or (
                    recent_options is not None
                    and 1 <= list_index <= len(recent_options)
                    and recent_options[list_index - 1] == listed_matches[0]
                )
            ):
                order_no = listed_matches[0]
                entities["order_no"] = order_no
            else:
                entities["order_no"] = None
                entities["order_reference"] = "ambiguous"
                reference = "ambiguous"
        else:
            entities["order_no"] = None
            entities["order_reference"] = "none"
            reference = entities["order_reference"]

    if reference == "latest" and not quote_is_current:
        reference = "none"
        entities["order_reference"] = reference

    base = {
        "semantic_result": data,
        "completed_question": data["completed_question"].strip(),
        "allowed_tools": [],
        "direct_reply": "",
        "order_lookup_pending": False,
        "order_lookup_mode": "",
        "selection_order_no": "",
        "authorized_order_no": "",
    }

    if not base["completed_question"]:
        base["direct_reply"] = _FALLBACK_REPLY
        return base

    if data["intent"] not in {"order", "logistics"}:
        if data["intent"] == "smalltalk":
            # 简单对话无需业务工具；主模型只按 System Prompt 说明身份和已接入能力。
            return base
        if data["needs_clarification"]:
            base["direct_reply"] = _clarification_reply(data)
            return base
        if data["intent"] == "product":
            base["allowed_tools"] = ["search_faq"]
            return base
        base["direct_reply"] = _FALLBACK_REPLY
        return base

    current_order_no = entities.get("order_no")
    # 模型解释引用方式；服务端只把其候选绑定到刚展示过的真实列表。
    selected_order_no = None
    if suffix_fragment:
        entities["order_no"] = None
        current_order_no = None
        if quote_is_current and _contains_exact_identifier(reference_quote, suffix_fragment) and recent_options:
            matches = _orders_matching_suffix(recent_options, suffix_fragment)
            if len(matches) == 1:
                selected_order_no = matches[0]
            else:
                base["direct_reply"] = "订单尾号未能唯一匹配，请提供完整订单号或列表序号。"
                return base
    elif reference == "listed_selection":
        if quote_is_current and recent_options:
            if list_index is not None and 1 <= list_index <= len(recent_options):
                selected_order_no = recent_options[list_index - 1]
            elif current_order_no in recent_options:
                selected_order_no = current_order_no
        if selected_order_no is None:
            base["direct_reply"] = "我没能从刚才的订单列表中确定您指的订单，请回复列表中的序号。"
            return base

    if selected_order_no:
        entities["order_no"] = None
        if same_turn_order_options and data["intent"] == "logistics":
            # 前一项本轮查询已验证列表属于当前用户，可直接查选中的物流。
            base["allowed_tools"] = ["query_logistics"]
            base["authorized_order_no"] = selected_order_no
        else:
            base.update({
                "allowed_tools": ["query_order"],
                "order_lookup_pending": True,
                "order_lookup_mode": (
                    "selection_order" if data["intent"] == "order" else "selection_logistics"
                ),
                "selection_order_no": selected_order_no,
            })
        return base

    # 目标已唯一时，needs_clarification 仍然阻止直接查单；目标缺失或不唯一时，
    # 它表示需要用户在下方的本人近期订单列表中选择，而不是阻止列单。
    if current_order_no and data["needs_clarification"]:
        base["direct_reply"] = _clarification_reply(data)
        return base

    if data["intent"] == "order":
        if current_order_no:
            base["allowed_tools"] = ["query_order"]
        elif reference == "latest":
            if data["needs_clarification"]:
                base["direct_reply"] = _clarification_reply(data)
                return base
            # 保留用户明确要求最近一笔时的原有 query_order 语义。
            base["allowed_tools"] = ["query_order"]
        else:
            # 缺失、历史指代不唯一或用户要求从本人订单中确定时先列真实选项。
            base.update(
                {
                    "allowed_tools": ["query_order"],
                    "order_lookup_pending": True,
                    "order_lookup_mode": "order_list",
                }
            )
        return base

    if data["intent"] == "logistics":
        if current_order_no:
            if data["needs_clarification"]:
                base["direct_reply"] = _clarification_reply(data)
                return base
            base["allowed_tools"] = ["query_logistics"]
            base["authorized_order_no"] = current_order_no
        elif reference == "latest":
            if data["needs_clarification"]:
                base["direct_reply"] = _clarification_reply(data)
                return base
            # 最新订单仍由本人近期订单查询结果中的第一条确定。
            base["allowed_tools"] = ["query_order"]
            base["order_lookup_pending"] = True
            base["order_lookup_mode"] = "latest"
        else:
            base.update(
                {
                    "allowed_tools": ["query_order"],
                    "order_lookup_pending": True,
                    "order_lookup_mode": "logistics_list",
                }
            )
        return base

    # 固定意图集合以外的值不应发生；保留关闭式兜底，避免默认放行任何工具。
    base["direct_reply"] = _FALLBACK_REPLY
    return base
