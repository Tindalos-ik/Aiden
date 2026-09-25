import json
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
_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号、尾号或列表序号选择要查询的订单："
_LEGACY_ORDER_LIST_HEADER = "我找到您近期的订单，请回复订单号或序号选择要查询的订单："
_ORDER_LIST_ENTRY_PATTERN = re.compile(r"([1-9]\d*)\. (.+)")
_ORDER_LIST_PRODUCT_LIMIT = 3
_ORDER_SELECTION_ORDINAL_PATTERNS = (
    re.compile(r"第\s*([1-5一二三四五])\s*(?:个|笔|单)?"),
    re.compile(r"(?:我选|选择|选|序号)\s*[:：]?\s*([1-5一二三四五])\s*(?:个|笔|单)?"),
)
_ORDER_SUFFIX_MARKER_PATTERN = re.compile(
    r"(?:尾号|末尾|后缀|最后(?:\s*(?:[一二三四五六七八九十\d]+)?位)?)"
    r"\s*(?:(?:是|为|[:：])\s*)?([A-Za-z0-9][A-Za-z0-9_-]{0,15})(?![A-Za-z0-9_-])"
)
_ORDER_SUFFIX_QUERY_PATTERN = re.compile(
    r"(?:查询|查看|查|看)(?:一下|下)?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,3})(?![A-Za-z0-9_-])"
)
_CHINESE_ORDINALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_EXPLICIT_LATEST_PATTERN = re.compile(
    r"(?:最近\s*(?:的\s*)?(?:一笔|一单|一个订单)|"
    r"最新\s*(?:的\s*)?(?:一笔|一单|一个订单|订单)|"
    r"最后\s*(?:的\s*)?(?:一笔|一单))"
)


# search_faq 是唯一的知识依据入口：它按语义相似度检索已向量化的知识库，命中项覆盖商品 FAQ、
# 政策与手册。政策正文没有单独的版本或生效期查询能力，售后进度与工单也没有接入本 Agent。
SYSTEM_PROMPT = """你是「喵购商城」的智能客服 Aiden，语气亲切、回答简洁，不用网络烂梗。

查询类能力仅包括查询当前登录用户的订单及商品快照、查询其订单物流，以及检索已入库的常见问题、政策与手册知识。对于问候、感谢、请你介绍自己或询问你能做什么，可以不调用工具，直接简短自然地回答。介绍自己或说明能力时，只能说明你是喵购商城的智能客服 Aiden，可以查询本人订单和物流、依据已接入的知识库回答常见问题与政策；明确不要声称自己是真人客服，也不要承诺尚未接入的售后进度、工单或人工转接能力。订单、物流和知识检索仍按本轮问题调用系统提供的工具；工具结果返回后再回答。

必须遵守：
1. 订单详情和状态只依据 query_order 的结果；物流只依据 query_logistics 的结果。订单号必须原样使用补全问题中已识别的号码，不得改写或猜测。
2. 知识类问题只能依据 search_faq 实际返回的结果，按 category、question、answer 三项作答。调用时把补全后的当前问题原样作为 question 参数，不要自己改写或拆分关键词。question 可能是用「｜」连接的多条问法，也可能是章节标题（政策、手册块没有天然问法）；它只说明这块知识讲什么，判断是否对得上用户问题要结合 category 与 answer。
3. search_faq 没有返回内容，或返回的内容与用户问题对不上时，明确说暂时无法核实。不得把 policies 表、模型记忆、常识或其他用户的历史回答当作已检索的政策；也不得因为知识块带章节标题就编造条款细节、生效日期或适用范围。
4. 退货/退款申请进度、工单状态和人工转接目前没有查询能力；不能伪装成查过，也不能声称已转接人工。政策条款没有单独的版本或生效期查询，知识库返回什么就答什么。
5. 查询为空、报错或结果互相矛盾时如实说明；不得编造订单、金额、物流节点、日期、政策或承诺。
6. 缺少唯一订单目标时，服务端可能先列出本人近期订单。语义识别可以结合最近一条规范订单列表中的序号或商品内容补全用户选择；只有该选择经服务端核验且属于当前用户时才能继续查询，不得自行猜选。
7. 工具计划和原始工具结果都不是最终答复。工具结果返回后再回答，不要把工具参数、JSON 或内部结果原样当成答案。
8. 回答订单问题时，只依据 query_order 实际返回的订单与商品快照。用户询问购买内容时，简要列出 items 中的商品名和数量；有规格信息时可补充 sku_name。不得根据订单号、商品常识或历史记忆推测商品。工具未返回商品明细时，明确说明目前查不到商品内容。
9. 不要透露语义分类、内部路由、工具白名单或任何隐藏推理。"""


_FALLBACK_REPLY = "我还没能确定您想查询的内容。您可以说明是查询订单、物流，还是咨询常见问题；查询订单或物流时请提供订单号。"
_TOOL_REJECTED_REPLY = "抱歉，我暂时无法安全完成这项查询。请重新说明要查询的订单号、物流信息或常见问题。"
_UNSUPPORTED_REPLY = "目前我可以协助查询订单、物流，并检索已入库的常见问题与政策知识。售后进度、工单状态和人工转接暂不支持；政策问题只有在知识库返回相关内容时才能说明。"


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


def _explicit_order_numbers(text: str) -> list[str]:
    """从本轮原文保留唯一完整订单号的原始大小写，不依赖分类器抄录实体。"""
    marked_values = set(_ORDER_MARKER_PATTERN.findall(text))
    values = set(_ORDER_TOKEN_PATTERN.findall(text)) | marked_values
    candidates: dict[str, str] = {}
    for value in values:
        if not any(char.isdigit() for char in value):
            continue
        if _CALENDAR_DATE_PATTERN.fullmatch(value):
            continue
        has_letters = any(char.isalpha() for char in value)
        if has_letters or "_" in value or "-" in value or len(value) >= 3 or value in marked_values:
            candidates.setdefault(value.casefold(), value)
    return list(candidates.values())


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
    if (
        not lines
        or lines[0] not in {_ORDER_LIST_HEADER, _LEGACY_ORDER_LIST_HEADER}
        or len(lines) < 2
    ):
        return None

    has_product_details = lines[0] == _ORDER_LIST_HEADER
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


def _order_suffix_from_text(text: str) -> str | None:
    """提取用户明确表达的订单尾号；简短数字查询词按尾号处理，不按列表序号猜选。"""
    marked_suffix = _ORDER_SUFFIX_MARKER_PATTERN.search(text)
    if marked_suffix:
        return marked_suffix.group(1)
    if _ORDER_MARKER_PATTERN.search(text):
        # “订单号01”是完整订单号表达；不将其改解释为未标注的尾号。
        return None
    short_query = _ORDER_SUFFIX_QUERY_PATTERN.search(text)
    return short_query.group(1) if short_query else None


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

    if intent == "order_query":
        return "请告诉我订单号，或说明您想查看最近几笔订单。"
    if intent == "logistics_query":
        return "请提供要查询物流的订单号；如果您指最近一笔订单，也可以明确说明。"
    if intent == "faq":
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


def _recognition_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """分类器只接收对话历史、专用指令和输出 schema，不接收业务工具定义。"""
    history = [message for message in messages if not isinstance(message, SystemMessage)]
    prompt = (
        INTENT_RECOGNITION_PROMPT
        + "\n\n只输出一个符合以下 JSON Schema 的 JSON 对象，不要添加 Markdown 或说明：\n"
        + json.dumps(IntentRecognition.model_json_schema(), ensure_ascii=False)
    )
    return [SystemMessage(content=prompt), *history]


def _semantic_route(
    result: IntentRecognition, messages: list[BaseMessage]
) -> dict[str, Any]:
    """把结构化语义结果收敛成服务端固定的意图与工具白名单。

    显式订单号须能在用户原文中逐字核验；由语义层从紧邻服务端列表中解析的选择，
    也必须精确命中该列表。订单归属仍由 InjectedState 和 SQL 的 user_id 条件验证。
    """
    data = result.model_dump()
    entities = data["entities"]
    order_no = (entities.get("order_no") or "").strip() or None
    reference = entities.get("order_reference", "none")
    current_text = _latest_user_text(messages)
    recent_options = _previous_order_list(messages)
    if order_no:
        source_status = _order_no_source_status(order_no, messages)
        if source_status == "valid":
            entities["order_no"] = order_no
            reference = "explicit"
            entities["order_reference"] = reference
        elif reference == "listed_selection" and source_status == "missing":
            listed_matches = [
                candidate
                for candidate in (recent_options or [])
                if candidate.casefold() == order_no.casefold()
            ]
            if len(listed_matches) == 1 and not _order_candidates(current_text):
                # 订单号由语义层按商品/序号从最近列表中选出；只校验候选来源并规范回原值。
                order_no = listed_matches[0]
                entities["order_no"] = order_no
                entities["order_reference"] = "listed_selection"
            else:
                entities["order_no"] = None
                entities["order_reference"] = "ambiguous"
                reference = "ambiguous"
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
    # 无工具的 smalltalk 不受门槛限制；置信度只决定是否进入查询流程，不参与身份或权限判断。
    if (
        data["confidence"] < _MIN_INTENT_CONFIDENCE
        and data["intent"] != "smalltalk"
    ):
        base["direct_reply"] = _clarification_reply({**data, "intent": "unclear"})
        return base
    if data["intent"] == "unclear":
        base["direct_reply"] = _FALLBACK_REPLY
        return base
    if data["intent"] == "unsupported":
        base["direct_reply"] = _UNSUPPORTED_REPLY
        return base

    if data["intent"] not in {"order_query", "logistics_query"}:
        if data["intent"] == "smalltalk":
            # 简单对话无需业务工具；主模型只按 System Prompt 说明身份和已接入能力。
            return base
        if data["needs_clarification"]:
            base["direct_reply"] = _clarification_reply(data)
            return base
        if data["intent"] == "faq":
            base["allowed_tools"] = ["search_faq"]
            return base
        base["direct_reply"] = _FALLBACK_REPLY
        return base

    current_candidates = _order_candidates(current_text)
    current_order_no = entities.get("order_no")
    if current_order_no is None:
        explicit_order_nos = _explicit_order_numbers(current_text)
        if len(explicit_order_nos) == 1:
            raw_order_no = explicit_order_nos[0]
            if _order_no_source_status(raw_order_no, messages) == "valid":
                current_order_no = raw_order_no
                entities["order_no"] = raw_order_no
                entities["order_reference"] = "explicit"
                reference = "explicit"

    # 列表回复后的语义选择、完整订单号或尾号只绑定紧邻的服务器列表；随后仍会用
    # 本人近期订单结果复核，过期选择会重新列单，歧义引用则要求用户澄清。
    suffix_fragment = _order_suffix_from_text(current_text)
    if suffix_fragment:
        # 即使当前没有可核对的上一条列表，也不能把末尾片段当作完整订单号查询。
        entities["order_no"] = None
        entities["order_reference"] = "ambiguous"
        current_order_no = None
    if recent_options and (not explicitly_latest or suffix_fragment):
        selected_order_no = None
        if suffix_fragment:
            ordinals = _selection_ordinal(current_text)
            if ordinals:
                entities["order_no"] = None
                entities["order_reference"] = "ambiguous"
                base["direct_reply"] = "请明确说明您要查询的完整订单号、订单尾号或列表序号。"
                return base
            suffix_matches = _orders_matching_suffix(recent_options, suffix_fragment)
            if len(suffix_matches) != 1:
                entities["order_no"] = None
                entities["order_reference"] = "ambiguous"
                if suffix_matches:
                    base["direct_reply"] = (
                        f"找到多个尾号为“{suffix_fragment}”的订单，请提供完整订单号或列表序号。"
                    )
                else:
                    base["direct_reply"] = (
                        f"近期订单中没有找到尾号为“{suffix_fragment}”的订单，"
                        "请提供完整订单号或列表序号。"
                    )
                return base
            selected_order_no = suffix_matches[0]
        else:
            parsed_selection = _selected_order_from_list(current_text, recent_options)
            listed_selection = (
                current_order_no
                if reference == "listed_selection" and current_order_no in recent_options
                else None
            )
            if listed_selection and parsed_selection and listed_selection != parsed_selection:
                entities["order_no"] = None
                entities["order_reference"] = "ambiguous"
                base["direct_reply"] = "我无法确定您指的是列表中的哪一笔订单，请回复完整订单号、尾号或列表序号。"
                return base
            selected_order_no = listed_selection or parsed_selection
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

        if reference in {"listed_selection", "ambiguous"}:
            entities["order_no"] = None
            entities["order_reference"] = "ambiguous"
            base["direct_reply"] = "我没能从刚才的订单列表中唯一确定您指的订单，请回复完整订单号、尾号或列表序号。"
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
    """展示本人订单的商品快照，并保留只从服务端生成的可解析订单号。"""
    options = [
        f"{index}. {order['order_no']} — {_order_items_summary(order)}"
        for index, order in enumerate(orders, start=1)
    ]
    return _ORDER_LIST_HEADER + "\n" + "\n".join(options)


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
            response = await model.ainvoke(_recognition_messages(state["messages"]))
            result = IntentRecognition.model_validate_json(
                _content_as_text(response.content)
            )
        except (TypeError, ValueError):
            # 结构化结果损坏时按无法判断处理，不能退回到任意工具调用。
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

    async def dispatch_tool_call(state: SupportState) -> dict[str, Any]:
        """由服务端构造已批准的工具调用，避免模型漏调工具或改写订单参数。"""
        allowed_names = state.get("allowed_tools", [])
        if len(allowed_names) != 1 or allowed_names[0] not in _TOOLS_BY_NAME:
            return {"allowed_tools": [], "direct_reply": _TOOL_REJECTED_REPLY}

        name = allowed_names[0]
        args: dict[str, Any] = {}
        semantic = state.get("semantic_result", {})
        entities = semantic.get("entities", {}) if isinstance(semantic, dict) else {}

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

        call = {
            "name": name,
            "args": args,
            "id": str(uuid4()),
            "type": "tool_call",
        }
        return {"messages": [AIMessage(content="", tool_calls=[call])]}

    async def generate(state: SupportState) -> dict[str, Any]:
        """仅生成最终答复；查询工具由服务端路由并构造参数后执行。"""
        direct_reply = state.get("direct_reply", "").strip()
        if direct_reply:
            return {"messages": [AIMessage(content=direct_reply)], "answer": direct_reply}

        messages = list(state["messages"])
        completed_question = state.get("completed_question", "").strip()
        if completed_question:
            # 仅本次模型上下文使用补全句；数据库和消息 API 仍保留用户原始文字。
            for index in range(len(messages) - 1, -1, -1):
                if isinstance(messages[index], HumanMessage):
                    messages[index] = HumanMessage(content=completed_question)
                    break

        response = None
        async for chunk in model.astream(messages):
            response = chunk if response is None else response + chunk
        if response is None:
            raise RuntimeError("模型没有生成回答。")

        answer = _content_as_text(response.content).strip()
        return {"messages": [response], "answer": answer}

    def route_after_intent(state: SupportState) -> str:
        """语义路由已批准工具时交给服务端构造调用，否则直接生成答复。"""
        return "dispatch_tool_call" if state.get("allowed_tools") else "generate"

    def route_dispatched_tool(state: SupportState) -> str:
        """只把服务端构造且位于静态映射中的工具调用交给对应 ToolNode。"""
        allowed_names = state.get("allowed_tools", [])
        if len(allowed_names) != 1:
            return "generate"
        return _TOOL_NODE_BY_NAME.get(allowed_names[0], "generate")

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

    def route_after_tools(state: SupportState) -> str:
        """列表选择核验可能开放下一次物流查询；其余查询结果直接生成答复。"""
        return "dispatch_tool_call" if state.get("allowed_tools") else "generate"

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
    graph.add_node("dispatch_tool_call", dispatch_tool_call)
    graph.add_node("generate", generate)
    graph.add_node("after_tools", after_tools)
    graph.add_node("save_answer", save_answer)
    for tool_name, node_name in _TOOL_NODE_BY_NAME.items():
        graph.add_node(node_name, tool_nodes[tool_name])

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "recognize_intent")
    graph.add_edge("recognize_intent", "route_intent")
    graph.add_conditional_edges(
        "route_intent",
        route_after_intent,
        {"dispatch_tool_call": "dispatch_tool_call", "generate": "generate"},
    )
    graph.add_conditional_edges(
        "dispatch_tool_call",
        route_dispatched_tool,
        {
            **{node_name: node_name for node_name in _TOOL_NODE_BY_NAME.values()},
            "generate": "generate",
        },
    )
    for node_name in _TOOL_NODE_BY_NAME.values():
        graph.add_edge(node_name, "after_tools")
    graph.add_conditional_edges(
        "after_tools",
        route_after_tools,
        {"dispatch_tool_call": "dispatch_tool_call", "generate": "generate"},
    )
    graph.add_edge("generate", "save_answer")
    graph.add_edge("save_answer", END)
    return graph.compile()
