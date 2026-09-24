import json
import re
from typing import Any

from langchain_core.exceptions import OutputParserException
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

from app.agent.intent import INTENT_RECOGNITION_PROMPT, IntentRecognition
from app.agent.state import SupportState
from app.config.settings import settings
from app.persistence.mysql.chat import finish_assistant_message, recent_messages
from app.services.tools.customer import query_logistics, query_order, search_faq


# 每个意图只接入当前确实存在的只读工具。这个映射完全由服务端定义，绝不从模型
# 返回的名字创建 ToolNode，也不把用户身份放进模型可填写的参数。
_TOOLS_BY_NAME = {
    query_order.name: query_order,
    query_logistics.name: query_logistics,
    search_faq.name: search_faq,
}
_TOOL_NODE_BY_NAME = {
    "query_order": "run_query_order",
    "query_logistics": "run_query_logistics",
    "search_faq": "run_search_faq",
}
_MIN_INTENT_CONFIDENCE = 0.55
_ORDER_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]{1,62}[A-Za-z0-9])(?![A-Za-z0-9_-])"
)
_ORDER_MARKER_PATTERN = re.compile(
    r"(?:订单(?:号|编号)?|单号|order(?:\s*(?:no\.?|number))?)\s*(?:是|为|[:：#])?\s*"
    r"([A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?)",
    re.IGNORECASE,
)
_SHORT_NUMERIC_CHOICE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:还是|或者|或是|或|和|与|及|、|/|[,，]|and\b|or\b)\s*"
    r"(\d{1,2})(?!\d)",
    re.IGNORECASE,
)
_ORDER_SUFFIX_PATTERN = re.compile(r"\s*(?:号(?:订单|物流|包裹)?|订单号|单号|运单号)")
_CALENDAR_DATE_PATTERN = re.compile(r"^(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?$")
_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号或序号选择要查询的订单："
_ORDER_LIST_ENTRY_PATTERN = re.compile(r"([1-9]\d*)\. (.+)")
_ORDER_SELECTION_ORDINAL_PATTERNS = (
    re.compile(r"第\s*([1-5一二三四五])\s*(?:个|笔|单)?"),
    re.compile(r"(?:我选|选择|选|序号)\s*[:：]?\s*([1-5一二三四五])\s*(?:个|笔|单)?"),
)
_CHINESE_ORDINALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_EXPLICIT_LATEST_PATTERN = re.compile(
    r"(?:最近\s*(?:的\s*)?(?:一笔|一单|一个订单)|"
    r"最新\s*(?:的\s*)?(?:一笔|一单|一个订单|订单)|"
    r"最后\s*(?:的\s*)?(?:一笔|一单))"
)


# FAQ 是当前唯一可用的常见问题依据；政策表、售后进度和工单没有接入本 Agent。
SYSTEM_PROMPT = """你是「喵购商城」的智能客服 Aiden，语气亲切、回答简洁，不用网络烂梗。

当前能力仅包括查询当前登录用户的订单及商品快照、查询其订单物流，以及检索启用中的 FAQ。按本轮问题调用系统提供的工具；工具结果返回后再回答。

必须遵守：
1. 订单详情和状态只依据 query_order 的结果；物流只依据 query_logistics 的结果。订单号必须原样使用补全问题中已识别的号码，不得改写或猜测。
2. FAQ 只能依据 search_faq 实际返回的问题、答案和分类；调用时把补全后的当前问题原样作为 question 参数。没有匹配内容时明确说暂时无法核实，不得把 policies 表、模型记忆或常识当作已检索的政策。
3. 退货/退款申请进度、政策条款和工单目前没有独立查询能力；不能伪装成查过，也不能声称已转接人工。
4. 查询为空、报错或结果互相矛盾时如实说明；不得编造订单、金额、物流节点、日期、政策或承诺。
5. 缺少唯一订单目标时，服务端可能先列出本人近期订单；只能按用户明确选择且服务端授权的订单继续查询，不能从列表或工具结果自行挑选。
6. 工具计划和原始工具结果都不是最终答复。工具结果返回后再回答，不要把工具参数、JSON 或内部结果原样当成答案。
7. 不要透露语义分类、内部路由、工具白名单或任何隐藏推理。"""


_FALLBACK_REPLY = "我还没能确定您想查询的内容。您可以说明是查询订单、物流，还是咨询常见问题；查询订单或物流时请提供订单号。"
_TOOL_REJECTED_REPLY = "抱歉，我暂时无法安全完成这项查询。请重新说明要查询的订单号、物流信息或常见问题。"
_UNSUPPORTED_REPLY = "目前我可以协助查询订单、物流，并检索已接入的常见问题。售后进度、工单状态和人工转接暂不支持；政策问题只有在 FAQ 返回相关内容时才能说明。"


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


def _latest_user_text(messages: list[BaseMessage]) -> str:
    """取本轮原始用户消息；解析失败时仅用于安全兜底，不写回对话历史。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _content_as_text(message.content).strip()
    return ""


def _contains_exact_order_no(text: str, order_no: str) -> bool:
    """只认可用户原文中独立出现的订单号，避免把更长编号的子串当作来源。"""
    if not text or not order_no:
        return False
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(order_no)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


def _order_candidates(text: str) -> set[str]:
    """提取常见 ASCII 订单号形态，用于在服务端发现同一请求中的多个目标。"""
    marked_values = set(_ORDER_MARKER_PATTERN.findall(text))
    values = set(_ORDER_TOKEN_PATTERN.findall(text)) | marked_values
    candidates: set[str] = set()
    for value in values:
        if not any(char.isdigit() for char in value):
            continue
        if _CALENDAR_DATE_PATTERN.fullmatch(value):
            continue
        has_letters = any(char.isalpha() for char in value)
        # 短纯数字仅在订单标记直接指向它时作为单号候选，避免把普通数量当成订单号。
        if has_letters or "_" in value or "-" in value or len(value) >= 3 or value in marked_values:
            candidates.add(value.casefold())

    for match in _SHORT_NUMERIC_CHOICE_PATTERN.finditer(text):
        # 只在订单标记或订单/物流后缀能证明这是目标选项时，加入短数字两端。
        has_order_marker = bool(_ORDER_MARKER_PATTERN.search(text))
        has_order_suffix = bool(_ORDER_SUFFIX_PATTERN.match(text, match.end()))
        if has_order_marker or has_order_suffix:
            candidates.update(value.casefold() for value in match.groups())
    return candidates


def _order_no_source_status(order_no: str, messages: list[BaseMessage]) -> str:
    """验证号码来自用户原文，并拒绝无法唯一确定的多订单上下文。"""
    user_texts = [
        _content_as_text(message.content).strip()
        for message in messages
        if isinstance(message, HumanMessage)
    ]
    if not user_texts or not order_no or len(order_no) > 64:
        return "missing"

    current_text = user_texts[-1]
    if _contains_exact_order_no(current_text, order_no):
        # 本轮明确写出的目标以本轮原文为准；同句出现其他订单号时必须澄清。
        source_texts = [current_text]
    else:
        # 省略订单号的指代只能回溯用户自己曾写过的号码，不信任助手生成的文本。
        source_texts = user_texts
        if not any(_contains_exact_order_no(text, order_no) for text in source_texts):
            return "missing"

    candidates = set().union(*(_order_candidates(text) for text in source_texts))
    candidates.add(order_no.casefold())
    if len(candidates) > 1:
        return "ambiguous"
    return "valid"


def _parse_order_list(content: str) -> list[str] | None:
    """只接受本服务端固定格式的最近订单列表，避免把任意助手文字当成选择授权。"""
    lines = content.splitlines()
    if not lines or lines[0] != _ORDER_LIST_HEADER or len(lines) < 2:
        return None

    order_nos: list[str] = []
    for position, line in enumerate(lines[1:], start=1):
        match = _ORDER_LIST_ENTRY_PATTERN.fullmatch(line)
        if match is None or int(match.group(1)) != position:
            return None
        order_no = match.group(2)
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
    """仅把紧邻当前用户消息的规范客服列表视作可供本轮选择的候选。"""
    conversation_messages = [
        message for message in messages if not isinstance(message, SystemMessage)
    ]
    if (
        len(conversation_messages) < 2
        or not isinstance(conversation_messages[-1], HumanMessage)
        or not isinstance(conversation_messages[-2], AIMessage)
    ):
        return None
    return _parse_order_list(_content_as_text(conversation_messages[-2].content))


def _explicit_latest_in_current_message(messages: list[BaseMessage]) -> bool:
    """只保留用户本轮明确说最近一笔的原有语义，不从历史或助手文字推断。"""
    return bool(_EXPLICIT_LATEST_PATTERN.search(_latest_user_text(messages)))


def _selection_ordinal(text: str) -> set[int]:
    """读取常见中文或阿拉伯序号；没有命中时不猜测用户想选哪一行。"""
    ordinals: set[int] = set()
    for pattern in _ORDER_SELECTION_ORDINAL_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1)
            ordinal = _CHINESE_ORDINALS.get(value)
            ordinals.add(ordinal if ordinal is not None else int(value))
    bare_ordinal = re.fullmatch(r"\s*([1-5])\s*", text)
    if bare_ordinal:
        ordinals.add(int(bare_ordinal.group(1)))
    return ordinals


def _selected_order_from_list(text: str, order_nos: list[str]) -> str | None:
    """将用户原文中的订单号或序号绑定到上一条列表中的唯一真实候选。"""
    ordinals = _selection_ordinal(text)
    ordinal_targets = {
        order_nos[ordinal - 1]
        for ordinal in ordinals
        if 1 <= ordinal <= len(order_nos)
    }
    has_order_marker = bool(_ORDER_MARKER_PATTERN.search(text))
    if ordinals and not has_order_marker:
        if len(ordinals) != 1:
            return None
        if len(ordinal_targets) != 1:
            return None
        ordinal_target = next(iter(ordinal_targets))
        explicit_id_targets = {
            order_no
            for order_no in order_nos
            if _contains_exact_order_no(text, order_no)
        }
        if explicit_id_targets and explicit_id_targets != {ordinal_target}:
            return None
        return ordinal_target

    matched_order_nos = {
        order_no
        for order_no in order_nos
        if _contains_exact_order_no(text, order_no)
    }
    for marked_order_no in _ORDER_MARKER_PATTERN.findall(text):
        matched_order_nos.update(
            order_no
            for order_no in order_nos
            if marked_order_no.casefold() == order_no.casefold()
        )
    selected = ordinal_targets | matched_order_nos
    return next(iter(selected)) if len(selected) == 1 else None


def _clarification_reply(result: dict[str, Any]) -> str:
    """使用受控追问模板，避免分类器把凭据或无关要求变成用户可见问题。"""
    intent = result.get("intent")

    if intent == "order_query":
        return "请告诉我订单号，或说明您想查看最近几笔订单。"
    if intent == "logistics_query":
        return "请提供要查询物流的订单号；如果您指最近一笔订单，也可以明确说明。"
    if intent == "faq":
        # FAQ 澄清只接受短问题，并屏蔽凭据类要求；其余情况使用固定追问。
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
        return "请再具体描述您想咨询的问题，我会根据已接入的常见问题检索。"
    return _FALLBACK_REPLY


def _recognition_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """分类器只接收对话历史和专用指令，不接收客服工具定义或原主模型提示。"""
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    return [SystemMessage(content=INTENT_RECOGNITION_PROMPT), *history]


def _semantic_route(
    result: IntentRecognition, messages: list[BaseMessage]
) -> dict[str, Any]:
    """把结构化语义结果收敛成服务端固定的意图与工具白名单。

    显式订单号须能在用户原文中逐字核验，且同一上下文不能包含多个候选；订单归属
    仍由 InjectedState 和 SQL 的 user_id 条件验证，分类结果不能认证用户或扩大权限。
    """
    data = result.model_dump()
    entities = data["entities"]
    order_no = (entities.get("order_no") or "").strip() or None
    reference = entities.get("order_reference", "none")
    if order_no:
        source_status = _order_no_source_status(order_no, messages)
        if source_status == "valid":
            entities["order_no"] = order_no
            reference = "explicit"
            entities["order_reference"] = reference
        else:
            # 未核验号码不能成为工具参数；之后只会走安全列单或澄清分支。
            entities["order_no"] = None
            entities["order_reference"] = "ambiguous" if source_status == "ambiguous" else "none"
            reference = entities["order_reference"]

    explicitly_latest = _explicit_latest_in_current_message(messages)
    if reference == "latest" and not explicitly_latest:
        # latest 只能由本轮用户原文授权，不能从历史或助手回复推断。
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

    # 此置信度门槛先于列单路由；needs_clarification 不会屏蔽高置信度的缺目标列单。
    # 置信度只决定是否进入查询流程，不参与用户身份或数据权限判断。
    if data["confidence"] < _MIN_INTENT_CONFIDENCE:
        base["direct_reply"] = _clarification_reply({**data, "intent": "unclear"})
        return base
    if data["intent"] == "unclear":
        base["direct_reply"] = _FALLBACK_REPLY
        return base
    if data["intent"] == "unsupported":
        base["direct_reply"] = _UNSUPPORTED_REPLY
        return base

    if data["intent"] not in {"order_query", "logistics_query"}:
        if data["needs_clarification"]:
            base["direct_reply"] = _clarification_reply(data)
            return base
        if data["intent"] == "faq":
            base["allowed_tools"] = ["search_faq"]
            return base
        base["direct_reply"] = _FALLBACK_REPLY
        return base

    current_text = _latest_user_text(messages)
    current_candidates = _order_candidates(current_text)
    recent_options = _previous_order_list(messages)
    current_order_no = entities.get("order_no")

    # 列表回复后的序号/订单号选择只在紧邻的服务器列表中解析；随后还会用本轮
    # query_order 的本人近期订单结果复核，过期或无法唯一定位的选择会重新列单。
    if recent_options and not explicitly_latest:
        selected_order_no = _selected_order_from_list(current_text, recent_options)
        if selected_order_no:
            entities["order_no"] = None
            entities["order_reference"] = "explicit"
            base.update(
                {
                    "allowed_tools": ["query_order"],
                    "order_lookup_pending": True,
                    "order_lookup_mode": (
                        "selection_order" if data["intent"] == "order_query"
                        else "selection_logistics"
                    ),
                    "selection_order_no": selected_order_no,
                }
            )
            return base

        # 本轮重新提供了一个可核验订单号时，按原有明确订单号语义处理；其他
        # 无法绑定到上一条列表的答复先刷新选项，不允许模型沿历史猜选。
        current_order_is_explicit = (
            isinstance(current_order_no, str)
            and _contains_exact_order_no(current_text, current_order_no)
        )
        if not current_order_is_explicit:
            base.update(
                {
                    "allowed_tools": ["query_order"],
                    "order_lookup_pending": True,
                    "order_lookup_mode": (
                        "order_list" if data["intent"] == "order_query"
                        else "logistics_list"
                    ),
                }
            )
            return base

    if len(current_candidates) > 1:
        # 多候选不能由分类器选中其一；让当前用户从真实近期订单中选择。
        entities["order_no"] = None
        entities["order_reference"] = "ambiguous"
        base.update(
            {
                "allowed_tools": ["query_order"],
                "order_lookup_pending": True,
                "order_lookup_mode": (
                    "order_list" if data["intent"] == "order_query"
                    else "logistics_list"
                ),
            }
        )
        return base

    # 目标已唯一时，needs_clarification 仍然阻止直接查单；目标缺失或不唯一时，
    # 它表示需要用户在下方的本人近期订单列表中选择，而不是阻止列单。
    if current_order_no and data["needs_clarification"]:
        base["direct_reply"] = _clarification_reply(data)
        return base

    if data["intent"] == "order_query":
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

    if data["intent"] == "logistics_query":
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


def _arguments_are_authorized(call: dict[str, Any], state: SupportState) -> bool:
    """在 ToolNode 前复核工具名与关键参数，拒绝模型拼造的工具请求。"""
    name = call.get("name")
    allowed_tools = state.get("allowed_tools", [])
    args = call.get("args")
    if (
        not isinstance(name, str)
        or name not in allowed_tools
        or name not in _TOOLS_BY_NAME
        or not isinstance(args, dict)
    ):
        return False

    permitted_arguments = {
        "query_order": {"order_no"},
        "query_logistics": {"order_no"},
        "search_faq": {"question"},
    }[name]
    # 尤其拒绝模型自行提供 user_id；身份只从服务端状态注入工具。
    if not set(args).issubset(permitted_arguments):
        return False

    semantic = state.get("semantic_result", {})
    entities = semantic.get("entities", {}) if isinstance(semantic, dict) else {}
    expected_order_no = entities.get("order_no") if isinstance(entities, dict) else None
    requested_order_no = args.get("order_no")
    if isinstance(requested_order_no, str):
        requested_order_no = requested_order_no.strip() or None

    if name == "query_order":
        # 列出或复核候选时，模型不得自行指定一个订单号缩小服务端查询范围。
        if state.get("order_lookup_pending"):
            return requested_order_no is None
        authorized_order_no = state.get("authorized_order_no")
        if authorized_order_no:
            return requested_order_no == authorized_order_no
        if expected_order_no:
            return requested_order_no == expected_order_no
        return requested_order_no is None

    if name == "query_logistics":
        # 明确订单号或服务端刚从本人订单结果中选出的订单号必须精确匹配。
        authorized_order_no = state.get("authorized_order_no") or expected_order_no
        return (
            isinstance(requested_order_no, str)
            and isinstance(authorized_order_no, str)
            and requested_order_no == authorized_order_no
        )

    question = args.get("question")
    completed_question = state.get("completed_question", "").strip()
    return isinstance(question, str) and question.strip() == completed_question


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

    if mode == "latest":
        # SQL 已按下单时间倒序；仅在用户明确指定最近一笔时才采用第一条。
        selected = valid_orders[0]
    elif mode in {"order_list", "logistics_list"}:
        return {
            "allowed_tools": [],
            "order_lookup_pending": False,
            "direct_reply": _order_list_reply(valid_orders),
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
        "allowed_tools": ["query_logistics"],
        "order_lookup_pending": False,
        "authorized_order_no": selected["order_no"].strip(),
        "direct_reply": "",
    }


def _order_list_reply(orders: list[dict[str, Any]]) -> str:
    """把本人订单工具返回的真实候选格式化成跨轮可精确解析的编号列表。"""
    options = [
        f"{index}. {order['order_no']}"
        for index, order in enumerate(orders, start=1)
    ]
    return _ORDER_LIST_HEADER + "\n" + "\n".join(options)


def build_support_graph():
    """构建显式识别、服务端路由、白名单工具调用和最终回答保存流程。"""
    kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.2,
        "streaming": True,
        "max_tokens": 800,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    model = ChatOpenAI(**kwargs)
    # 结构化分类器没有绑定任何业务工具，输出 schema 也不包含身份或推理过程。
    # 一些 OpenAI 兼容端点不接受 response_format JSON Schema，改用其工具调用格式。
    intent_model = model.with_structured_output(
        IntentRecognition,
        method="function_calling",
    )

    # 分开的 ToolNode 让一次通过校验的调用只能到达对应只读工具。
    tool_nodes = {
        "query_order": ToolNode([query_order], handle_tool_errors=False),
        "query_logistics": ToolNode([query_logistics], handle_tool_errors=False),
        "search_faq": ToolNode([search_faq], handle_tool_errors=False),
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
        return {"messages": [SystemMessage(content=SYSTEM_PROMPT), *history]}

    async def recognize_intent(state: SupportState) -> dict[str, Any]:
        """用会话历史补全当前问题，只保存结构化标签和可路由实体。"""
        try:
            result = await intent_model.ainvoke(_recognition_messages(state["messages"]))
        except (ValidationError, OutputParserException):
            # 结构化结果损坏时按无法判断处理，不能退回到任意工具调用。
            return {
                "semantic_result": {},
                "completed_question": _latest_user_text(state["messages"]),
                "allowed_tools": [],
                "direct_reply": _FALLBACK_REPLY,
            }
        if not isinstance(result, IntentRecognition):
            return {
                "semantic_result": {},
                "completed_question": _latest_user_text(state["messages"]),
                "allowed_tools": [],
                "direct_reply": _FALLBACK_REPLY,
            }
        return {"semantic_result": result.model_dump()}

    def route_intent(state: SupportState) -> dict[str, Any]:
        """由服务端按有限意图集合计算白名单；模型不能提交工具名来改变路由。"""
        if state.get("direct_reply"):
            return {"allowed_tools": []}
        raw = state.get("semantic_result")
        if not isinstance(raw, dict):
            return {
                "allowed_tools": [],
                "completed_question": _latest_user_text(state.get("messages", [])),
                "direct_reply": _FALLBACK_REPLY,
            }
        try:
            recognition = IntentRecognition.model_validate(raw)
        except ValidationError:
            return {"allowed_tools": [], "direct_reply": _FALLBACK_REPLY}
        return _semantic_route(recognition, state.get("messages", []))

    async def generate(state: SupportState) -> dict[str, Any]:
        """生成工具调用轮或最终回答；只有最终回答写入 answer 供 SSE 和保存节点使用。"""
        direct_reply = state.get("direct_reply", "").strip()
        if state.get("tool_rejected"):
            direct_reply = _TOOL_REJECTED_REPLY
        if direct_reply:
            # 澄清、能力兜底和被拒绝工具的答复也经过 generate，沿用现有 SSE 收集点。
            return {"messages": [AIMessage(content=direct_reply)], "answer": direct_reply}

        allowed_names = state.get("allowed_tools", [])
        # 只从服务端常量映射中取工具；状态意外包含其他名字时关闭工具并安全兜底。
        if any(name not in _TOOLS_BY_NAME for name in allowed_names):
            return {
                "messages": [AIMessage(content=_TOOL_REJECTED_REPLY)],
                "answer": _TOOL_REJECTED_REPLY,
            }
        turn_model = model.bind_tools([_TOOLS_BY_NAME[name] for name in allowed_names]) if allowed_names else model

        messages = list(state["messages"])
        completed_question = state.get("completed_question", "").strip()
        if completed_question:
            # 仅本次模型上下文使用补全句；数据库和消息 API 仍保留用户原始文字。
            for index in range(len(messages) - 1, -1, -1):
                if isinstance(messages[index], HumanMessage):
                    messages[index] = HumanMessage(content=completed_question)
                    break

        if state.get("authorized_order_no"):
            messages[0] = SystemMessage(
                content=(
                    SYSTEM_PROMPT
                    + "\n\n本轮目标订单号已由服务端验证并授权；调用 query_order 或 "
                    "query_logistics 时，参数必须使用该订单号。"
                )
            )

        response = None
        async for chunk in turn_model.astream(messages):
            response = chunk if response is None else response + chunk
        if response is None:
            raise RuntimeError("模型没有生成回答。")

        # 任何工具调用都先经过 route_after_generate 的服务端白名单和参数检查。
        has_tool_calls = bool(
            getattr(response, "tool_calls", []) or getattr(response, "invalid_tool_calls", [])
        )
        # SSE 只发送 generate.output.answer；工具白名单仍有效时，模型文本只能是待验证的
        # 工具轮输出，不能先作为答案发出，再由后续拒绝节点撤回。
        answer = (
            ""
            if has_tool_calls or allowed_names
            else _content_as_text(response.content).strip()
        )
        return {"messages": [response], "answer": answer}

    def route_after_generate(state: SupportState) -> str:
        """所有工具调用先做服务端校验；名称或参数不符时绝不进入任何 ToolNode。"""
        message = state["messages"][-1]
        calls = getattr(message, "tool_calls", [])
        invalid_calls = getattr(message, "invalid_tool_calls", [])
        if not calls and not invalid_calls:
            if state.get("allowed_tools"):
                # 仍有业务工具权限时，模型的自然语言不能代替真实查询结果。
                return "reject_tool_call"
            return "save_answer"
        if len(calls) != 1 or invalid_calls or not _arguments_are_authorized(calls[0], state):
            return "reject_tool_call"
        # 名称已通过服务端常量表验证，路由目标也只可能是下方静态 ToolNode。
        return _TOOL_NODE_BY_NAME[calls[0]["name"]]

    def after_tools(state: SupportState) -> dict[str, Any]:
        """工具返回后关闭本轮权限；物流前置查单只有唯一目标才开放物流工具。"""
        if (
            state.get("order_lookup_pending")
            and state.get("messages")
            and isinstance(state["messages"][-1], ToolMessage)
            and state["messages"][-1].name == "query_order"
        ):
            return _order_lookup_result(state)
        # 查询工具一旦执行完毕就收回白名单，防止模型在同一意图下重复或扩展查询。
        return {"allowed_tools": [], "order_lookup_pending": False}

    def reject_tool_call(state: SupportState) -> dict[str, Any]:
        """拒绝模型越过意图白名单或篡改订单号的调用，并让 generate 给出安全答复。"""
        return {"allowed_tools": [], "tool_rejected": True}

    def save_answer(state: SupportState) -> dict[str, str]:
        """模型完成工具循环或澄清/兜底后，仅保存 generate 产出的最终回答。"""
        answer = state["answer"]
        if not answer:
            raise RuntimeError("模型没有生成可显示的回答。")
        finish_assistant_message(
            state["assistant_message_id"],
            state["conversation_id"],
            state["user_id"],
            answer,
            "complete",
        )
        return {}

    graph = StateGraph(SupportState)
    graph.add_node("load_context", load_context)
    graph.add_node("recognize_intent", recognize_intent)
    graph.add_node("route_intent", route_intent)
    graph.add_node("generate", generate)
    graph.add_node("after_tools", after_tools)
    graph.add_node("reject_tool_call", reject_tool_call)
    graph.add_node("save_answer", save_answer)
    for tool_name, node_name in _TOOL_NODE_BY_NAME.items():
        graph.add_node(node_name, tool_nodes[tool_name])

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "recognize_intent")
    graph.add_edge("recognize_intent", "route_intent")
    graph.add_edge("route_intent", "generate")
    graph.add_conditional_edges(
        "generate",
        route_after_generate,
        {
            **{node_name: node_name for node_name in _TOOL_NODE_BY_NAME.values()},
            "reject_tool_call": "reject_tool_call",
            "save_answer": "save_answer",
        },
    )
    for node_name in _TOOL_NODE_BY_NAME.values():
        graph.add_edge(node_name, "after_tools")
    graph.add_edge("after_tools", "generate")
    graph.add_edge("reject_tool_call", "generate")
    graph.add_edge("save_answer", END)
    return graph.compile()
