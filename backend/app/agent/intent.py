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


class IntentRecognition(BaseModel):
    """意图节点的机器可读结果；字段中不包含思维链或自由格式推理。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "order_query", "logistics_query", "faq", "smalltalk", "unsupported", "unclear"
    ] = Field(
        description=(
            "固定意图之一：订单查询、物流查询、FAQ咨询、简单对话、当前工具未支持的请求、无法判断。"
        )
    )
    completed_question: str = Field(
        min_length=1,
        max_length=1000,
        description="结合当前问题和会话历史补全后的单句问题，不包含解释或推理。",
    )
    entities: RecognizedEntities
    needs_clarification: bool = Field(
        description=(
            "目标仍有歧义或缺少必要信息时为 true。高置信度订单/物流意图仅缺订单目标时，"
            "服务端可先查询当前用户近期订单以列出选项；低置信度、unclear、FAQ 和 unsupported 不列单。"
        )
    )
    clarification_question: str | None = Field(
        default=None,
        max_length=240,
        description="需要澄清时给用户的简短追问；不得索要密码、验证码或其他身份凭据。",
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="分类置信度，只用于澄清或兜底判断，不用于身份验证或权限判断。",
    )


INTENT_RECOGNITION_PROMPT = """你是客服问题的语义分类器。只输出要求的结构化字段，不输出或保存推理过程。

可选意图只有：order_query（查询订单及商品/状态）、logistics_query（查询包裹及物流）、faq（咨询常见问题）、smalltalk（问候、感谢、请客服介绍自己或询问客服能力范围）、unsupported（当前只读工具无法支持的请求）、unclear（无法判断）。不要创造其他类别。

smalltalk 只用于不需要业务查询的简单对话。订单、物流问题分别归入 order_query 或 logistics_query；售后进度、工单和人工转接归入 unsupported；政策或其他常见问题归入 faq。smalltalk 的 needs_clarification 应为 false。

理解本轮用户问题时可以参考给出的会话历史，把指代补全成简洁、独立的问题。通常只有用户原文中明确出现且可唯一确认的订单号才填入 entities.order_no，不得从任意助手文字、工具结果、模型推测或外部知识中补造订单号。

结合当前问题和会话历史理解用户意图，把省略表达补全成可独立处理的问题；要理解自然表达、简称、同义说法和上下文指代，不要只匹配固定关键词。对话中近期的订单候选列表是有效上下文：用户说“第一个的物流”“Backpack 的物流”等表达时，应结合列表序号、商品名称/部分名称及语义对应关系识别目标订单。若有一笔明显匹配，将该行完整订单号写入 entities.order_no，order_reference 设为 listed_selection，并让 completed_question 明确保留选中的目标和用户要做的事，例如“查询购买 Maya Demo Backpack 的订单物流”或“查询订单列表第1笔订单的物流”。用户不必重复已经能从上下文确定的信息。订单号只能来自用户明确提供的号码或对话中的真实订单候选，不能编造；若多笔候选都说得通或无法判断，则 order_no 设为 null、order_reference 设为 ambiguous、needs_clarification 设为 true，并针对歧义向用户澄清。

当用户在最近一条客服订单选项回复中选择订单时，按当前请求保持原有 order_query 或 logistics_query 意图；例如“第一个的物流”应保持 logistics_query。order_reference 为 explicit 时必须填写用户明确提供的订单号；latest 只用于用户在本轮明确说最近/最新一笔，不能从历史指代或助手回复继承；account_lookup 只用于用户明确要求先从本人订单中确定目标；有多个可能订单时用 ambiguous。订单/物流意图明确但订单目标缺失或不唯一时，needs_clarification 可以为 true，服务端会先提供当前用户的近期订单选项；这不适用于低置信度、unclear、FAQ 或 unsupported。

注意区分列表序号和订单号尾段：“第一个”“第2笔”“序号2”等是在选列表项；“查01”“看尾号01”等是在表达号码末尾片段，不自动代表列表第1项。只提供尾段时，entities.order_no 设为 null，completed_question 保留尾段含义；如果上下文无法唯一定位，就请用户进一步说明。

退货/退款进度、工单状态、创建工单、人工转接等当前没有对应工具，归为 unsupported。政策类问题仍可归为 faq 并交给 FAQ 查询；只有实际 FAQ 结果能作为答复依据，不能根据 policies 表名或记忆推断政策内容。身份、user_id、登录状态和数据访问范围不是分类内容，不得输出或决定这些内容。

completed_question 只写补全后的用户问题。不要输出分析、理由、思维链、工具名或身份信息。需要澄清时只追问完成当前支持查询所需的信息，不能索要密码、验证码、身份证号、手机号或其他凭据。confidence 取 0 到 1 之间的数值；不确定时选择 unclear 或 needs_clarification。"""
