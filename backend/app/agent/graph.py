import json
import logging
import math
import re
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from app.agent.intent import (
    MULTI_REQUEST_PROMPT,
    IntentClassification,
    SupportRequest,
    SupportRequests,
    SemanticExtraction,
)
from app.agent.state import SupportState
from app.agent.service_intent import ServiceExtraction
from app.config.rag import rag_settings
from app.config.settings import settings
from app.persistence.mysql.chat import finish_assistant_message, recent_messages
from app.services.tools.registry import SUPPORT_TOOLS_BY_NAME


logger = logging.getLogger(__name__)

# 工具注册和意图权限由服务端静态定义；模型不能提交工具名或用户身份。
_TOOLS_BY_NAME = SUPPORT_TOOLS_BY_NAME
_TOOL_NODE_BY_NAME = {name: f"run_{name}" for name in _TOOLS_BY_NAME}
_SERVICE_TOOL_BY_GOAL = {
    "policy": "search_faq",
    "after_sale_status": "query_after_sale",
    "ticket_status": "query_ticket",
    "create_ticket": "create_ticket",
}
_MIN_INTENT_CONFIDENCE = 0.55
_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号、尾号或列表序号选择要查询的订单："
_REFUND_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号、尾号或列表序号选择要办理退款处理的订单，并说明退款原因："
_LEGACY_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号或序号选择要查询的订单："
_ORDER_LIST_ENTRY_PATTERN = re.compile(r"([1-9]\d*)\. (.+)")
_ORDER_LIST_PRODUCT_LIMIT = 3
_REFUND_REASON_PROMPT = "退款处理核对：订单号 {order_no}。请说明这笔订单的退款原因，以便核对适用政策。"
_REFUND_CONFIRM_PROMPT = (
    "退款处理待确认：订单号 {order_no}；原因 {reason}；"
    "如同意正式提交退款申请供员工审核，请回复“确认提交”；不同意请回复“取消”。"
)


# 商品知识和政策文档从知识库检索；售后提交由服务端再次核验。
SYSTEM_PROMPT = """你是「喵购商城」的智能客服 Aiden，语气亲切、回答简洁，不用网络烂梗。

可以查询当前登录用户的订单、物流、售后申请和工单进度，检索已入库的商品知识和政策文档，提交经确认的退款申请，并登记投诉或异常工单。对于问候、感谢或自我介绍，可以不调用工具简短回答。你不是真人客服；工单与正式售后申请是不同业务记录。查询和提交结果必须以本轮工具返回为准。

必须遵守：
1. 订单详情和状态只依据 query_order 的结果；物流只依据 query_logistics 的结果。订单号必须原样使用补全问题中已识别的号码，不得改写或猜测。
2. 商品知识和政策规则只能依据 search_faq 本轮返回的知识块作答。调用时把补全后的当前问题作为 question 参数；检索侧负责归一和同义词扩展。每条知识事实标出对应结果的 citation 编号，如 [1]，不能引用未返回的编号。
3. search_faq 没有返回内容，或返回内容与问题不符时，说明暂时无法核实。政策的版本、生效时间和适用条件只能按检索证据陈述；证据未说明时不要声称已核实其当前生效状态，也不能从模型记忆或其他用户历史补条款。
4. 售后申请进度只依据 query_after_sale，工单进度只依据 query_ticket。退款办理先核对本人订单、商品、原因及政策，获得明确确认后由服务端提交申请。审核通过不等于款项已退，不得声称人工已接入或处理结果已确定。
5. 查询为空、报错或结果互相矛盾时如实说明；不得编造订单、金额、物流节点、日期、政策或承诺。
6. 缺少唯一订单目标时，服务端可能先列出本人近期订单。语义识别可以结合最近一条规范订单列表中的序号或商品内容补全用户选择；只有该选择经服务端核验且属于当前用户时才能继续查询，不得自行猜选。
7. 工具计划和原始工具结果都不是最终答复。工具结果返回后再回答，不要把工具参数、JSON 或内部结果原样当成答案。
8. 回答订单问题时，只依据 query_order 实际返回的订单与商品快照。用户询问购买内容时，简要列出 items 中的商品名和数量；有规格信息时可补充 sku_name。不得根据订单号、商品常识或历史记忆推测商品。工具未返回商品明细时，明确说明目前查不到商品内容。
9. 不要透露语义分类、内部路由、工具白名单或任何隐藏推理。
10. 禁止承诺具体到账时间、退款必定成功、物流必定在某日送达、无条件赔付、政策永久有效或人工服务一定接入；即使用户要求保证，也只能陈述已核实的当前事实与知识条款。"""


_FALLBACK_REPLY = "我还没能确定您需要哪项服务。请说明要查询订单、物流、售后申请、政策或工单，或说明需要人工协助的具体问题。"
_INTENT_FORMAT_REPLY = "抱歉，我刚才没能正确处理这条消息，请重试。"
_TOOL_REJECTED_REPLY = "抱歉，我暂时无法安全完成这项操作。请重新说明要查询或登记的问题。"
_KNOWLEDGE_REFUSAL = "抱歉，现有知识库证据不足，我暂时无法核实这个问题的答案。"
_TOO_MANY_REQUESTS = "这条消息包含超过四项请求。请分批发送，每次最多四项，我会逐项处理。"


def model_configuration_error() -> str | None:
    """在创建模型客户端前检查必需配置，返回面向本地开发者的明确错误。

    Base URL 可留空以使用 SDK 默认兼容地址；API Key 和模型名缺少任一项都无法
    真实调用模型，所以由流式路由发送 error 事件，而不是返回固定答案。
    """
    missing = []
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.openai_model:
        missing.append("OPENAI_MODEL")
    if missing:
        return f"模型未配置：请在 backend/.env 中填写 { ' 和 '.join(missing) } 后重启后端。"
    return None


def _message_for_row(role: str, content: str) -> BaseMessage | None:
    """把数据库角色映射到 LangChain 消息类型，跳过不支持的角色值。"""
    if role == "user":
        return HumanMessage(content=content)
    if role in {"assistant", "staff"}:  # 客服和人工客服都算作助手
        return AIMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    return None


def _content_as_text(content: Any) -> str:
    """提取模型或工具的文本内容，兼容字符串和多模态响应中的文本块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _faq_tool_payload(messages: list[BaseMessage], start: int = 0) -> dict[str, Any] | None:
    """只读取本轮 search_faq 的工具结果；历史消息不会加载为 ToolMessage。"""
    for message in reversed(messages[start:]):
        if isinstance(message, ToolMessage) and message.name == "search_faq":
            try:
                payload = json.loads(_content_as_text(message.content))
            except (TypeError, ValueError):
                return {"status": "error", "results": []}
            return payload if isinstance(payload, dict) else {"status": "error", "results": []}
    return None


def _citation_rows(results: list[dict[str, Any]]) -> list[dict[str, object]]:
    """把工具结果收窄为可持久化、可在历史消息中展示的来源快照。"""
    return [
        {
            "number": index,
            "chunkId": str(item["chunkId"]),
            "sectionPath": str(item.get("sectionPath") or ""),
            "content": str(item.get("answer") or ""),
            "sourcePath": item.get("sourcePath"),
            "sourceUrl": item.get("sourceUrl"),
        }
        for index, item in enumerate(results, 1)
        if item.get("chunkId")
    ]


def _edge_ordered_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最高相关放开头，次高相关放结尾，降低长上下文中段的信息损失。"""
    if len(results) < 3:
        return results
    return [results[0], *results[2:], results[1]]


def _latest_user_text(messages: list[BaseMessage]) -> str:
    """取本轮原始用户消息；解析失败时仅用于安全兜底，不写回对话历史。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _content_as_text(message.content).strip()
    return ""


def _contains_exact_identifier(text: str, identifier: str) -> bool:
    """核对完整业务编号的原文来源，避免把更长编号的子串当作来源。"""
    if not text or not identifier:
        return False
    def is_identifier_char(char: str) -> bool:
        return char.isascii() and (char.isalnum() or char in "_-")

    offset = 0
    while (start := text.find(identifier, offset)) != -1:
        end = start + len(identifier)
        if (
            (start == 0 or not is_identifier_char(text[start - 1]))
            and (end == len(text) or not is_identifier_char(text[end]))
        ):
            return True
        offset = start + 1
    return False


def _valid_order_no(value: object) -> bool:
    """退款跨轮状态沿用订单列表的长度及控制字符边界。"""
    return (isinstance(value, str) and 0 < len(value) <= 64
            and value.strip() == value
            and not any(ord(char) < 32 or char in "\u2028\u2029" for char in value))


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


def _verified_refund_reason(request: SupportRequest, messages: list[BaseMessage]) -> str:
    """原因必须逐字来自本轮用户，规范化后才能放入固定确认话术。"""
    quote = (request.refund_reason_quote or "").strip()
    if not quote or quote not in _latest_user_text(messages):
        return ""
    return " ".join(quote.replace("；", "，").split())[:160]


def _orders_matching_suffix(order_nos: list[str], suffix: str) -> list[str]:
    """按不区分大小写的编号末尾片段筛选候选，不从助手文字生成编号。"""
    normalized_suffix = suffix.casefold()
    return [
        order_no
        for order_no in order_nos
        if order_no.casefold().endswith(normalized_suffix)
    ]


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


def _refund_order_result(state: SupportState) -> dict[str, Any]:
    """订单必须由本轮本人查询证实；原因齐备后才检索适用政策。"""
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
    if not _valid_order_no(requested) or matched is None:
        return {"allowed_tools": [], "refund_phase": "", "direct_reply": "当前账号下没有核对到这笔订单，请提供本人订单号或从近期订单中选择。"}
    if len(matched.get("items") or []) != 1:
        return {"allowed_tools": [], "refund_phase": "", "direct_reply": "这笔订单包含多件商品或缺少商品明细。请打开“售后与工单”页面，选择具体商品并核对政策后提交申请。"}
    reason = state.get("refund_reason", "")
    if not reason:
        return {"allowed_tools": [], "refund_phase": "", "refund_order_no": requested,
                "direct_reply": _REFUND_REASON_PROMPT.format(order_no=requested)}
    return {
        "allowed_tools": ["search_faq"], "refund_phase": "policy",
        "refund_order_no": requested,
        "completed_question": (
            f"订单状态：{matched.get('status') or '未知'}；下单时间：{matched.get('ordered_at') or '未知'}；"
            f"退款原因：{reason}。请核对适用的退款政策、条件和所需信息。"
        ),
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

def _route_refund_request(
    request: SupportRequest, messages: list[BaseMessage],
    same_turn_order_options: list[str] | None, context: dict[str, str],
) -> dict[str, Any]:
    """退款办理先核单和政策；只有上一轮已给出固定确认请求才可能写入。"""
    if request.application_type in {"return", "exchange"}:
        return {"allowed_tools": [], "direct_reply": "请打开“售后与工单”页面，选择本人订单和具体商品，核对政策并提交正式退货或换货申请。",
                "refund_phase": "", "refund_authorized": False}
    if request.application_type != "refund" and context.get("stage") not in {"reason", "confirm"}:
        return {"allowed_tools": [], "direct_reply": "请说明要申请退款、退货还是换货；也可以在“售后与工单”页面选择申请类型。",
                "refund_phase": "", "refund_authorized": False}
    if request.refund_consent == "decline" and context.get("stage") == "confirm":
        return {"allowed_tools": [], "direct_reply": "好的，我不会提交这项退款申请。",
                "refund_phase": "", "refund_authorized": False}
    if request.refund_consent == "agree":
        quote = (request.action_quote or "").strip()
        if context.get("stage") == "confirm" and quote and quote in _latest_user_text(messages):
            order_no, reason = context["order_no"], context["reason"]
            return {
                "allowed_tools": ["submit_after_sale"], "direct_reply": "",
                "refund_phase": "confirm", "refund_order_no": order_no,
                "refund_reason": reason, "refund_authorized": True,
                "semantic_result": {"classification": {"intent": "refund_return"}},
            }
        return {"allowed_tools": [], "direct_reply": "请先确定订单和退款原因，待我核对政策后再确认是否提交申请。",
                "refund_phase": "", "refund_authorized": False}

    reason = _verified_refund_reason(request, messages)
    if context.get("stage") == "reason":
        order_no = context["order_no"]
        return {
            "allowed_tools": ["query_order"], "direct_reply": "",
            "refund_phase": "order", "refund_order_no": order_no,
            "refund_reason": reason, "refund_authorized": False,
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
                   "refund_authorized": False, "refund_order_no": ""})
    if routed.get("order_lookup_mode") == "order_list":
        routed["order_lookup_mode"] = "refund_list"
    elif request.entities.order_reference == "latest" and routed.get("allowed_tools") == ["query_order"]:
        routed["order_lookup_pending"] = True
        routed["order_lookup_mode"] = "refund_latest"
    if routed.get("allowed_tools") != ["query_order"]:
        routed["refund_phase"] = ""
    return routed

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


def build_support_graph():
    """构建显式识别、服务端路由、白名单工具调用和最终回答保存流程。"""
    kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.2,
        "streaming": True,
        # 兼容接口的流式响应需要显式请求 usage，回调才能拿到准确的 token 消耗。
        "stream_usage": settings.langfuse_enabled,
        "max_tokens": 800,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    # DeepSeek 等思考型模型下，带 tools 的请求必须回传历史 reasoning_content，而本图的工具调用
    # 是服务端合成的、不带该字段，会让“工具结果后再生成回答”这一步被服务端拒绝（400）。
    # OPENAI_THINKING_MODE=disabled 可关闭思考模式绕开该约束；未配置时不传此参数，
    # 保持服务端默认行为。详见 app.config.settings 中的说明。
    thinking_body = settings.openai_thinking_body
    if thinking_body:
        kwargs["extra_body"] = thinking_body
    model = ChatOpenAI(**kwargs)
    # 结构化识别不向客户端逐字输出；非流式 JSON 请求降低兼容模型只返回空白片段的概率。
    json_model = ChatOpenAI(**{**kwargs, "streaming": False, "stream_usage": False}).bind(
        response_format={"type": "json_object"}
    )
    # 分开的 ToolNode 让一次通过校验的调用只能到达对应工具。
    tool_nodes = {
        name: ToolNode([tool], handle_tool_errors=False)
        for name, tool in _TOOLS_BY_NAME.items()
    }

    async def load_context(state: SupportState) -> dict[str, Any]:
        """加载归属当前用户的有限历史，并在模型 await 前关闭 MySQL Session。"""
        rows = recent_messages(
            state["conversation_id"], state["user_id"], state["assistant_message_id"]
        )
        history = [
            msg
            for row in rows
            if (msg := _message_for_row(row["role"], row["content"])) is not None
        ]
        context: dict[str, str] = {}
        if len(rows) >= 2 and rows[-1]["role"] == "user" and rows[-2]["role"] == "assistant":
            workflow = rows[-2].get("workflow_state")
            candidate = workflow.get("refund") if isinstance(workflow, dict) else None
            if isinstance(candidate, dict) and candidate.get("stage") in {"reason", "confirm"}:
                order_no = candidate.get("order_no")
                reason = candidate.get("reason", "")
                if (_valid_order_no(order_no)
                        and isinstance(reason, str) and len(reason) <= 160
                        and (candidate["stage"] != "confirm" or reason)):
                    context = {"stage": candidate["stage"], "order_no": order_no,
                               "reason": reason}
        return {"messages": [SystemMessage(content=SYSTEM_PROMPT), *history],
                "refund_context": context}

    async def recognize_intent(state: SupportState) -> dict[str, Any]:
        """先按本轮原话识别；格式异常重试一次，指代不明时再借助历史。"""
        current_message = HumanMessage(content=_latest_user_text(state["messages"]))
        current_only = _structured_messages([current_message], MULTI_REQUEST_PROMPT, SupportRequests)
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
        return routed

    async def dispatch_tool_call(state: SupportState) -> dict[str, Any]:
        """由服务端构造已批准的工具调用，避免模型漏调工具或改写订单参数。"""
        allowed_names = state.get("allowed_tools", [])
        if len(allowed_names) != 1 or allowed_names[0] not in _TOOLS_BY_NAME:
            return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}

        name = allowed_names[0]
        args: dict[str, Any] = {}
        semantic = state.get("semantic_result", {})
        entities = semantic.get("entities", {}) if isinstance(semantic, dict) else {}
        service = semantic.get("service", {}) if isinstance(semantic, dict) else {}
        classification = semantic.get("classification", {}) if isinstance(semantic, dict) else {}

        if name == "search_faq":
            args["question"] = state.get("completed_question", "").strip()
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
            args["order_no"] = state["refund_order_no"]
            args["reason"] = state["refund_reason"]
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

    async def assess_knowledge(state: SupportState) -> dict[str, Any]:
        """生成前让模型核对召回证据是否足够，失败问法进入问题池。"""
        payload = _faq_tool_payload(state.get("messages", []), state.get("request_tool_start", 0))
        if payload is None:
            return {}
        original = (
            state["requests"][state["request_index"]]["completed_question"].strip()
            or _latest_user_text(state.get("messages", []))
        )

        def refuse(entrypoint: str, reason: str) -> dict[str, Any]:
            return {
                "direct_reply": _KNOWLEDGE_REFUSAL,
                "citations": [],
                "request_low_confidence": {
                    "original_question": original,
                    "entrypoint": entrypoint,
                    "reason": reason,
                },
            }

        if payload.get("status") != "ok":
            return {"direct_reply": "抱歉，知识检索暂时不可用，请稍后重试。", "citations": []}
        results = [item for item in payload.get("results", []) if isinstance(item, dict)]
        if not results:
            return refuse("retrieval_low_confidence", "知识库没有召回可用的证据")
        # 非重排检索的 score 为 None；只对可用的 rerank 分数应用该阈值。
        scores = [
            item["score"] for item in results
            if isinstance(item.get("score"), (int, float))
            and not isinstance(item["score"], bool)
            and math.isfinite(item["score"])
        ]
        highest_score = max(scores) if scores else None
        if highest_score is not None and highest_score < rag_settings.online_min_rerank_score:
            return refuse(
                "retrieval_low_confidence",
                f"最高重排分数 {highest_score:.3f} 低于阈值 {rag_settings.online_min_rerank_score:.2f}",
            )

        question = original
        check_messages = [
            SystemMessage(content=(
                "你是知识证据充分性检查器。只判断给定证据能否直接回答用户问题，不能使用常识或模型记忆补足。"
                "型号、时间、金额、条件不一致时必须判不能。只输出合法 JSON，"
                '包含布尔字段 "sufficient" 和简短原因字段 "reason"。'
            )),
            HumanMessage(content=json.dumps({"question": question, "evidence": results}, ensure_ascii=False)),
        ]
        try:
            cost_intent = state["requests"][state["request_index"]]["intent"]
            check = await model.ainvoke(
                check_messages,
                config=(
                    {
                        "metadata": {"cost_intent": cost_intent},
                        "run_name": f"cost_intent_{cost_intent}",
                    }
                    if settings.langfuse_enabled else None
                ),
            )
            raw = _content_as_text(check.content).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
            verdict = json.loads(raw)
            if not isinstance(verdict, dict) or verdict.get("sufficient") is not True:
                reason = str(verdict.get("reason") or "模型判断证据不足") if isinstance(verdict, dict) else "模型判断证据不足"
                return refuse("generation_insufficient_knowledge", reason)
        except (TypeError, ValueError):
            return refuse("generation_insufficient_knowledge", "证据充分性自评结果无法解析")
        return {"citations": _citation_rows(results)}

    async def generate(state: SupportState) -> dict[str, Any]:
        """逐项生成并组合答复，单项失败不影响其他项；引用按全轮重新编号。"""
        direct_reply = state.get("direct_reply", "").strip()
        items = state.get("request_results", [])
        if direct_reply and not items:
            return {"messages": [AIMessage(content=direct_reply)], "answer": direct_reply}
        answers: list[str] = []
        all_citations: list[dict[str, object]] = []
        low_confidence: list[dict[str, object]] = []
        refund_pending: dict[str, str] = {}
        retrieval_snapshots: list[dict[str, object]] = []
        for item in items:
            snapshot = item["retrieval"]
            retrieval_snapshots.append(snapshot)
            if item.get("low_confidence"):
                low_confidence.append({**item["low_confidence"], **snapshot})
            reply = item.get("direct_reply", "")
            tools = item.get("tools", [])
            if not reply and tools:
                latest = tools[-1]
                payload = latest["payload"]
                if payload.get("status") != "ok":
                    reply = str(payload.get("message") or ("申请提交暂时失败，请稍后重试。" if latest["name"] == "submit_after_sale" else "查询暂时失败，请稍后重试。"))
                elif latest["name"] == "submit_after_sale":
                    prefix = "已有待处理申请" if payload.get("already_exists") else "已提交退款申请"
                    reply = f"{prefix}，申请号：{payload['request_no']}。当前待审核；审核通过不代表款项已退，后续进度可查询申请号。"
                elif latest["name"] == "search_faq" and not item.get("citations"):
                    reply = _KNOWLEDGE_REFUSAL
                elif latest["name"] == "create_ticket" and not payload.get("ticket_no"):
                    reply = "工单登记未返回工单号，请稍后核实。"
                elif latest["name"] == "create_ticket":
                    prefix = "已有待处理工单" if payload.get("already_exists") else "已登记待处理工单"
                    reply = f"{prefix}，工单号：{payload['ticket_no']}。可查询工单进度。"
                elif latest["name"] == "query_ticket" and payload.get("tickets"):
                    labels = {"open": "待接单", "in_progress": "处理中", "resolved": "已解决，待关闭", "closed": "已关闭"}
                    reply = "\n".join(
                        f"工单 {row['ticket_no']}：当前状态为{labels.get(row['status'], row['status'])}。"
                        for row in payload["tickets"] if isinstance(row, dict)
                        and isinstance(row.get("ticket_no"), str)
                        and isinstance(row.get("status"), str)
                    )
                elif payload.get("found") is False or (
                    latest["name"] == "search_faq" and not payload.get("results")
                ):
                    reply = str(payload.get("message") or "当前没有查到这项请求的结果。")
            if not reply and item.get("goal") == "smalltalk":
                # 闲聊没有工具证据；沿用近期对话语境，但只回答当前这一项。
                conversation = [
                    message for message in state.get("messages", [])
                    if isinstance(message, (HumanMessage, AIMessage))
                    and not (isinstance(message, AIMessage) and message.tool_calls)
                ]
                if conversation and isinstance(conversation[-1], HumanMessage):
                    conversation.pop()  # 当前完整用户消息由下方的单项问题替代。
                response = await model.ainvoke([
                    SystemMessage(content=(
                        SYSTEM_PROMPT + "\n当前只回答一项闲聊。自然、简短地回应问候、感谢、"
                        "自我介绍或能力范围问题；无需工具证据，不要声称查到了订单、"
                        "政策或其他业务结果，也不要编造未接入的能力。"
                    )),
                    *conversation[-6:],
                    HumanMessage(content=json.dumps({
                        "question": item["question"], "evidence": [],
                    }, ensure_ascii=False)),
                ], config=(
                    {
                        "metadata": {"cost_intent": item["intent"]},
                        "run_name": f"cost_intent_{item['intent']}",
                    }
                    if settings.langfuse_enabled else None
                ))
                reply = _content_as_text(response.content).strip()
            if not reply:
                evidence = []
                local_citations = item.get("citations", [])
                offset = max((int(row["number"]) for row in all_citations), default=0)
                for tool_result in tools:
                    payload = dict(tool_result["payload"])
                    if tool_result["name"] == "search_faq":
                        results = [dict(row) for row in payload.get("results", []) if isinstance(row, dict)]
                        for index, row in enumerate(results, 1):
                            row["citation"] = f"[{offset + index}]"
                        payload["results"] = _edge_ordered_results(results)
                    evidence.append({"name": tool_result["name"], "result": payload})
                refund_policy = item.get("goal") == "create_ticket" and item.get("refund_phase") == "policy"
                response = await model.ainvoke([
                    SystemMessage(content=SYSTEM_PROMPT + "\n只回答这一项请求。必须以给出的本轮结果为准，简短说明查到的事实；不要输出 JSON 或工具名。商品知识和政策规则的每条事实必须引用给出的编号。"
                                  + ("\n这是退款申请提交前说明：指出已核对的订单事实、政策证据能确认的条件、仍缺少或无法判断的信息；提交后待员工审核，不得说退款资格、金额或时效已经确定，也不要自行要求用户确认。" if refund_policy else "")),
                    HumanMessage(content=json.dumps({"question": item["question"], "evidence": evidence}, ensure_ascii=False)),
                ], config=(
                    {
                        "metadata": {"cost_intent": item["intent"]},
                        "run_name": f"cost_intent_{item['intent']}",
                    }
                    if settings.langfuse_enabled else None
                ))
                reply = _content_as_text(response.content).strip()
                if local_citations:
                    used_numbers = {int(value) for value in re.findall(r"\[(\d+)\]", reply)}
                    permitted = {offset + int(row["number"]) for row in local_citations}
                    if not used_numbers or not used_numbers <= permitted:
                        reply = _KNOWLEDGE_REFUSAL
                        low_confidence.append({
                            "original_question": item["question"].strip(),
                            "entrypoint": "generation_missing_citation",
                            "reason": "生成答案没有引用召回的知识块",
                            **snapshot,
                        })
                    else:
                        all_citations.extend(
                            {**row, "number": offset + int(row["number"])}
                            for row in local_citations if offset + int(row["number"]) in used_numbers
                        )
                if refund_policy and reply != _KNOWLEDGE_REFUSAL and local_citations:
                    reply += "\n" + _REFUND_CONFIRM_PROMPT.format(
                        order_no=item["refund_order_no"], reason=item["refund_reason"],
                    )
                    refund_pending = {"stage": "confirm", "order_no": item["refund_order_no"],
                                      "reason": item["refund_reason"]}
            if (item.get("goal") == "create_ticket" and item.get("refund_order_no")
                    and reply == _REFUND_REASON_PROMPT.format(order_no=item["refund_order_no"])):
                refund_pending = {"stage": "reason", "order_no": item["refund_order_no"],
                                  "reason": ""}
            answers.append(reply or _FALLBACK_REPLY)
        answer = answers[0] if len(answers) == 1 else "\n".join(
            f"【{index}】 {reply}" for index, reply in enumerate(answers, 1)
        )
        output: dict[str, Any] = {"messages": [AIMessage(content=answer)], "answer": answer, "citations": all_citations,
                                  "handoff_required": any(item.get("handoff") for item in items),
                                  "refund_pending": refund_pending}
        output["low_confidence"] = low_confidence
        output["retrieval_snapshots"] = retrieval_snapshots
        return output

    def route_after_intent(state: SupportState) -> str:
        """没有可执行工具的项也进入收尾，保留它在最终答复中的位置。"""
        return "dispatch_tool_call" if state.get("allowed_tools") else "finish_request"

    def route_dispatched_tool(state: SupportState) -> str:
        """只把服务端构造且位于静态映射中的工具调用交给对应 ToolNode。"""
        if state.get("cached_tool_hit"):
            return "after_tools"
        allowed_names = state.get("allowed_tools", [])
        if len(allowed_names) != 1:
            return "finish_request"
        return _TOOL_NODE_BY_NAME.get(allowed_names[0], "finish_request")

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
        # 查询工具一旦执行完毕就收回白名单，防止模型在同一意图下重复或扩展查询。
        return {**updates, "allowed_tools": [], "order_lookup_pending": False}

    def route_after_tools(state: SupportState) -> str:
        """列表选择核验可继续查询；其余结果先做知识校验再收尾当前项。"""
        if state.get("allowed_tools"):
            return "dispatch_tool_call"
        return "assess_knowledge" if _faq_tool_payload(
            state.get("messages", []), state.get("request_tool_start", 0)
        ) else "finish_request"

    def finish_request(state: SupportState) -> dict[str, Any]:
        """冻结当前项的事实与澄清结果，随后继续下一项。"""
        index = state.get("request_index", 0)
        requests = state.get("requests", [])
        if index >= len(requests):
            return {}
        tool_results = []
        for message in state.get("messages", [])[state.get("request_tool_start", 0):]:
            if not isinstance(message, ToolMessage):
                continue
            try:
                payload = json.loads(_content_as_text(message.content))
            except (TypeError, ValueError):
                payload = {"status": "error"}
            tool_results.append({
                "name": message.name,
                "payload": payload if isinstance(payload, dict) else {"status": "error"},
            })
        faq_payload = next((
            tool["payload"] for tool in reversed(tool_results) if tool["name"] == "search_faq"
        ), None)
        source_text = _latest_user_text(state.get("messages", []))
        quote = requests[index].get("original_question_quote")
        raw_question = (quote.strip() if isinstance(quote, str) and quote.strip()
                        and quote.strip() in source_text else
                        source_text if len(requests) == 1 else requests[index]["completed_question"].strip())
        retrieval = {
            "original_question": raw_question,
            "retrieval_query": requests[index]["completed_question"].strip()[:500] if faq_payload is not None else None,
            "retrieval_status": ("not_searched" if faq_payload is None else
                                 "searched" if faq_payload.get("status") == "ok" else "error"),
            "retrieval_snapshot": [
                {"rank": rank, "chunk_id": str(row.get("chunkId") or ""),
                 "content": str(row.get("answer") or ""),
                 "source": row.get("sourcePath") or row.get("sourceUrl") or "FAQ",
                 "section": str(row.get("sectionPath") or ""),
                 "score": row.get("score") if isinstance(row.get("score"), (int, float))
                 and not isinstance(row.get("score"), bool) and math.isfinite(row["score"]) else None}
                for rank, row in enumerate(faq_payload.get("results", []), 1)
                if isinstance(row, dict)
            ] if faq_payload is not None else [],
        }
        result = {
            "question": requests[index]["completed_question"],
            "intent": requests[index]["intent"],
            "goal": requests[index]["goal"],
            "direct_reply": state.get("direct_reply", ""),
            "tools": tool_results,
            "retrieval": retrieval,
            "citations": state.get("citations", []),
            "handoff": state.get("handoff_requested", False),
            "low_confidence": state.get("request_low_confidence"),
            "refund_phase": state.get("refund_phase", ""),
            "refund_order_no": state.get("refund_order_no", ""),
            "refund_reason": state.get("refund_reason", ""),
        }
        return {
            "request_results": [*state.get("request_results", []), result],
            "request_index": index + 1,
            "allowed_tools": [],
            "direct_reply": "",
            "citations": [],
            "request_low_confidence": None,
            "handoff_requested": False,
            "refund_authorized": False,
            "refund_phase": "",
        }

    def route_next_request(state: SupportState) -> str:
        return "route_intent" if state.get("request_index", 0) < len(state.get("requests", [])) else "generate"

    def save_answer(state: SupportState) -> dict[str, Any]:
        """模型完成工具循环或澄清/兜底后，仅保存 generate 产出的最终回答。"""
        answer = state["answer"]
        if not answer:
            raise RuntimeError("模型没有生成可显示的回答。")
        handoff_committed = finish_assistant_message(
            state["assistant_message_id"],
            state["conversation_id"],
            state["user_id"],
            answer,
            "complete",
            citations=state.get("citations", []),
            retrieval_snapshots=state.get("retrieval_snapshots", []),
            low_confidence=state.get("low_confidence", []),
            handoff=state.get("handoff_required", False),
            workflow_state=({"refund": state["refund_pending"]}
                            if state.get("refund_pending") else None),
        )
        return {"answer": answer, "citations": state.get("citations", []), "handoff_committed": handoff_committed}

    graph = StateGraph(SupportState)
    graph.add_node("load_context", load_context)
    graph.add_node("recognize_intent", recognize_intent)
    graph.add_node("route_intent", route_intent)
    graph.add_node("dispatch_tool_call", dispatch_tool_call)
    graph.add_node("generate", generate)
    graph.add_node("assess_knowledge", assess_knowledge)
    graph.add_node("after_tools", after_tools)
    graph.add_node("finish_request", finish_request)
    graph.add_node("save_answer", save_answer)
    for tool_name, node_name in _TOOL_NODE_BY_NAME.items():
        graph.add_node(node_name, tool_nodes[tool_name])

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "recognize_intent")
    graph.add_edge("recognize_intent", "route_intent")
    graph.add_conditional_edges(
        "route_intent",
        route_after_intent,
        {"dispatch_tool_call": "dispatch_tool_call", "finish_request": "finish_request"},
    )
    graph.add_conditional_edges(
        "dispatch_tool_call",
        route_dispatched_tool,
        {
            **{node_name: node_name for node_name in _TOOL_NODE_BY_NAME.values()},
            "after_tools": "after_tools",
            "finish_request": "finish_request",
        },
    )
    for node_name in _TOOL_NODE_BY_NAME.values():
        graph.add_edge(node_name, "after_tools")
    graph.add_conditional_edges(
        "after_tools",
        route_after_tools,
        {"dispatch_tool_call": "dispatch_tool_call", "assess_knowledge": "assess_knowledge", "finish_request": "finish_request"},
    )
    graph.add_edge("assess_knowledge", "finish_request")
    graph.add_conditional_edges("finish_request", route_next_request, {
        "route_intent": "route_intent", "generate": "generate",
    })
    graph.add_edge("generate", "save_answer")
    graph.add_edge("save_answer", END)
    compiled = graph.compile(name="aiden_support")
    # 图在每条新消息中构建一次；在编译出口绑定一个回调，让节点、模型和工具
    # 共享同一条 trace，而不必在每个模型调用或 ToolNode 上重复传 callbacks。
    if settings.langfuse_enabled:
        from langfuse.langchain import CallbackHandler

        return compiled.with_config({"callbacks": [CallbackHandler(update_trace=True)]})
    return compiled
