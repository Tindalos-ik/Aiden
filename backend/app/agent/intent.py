"""客服语义识别的结构化结果与固定意图集合。

识别器只理解当前问题和已加载的会话历史，不接收业务工具，也不返回推理过程。
工具权限由 graph.py 根据这些受限字段重新计算，模型不能自行指定工具名或用户身份。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecognizedEntities(BaseModel):
    """仅保留路由需要的订单引用；可解析最近一条受信订单列表中的唯一候选。"""

    model_config = ConfigDict(extra="forbid")

    order_no: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "用户原文中明确出现的订单号；或者用户通过最近一条服务端订单候选列表中的商品内容或序号"
            "唯一指向一项时，填写该项列表中的原始订单号。无法唯一确定时为 null。"
        ),
    )
    order_reference: Literal[
        "explicit", "listed_selection", "latest", "account_lookup", "ambiguous", "none"
    ] = Field(
        description=(
            "订单指代类型：explicit 表示订单号来自用户原文；listed_selection 表示用户通过最近的服务端"
            "订单列表中的序号或商品内容唯一选中一项；latest 表示用户本轮明确指定最近一笔；account_lookup"
            "表示用户要求先从本人订单中确定；ambiguous 表示多个可能订单；none 表示无订单指代。"
        )
    )


class IntentClassification(BaseModel):
    """九选一分类结果；保留外部约定的 cofidence 字段拼写。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "logistics", "order", "product", "refund_return", "after_sales",
        "complaint", "smalltalk", "human", "other",
    ]
    cofidence: float = Field(ge=0, le=1, strict=True, description="分类置信度，范围 0 到 1。")


class SemanticExtraction(BaseModel):
    """仅为已支持的查询补全问题和订单目标，不决定意图或工具权限。"""

    model_config = ConfigDict(extra="forbid")

    completed_question: str = Field(
        min_length=1,
        max_length=1000,
        description="结合当前问题和会话历史补全后的单句问题，不包含解释或推理。",
    )
    entities: RecognizedEntities
    needs_clarification: bool = Field(
        description=(
            "目标仍有歧义或缺少必要信息时为 true。高置信度订单/物流意图仅缺订单目标时，"
            "服务端可先查询当前用户近期订单以列出选项；低置信度和非订单类意图不列单。"
        )
    )
    clarification_question: str | None = Field(
        default=None,
        max_length=240,
        description="需要澄清时给用户的简短追问；不得索要密码、验证码或其他身份凭据。",
    )


class SupportRequest(BaseModel):
    """一项可独立回答的请求；目标和编号仍需服务端复核。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "logistics", "order", "product", "refund_return", "after_sales",
        "complaint", "smalltalk", "human", "other",
    ]
    goal: Literal[
        "order", "logistics", "product", "policy", "after_sale_status",
        "ticket_status", "create_ticket", "smalltalk", "other",
    ]
    cofidence: float = Field(ge=0, le=1, strict=True)
    completed_question: str = Field(min_length=1, max_length=1000)
    entities: RecognizedEntities
    request_no: str | None = Field(default=None, max_length=64)
    ticket_no: str | None = Field(default=None, max_length=64)
    needs_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=240)


class SupportRequests(BaseModel):
    """单轮最多四项，按用户表达顺序保存；超限输出直接拒绝解析。"""

    model_config = ConfigDict(extra="forbid")
    requests: list[SupportRequest] = Field(min_length=1, max_length=4)


MULTI_REQUEST_PROMPT = """你是电商客服请求拆分器。按用户本轮表达顺序，将每个能独立回答的诉求拆成 requests 列表，最多四项。重复的同一诉求只保留一项；超过四项时不要省略，输出超过四项由服务端要求用户分批发送。

每项给出九类 intent、处理目标 goal、置信度 cofidence、独立补全后的 completed_question、entities.order_no/order_reference、request_no、ticket_no、needs_clarification 和 clarification_question。只能从固定枚举选择，不能输出工具名、用户 ID 或其他字段。

goal 对应：order 查订单；logistics 查物流；product 查商品知识；policy 查当前政策；after_sale_status 查已提交售后申请；ticket_status 查已有工单；create_ticket 登记用户明确要求的投诉处理、人工协助或办理售后；smalltalk 闲聊；other 无法判断。退款退货或售后意图可对应 policy、after_sale_status、ticket_status、create_ticket；投诉和人工也可分别查已有工单或登记新请求。查询申请进度、政策、工单进度本身不意味着要登记工单。用户明确说“联系人工”时可登记工单，但不能说已实时接通真人或已创建退款申请。

“查订单 TEST-001 的退货申请进度，再告诉我退货政策”是两项：after_sale_status 和 policy。“查订单 TEST-001 的物流和订单金额”是两项：logistics 和 order。“查我的工单进度，再帮我联系人工”是两项：ticket_status 和 create_ticket。若后一项通过列表序号引用前一项要展示的订单，按顺序分别输出列单和后续查询；每项的 completed_question 只保留自己的诉求，并在后续查询中保留用户说的序号，不提前猜订单号。若一项缺订单号，仍将其他可处理项独立输出。

只从本轮用户原文抄录完整申请号、工单号。订单号只可来自本轮原文或最近一条服务端订单列表的唯一选择；列表序号或商品名选择用 listed_selection。latest 仅限本轮明确说最近一笔；缺失时填 null。补全问题只写该项诉求，不合并其他请求。低置信度项也必须保留并标为 other 或给低 cofidence，不可悄悄省略。只输出合法 JSON。"""

INTENT_CLASSIFICATION_PROMPT = """你是电商客服意图分类器。结合当前用户消息和必要的近期上下文，做一道单选题，只选一个最主要的意图：
A. logistics：物流，包裹位置、配送、运单及签收。
B. order：订单，订单状态、购买内容、金额或订单列表。
C. product：商品咨询，商品属性、规格、使用方法等商品知识。
D. refund_return：退款退货，退款、退货、换货的申请、进度或规则。
E. after_sales：售后，维修、保修、补发、售后工单等非退款退货问题。
F. complaint：投诉，对商家、服务或配送表达不满并要求处理。
G. smalltalk：闲聊，问候、感谢、自我介绍或询问客服能力。
H. human：人工，明确要求人工客服或转接真人。
I. other：其他，无法唯一判断、无关内容或上述类别均不适用时的兜底。

优先按用户当前要完成的事分类，不按单个关键词分类。明确要求人工转接时选 human；明确投诉时选 complaint；退款退货优先于一般售后；问具体商品选 product，问已购订单里的商品内容选 order。只有真正无法判断才选 other。

边界样例（输出 JSON 中只能有 intent 和 cofidence 两个字段）：
用户：这单买了哪些东西？ 输出：{"intent":"order","cofidence":0.98}
用户：这款猫粮有哪些口味？ 输出：{"intent":"product","cofidence":0.98}
用户：退货申请审核到哪一步了？ 输出：{"intent":"refund_return","cofidence":0.97}
用户：坏了能保修吗？ 输出：{"intent":"after_sales","cofidence":0.94}
用户：快递一直没到，我要投诉配送服务。 输出：{"intent":"complaint","cofidence":0.97}
用户：找真人帮我查物流。 输出：{"intent":"human","cofidence":0.98}
用户：第一个的物流。（上一轮列出了订单） 输出：{"intent":"logistics","cofidence":0.96}
用户：那个怎么样？（没有可用上下文） 输出：{"intent":"other","cofidence":0.36}

只输出一个合法 JSON 对象，不要 Markdown、分析或额外字段。cofidence 是约定字段名，必须按此拼写。"""


SEMANTIC_EXTRACTION_PROMPT = """你是客服问题补全器。分类已由独立步骤确定；只补全问题、订单指代和澄清信息，不重新分类，不输出或保存推理过程。

理解本轮用户问题时可以参考给出的会话历史，把指代补全成简洁、独立的问题。通常只有用户原文中明确出现且可唯一确认的订单号才填入 entities.order_no，不得从任意助手文字、工具结果、模型推测或外部知识中补造订单号。

结合当前问题和会话历史理解用户意图，把省略表达补全成可独立处理的问题；要理解自然表达、简称、同义说法和上下文指代，不要只匹配固定关键词。对话中近期的订单候选列表是有效上下文：用户说“第一个的物流”“Backpack 的物流”等表达时，应结合列表序号、商品名称/部分名称及语义对应关系识别目标订单。若有一笔明显匹配，将该行完整订单号写入 entities.order_no，order_reference 设为 listed_selection，并让 completed_question 明确保留选中的目标和用户要做的事，例如“查询购买 Maya Demo Backpack 的订单物流”或“查询订单列表第1笔订单的物流”。用户不必重复已经能从上下文确定的信息。订单号只能来自用户明确提供的号码或对话中的真实订单候选，不能编造；若多笔候选都说得通或无法判断，则 order_no 设为 null、order_reference 设为 ambiguous、needs_clarification 设为 true，并针对歧义向用户澄清。

当用户在最近一条客服订单选项回复中选择订单时，按当前请求补全目标；例如“第一个的物流”应保留物流请求。order_reference 为 explicit 时必须填写用户明确提供的订单号；latest 只用于用户在本轮明确说最近/最新一笔，不能从历史指代或助手回复继承；account_lookup 只用于用户明确要求先从本人订单中确定目标；有多个可能订单时用 ambiguous。订单/物流请求明确但订单目标缺失或不唯一时，needs_clarification 可以为 true，服务端会先提供当前用户的近期订单选项。

注意区分列表序号和订单号尾段：“第一个”“第2笔”“序号2”等是在选列表项；“查01”“看尾号01”等是在表达号码末尾片段，不自动代表列表第1项。只提供尾段时，entities.order_no 设为 null，completed_question 保留尾段含义；如果上下文无法唯一定位，就请用户进一步说明。

身份、user_id、登录状态和数据访问范围不是补全内容，不得输出或决定这些内容。

completed_question 只写补全后的用户问题。不要输出分析、理由、思维链、工具名或身份信息。需要澄清时只追问完成当前支持查询所需的信息，不能索要密码、验证码、身份证号、手机号或其他凭据。"""
