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
    reference_quote: str | None = Field(
        default=None, max_length=120,
        description="选择列表、最近一笔或尾号时，逐字摘录本轮用户表达该引用的短句。",
    )
    list_index: int | None = Field(
        default=None, ge=1, le=5,
        description="用户按列表序号选择时对应的序号；其他情况为 null。",
    )
    order_suffix: str | None = Field(
        default=None, max_length=16,
        description="用户只提供订单尾号时填写尾号原文；其他情况为 null。",
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
    """一项独立请求；原因取保留语义限定的最小原文片段，目标和编号仍需服务端复核。"""

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
    original_question_quote: str | None = Field(
        default=None, max_length=1000,
        description="仅逐字摘录本轮用户原文中属于这一诉求的连续片段，不得改写或混入其他诉求。",
    )
    entities: RecognizedEntities
    request_no: str | None = Field(default=None, max_length=64)
    ticket_no: str | None = Field(default=None, max_length=64)
    action_quote: str | None = Field(
        max_length=240,
        description="仅新建工单或转人工时，逐字摘录本轮用户要求该操作的原话；其他请求为 null。",
    )
    refund_reason_quote: str | None = Field(default=None, max_length=160,
        description="办理退款、退货或换货时，从本轮用户原文逐字摘录实际问题短语作为最小完整原因，必须是连续片段；排除无语义限定作用的订单/商品主语、申请动作及肯定陈述引导词（如有、存在、出现），但完整保留否定、假设、程度、时间和对象限定，不能制造肯定本人原因；未说明则为 null。")
    application_type: Literal["refund", "return", "exchange", "none"] = Field(
        default="none", description="本轮或已确认上下文的正式申请类型；无法确定时选 none。")
    refund_consent: Literal["agree", "decline", "none"] = Field(default="none",
        description="仅对上一轮明确提出的退款申请提交请求，识别本轮同意、拒绝或未表态。")
    needs_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=240)


class SupportRequests(BaseModel):
    """单轮最多四项，按用户表达顺序保存；超限输出直接拒绝解析。"""

    model_config = ConfigDict(extra="forbid")
    requests: list[SupportRequest] = Field(min_length=1, max_length=4)


MULTI_REQUEST_PROMPT = """你是电商客服请求拆分器。只处理对话中最后一条用户消息；历史仅用于补全这条消息中的指代，不要把历史请求或客服回复当成本轮诉求。按用户本轮表达顺序，将每个能独立回答的诉求拆成 requests 列表，最多四项。重复的同一诉求只保留一项；超过四项时不要省略，输出超过四项由服务端要求用户分批发送。

每项给出九类 intent、处理目标 goal、置信度 cofidence、独立补全后的 completed_question、original_question_quote（从本轮用户原文逐字摘录这一诉求，不能包含其他诉求）、entities.order_no/order_reference/reference_quote/list_index/order_suffix、request_no、ticket_no、action_quote、refund_reason_quote、application_type、refund_consent、needs_clarification 和 clarification_question。只能从固定枚举选择，不能输出工具名、用户 ID 或其他字段。

goal 对应：order 查订单；logistics 查物流；product 查商品知识；policy 检索政策文档中的规则；after_sale_status 查已提交售后申请；ticket_status 查已有工单；create_ticket 登记用户明确要求的投诉处理或办理售后；smalltalk 闲聊；other 无法判断。退款退货或售后意图可对应 policy、after_sale_status、ticket_status、create_ticket；要办理尚未提交的退款、退货或换货选 create_ticket，只询问规则或条件才选 policy，查询已提交申请的进度选 after_sale_status。明确要求“找真人/转人工/联系人工客服”时 intent 选 human、goal 选 other，由服务端转入实时人工队列；只有明确登记处理工单时才选 create_ticket。投诉可以提供转人工选择，不能仅凭“投诉”一词自动转接。查询申请进度、政策、工单进度本身不意味着要登记工单。

未说明具体工单类型的“查询工单”“查询一下工单”“我的工单”“工单进度”等通用工单查询，intent 选 after_sales、goal 选 ticket_status，不选 other。未提供工单号时 ticket_no 填 null，表示查询当前用户最近至多五条工单；不能仅因缺工单号或订单号而要求澄清，此时 needs_clarification 为 false、clarification_question 为 null。提供完整工单号时按该编号查询，不从历史或模型猜测补造编号。工单查询不等于新建工单、提交售后申请或转接人工，action_quote 填 null。

纯问候、感谢、告别、询问客服身份或能力归 smalltalk，goal 也填 smalltalk，needs_clarification 为 false；没有业务工具目标不等于 other。若闲聊和业务请求同时出现，分别输出，不要省略任一项。

用户希望办理退款、退货或换货时仍选 create_ticket 这个处理目标，但它只表示进入服务端核对流程，不能自行决定写入。application_type 按用户实际要办理的类型填写 refund、return 或 exchange；用户仅说“售后申请”且无法判断类型时选 none，不要默认退款。先确认本人订单、原因和适用政策；上一轮客服明确询问是否正式提交该订单的退款申请后，本轮用户明确同意才将 refund_consent 设为 agree，明确拒绝设为 decline，其余为 none。“我要退款”这类首次请求均为 none。确认或拒绝要结合最近一条客服答复理解，不能仅凭孤立词匹配。refund_reason_quote 必须从本轮用户原文逐字摘录实际问题短语作为最小完整退款、退货或换货原因，保持连续片段，不能改写、拼接或借用历史原因；排除没有语义限定作用的订单/商品主语、申请动作及肯定陈述引导词（如“有”“存在”“出现”），但完整保留否定、假设、程度、时间和对象限定，不能为匹配政策而缩短原因或制造肯定本人原因。词界对照：“存在质量问题”取“质量问题”；“没有质量问题”“如果有质量问题”“可能存在质量问题”“曾经有质量问题”“轻微质量问题”“别人的商品有质量问题”均保留完整限定，不得截成“质量问题”。若限定与主语或引导词连在一起，宁可保留它们也不能丢掉限定；只排除无语义限定作用的肯定叙述，不把“没有”拆成“有”来删除。只有订单选择、没有原因时为 null。仅缺订单号或原因不应阻止列本人订单或自然追问。
客服刚列出售后办理订单候选后，用户回复“第一个”“尾号001”“那笔猫粮”属于继续办理原申请类型，intent 仍为 refund_return、goal 仍为 create_ticket，并按订单列表引用字段填写选择，不得把退货或换货改成退款。客服刚核对订单并追问退款原因后，用户只回复原因也属于继续办理退款。客服刚给出退款处理待确认话术后，用户回复同意或取消仍属于同一项退款办理诉求；明确改为退货或换货则按新类型填写，不沿用旧退款确认。
客服说明旧退款确认因政策变化已失效并要求重新核验后，用户回复“重新核验”属于同一退款办理诉求，intent 为 refund_return、goal 为 create_ticket、refund_consent 为 none；这不是提交确认。用户回复取消办理则 refund_consent 为 decline。

只有用户明确要求本轮办理退款退货、投诉处理、登记工单或进入人工队列时，才填写 action_quote：从本轮用户消息逐字摘录表达该操作的完整短句。查询政策、已有申请或工单进度时填 null；不能把另一项请求的操作短句借给当前项。

按可独立完成的目标拆分：同一消息中的订单、物流、申请进度、政策、工单进度及人工诉求分别保留。若后一项通过列表序号引用前一项要展示的订单，按顺序分别输出列单和后续查询；该项填 listed_selection、list_index 和 reference_quote，order_no 留空。每项的 completed_question 只保留自己的诉求。若一项缺订单号，仍将其他可处理项独立输出。

只从本轮用户原文抄录完整申请号、工单号。订单号只可来自本轮原文或最近一条服务端订单列表的唯一选择；列表序号或商品名选择用 listed_selection，并摘录对应的 reference_quote。latest 仅限本轮明确说最近一笔，须摘录 reference_quote。只给订单尾号时，填写 order_suffix 和 reference_quote，不要当成完整订单号或列表序号。其他情况这些辅助字段填 null。补全问题只写该项诉求，不合并其他请求。低置信度项也必须保留并标为 other 或给低 cofidence，不可悄悄省略。只输出合法 JSON。"""

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
