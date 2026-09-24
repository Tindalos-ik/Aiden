"""客服语义识别的结构化结果与固定意图集合。

识别器只理解当前问题和已加载的会话历史，不接收业务工具，也不返回推理过程。
工具权限由 graph.py 根据这些受限字段重新计算，模型不能自行指定工具名或用户身份。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecognizedEntities(BaseModel):
    """仅保留路由需要的订单引用；订单号必须来自当前问题或会话历史原文。"""

    model_config = ConfigDict(extra="forbid")

    order_no: str | None = Field(
        default=None,
        max_length=64,
        description="用户当前问题或会话历史中明确出现的订单号；没有时为 null。",
    )
    order_reference: Literal[
        "explicit", "latest", "account_lookup", "ambiguous", "none"
    ] = Field(
        description=(
            "订单指代类型：explicit 表示已识别出具体订单号；latest 表示用户本轮明确指定最近一笔；"
            "account_lookup 表示用户要求先从本人订单中确定；ambiguous 表示多个可能订单；none 表示无订单指代。"
        )
    )


class IntentRecognition(BaseModel):
    """意图节点的机器可读结果；字段中不包含思维链或自由格式推理。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "order_query", "logistics_query", "faq", "unsupported", "unclear"
    ] = Field(
        description=(
            "固定意图之一：订单查询、物流查询、FAQ咨询、当前工具未支持的请求、无法判断。"
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

可选意图只有：order_query（查询订单及商品/状态）、logistics_query（查询包裹及物流）、faq（咨询常见问题）、unsupported（当前只读工具无法支持的请求）、unclear（无法判断）。不要创造其他类别。

理解本轮用户问题时可以参考给出的会话历史。把“这个订单”“它到哪了”等指代补全成简洁、独立的问题；只有用户原文中明确出现且可唯一确认的订单号才填入 entities.order_no，不得从助手列出的选项、工具结果、模型推测或外部知识中补造订单号。用户在最近一条客服订单选项回复中按序号选择时，按上下文保持原有 order_query 或 logistics_query 意图；序号不能填入 order_no。order_reference 为 explicit 时必须填写用户明确提供的订单号；latest 只用于用户在本轮明确说最近/最新一笔，不能从历史指代或助手回复继承；account_lookup 只用于用户明确要求先从本人订单中确定目标；有多个可能订单时用 ambiguous。订单/物流意图明确但订单目标缺失或不唯一时，needs_clarification 可以为 true，服务端会先提供当前用户的近期订单选项；这不适用于低置信度、unclear、FAQ 或 unsupported。

当最近一条客服消息是“我找到您近期的订单”及其订单候选列表时，用户可能用完整订单号、列表序号或订单号末尾片段选择订单。完整订单号按完整号码识别；“第2笔”“序号2”“选第2个”等明确表达按列表序号识别；“查01”“看尾号01”等表达中的 01 是用户提供的订单号末尾片段，不是完整订单号，也不自动代表列表第 1 项。用户只提供末尾片段时，不得根据对话中的候选列表补造完整订单号；将 entities.order_no 设为 null，并在 completed_question 中保留该片段，例如“查询订单号末尾为01的订单”。保持上下文中的 order_query 或 logistics_query 意图。是否唯一匹配由服务端核对当前有效候选列表；模型不得自行选择订单。无法唯一匹配时，请用户说明完整订单号或列表序号。

退货/退款进度、工单状态、创建工单、人工转接等当前没有对应工具，归为 unsupported。政策类问题仍可归为 faq 并交给 FAQ 查询；只有实际 FAQ 结果能作为答复依据，不能根据 policies 表名或记忆推断政策内容。身份、user_id、登录状态和数据访问范围不是分类内容，不得输出或决定这些内容。

completed_question 只写补全后的用户问题。不要输出分析、理由、思维链、工具名或身份信息。需要澄清时只追问完成当前支持查询所需的信息，不能索要密码、验证码、身份证号、手机号或其他凭据。confidence 取 0 到 1 之间的数值；不确定时选择 unclear 或 needs_clarification。"""
