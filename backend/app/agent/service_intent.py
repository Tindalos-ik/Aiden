"""售后、政策、投诉和人工请求的结构化目标。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ServiceExtraction(BaseModel):
    """只提取业务目标和用户明确给出的编号，不决定工具权限。"""

    model_config = ConfigDict(extra="forbid")

    goal: Literal["policy", "after_sale_status", "ticket_status", "create_ticket"] = Field(
        description="查询政策、售后申请、工单进度，或登记人工处理工单。"
    )
    completed_question: str = Field(min_length=1, max_length=1000)
    order_no: str | None = Field(default=None, max_length=64)
    request_no: str | None = Field(default=None, max_length=64)
    ticket_no: str | None = Field(default=None, max_length=64)


SERVICE_EXTRACTION_PROMPT = """你是电商客服请求目标识别器。根据本轮用户消息和必要的近期上下文，只输出符合 Schema 的 JSON。

goal 只能选以下四项：
- policy：询问退款、退货、换货、保修等规则或条件。
- after_sale_status：询问已经提交的退款、退货、换货申请状态、金额或处理结果。
- ticket_status：询问已经登记的投诉或人工处理工单进度。
- create_ticket：要求投诉处理、申请退款退货或其他尚未登记的售后处理；退款办理仍须先核对订单和政策、取得单独的登记确认。

本系统只能登记待处理工单，不能直接创建退款退货申请或实时接入人工。用户希望办理退款或退货时选 create_ticket，但这只是进入后续核对流程，并不表示本轮可以写入；说“退款申请处理到哪了”时选 after_sale_status。明确要查政策时优先 policy；明确询问已有工单时选 ticket_status。

completed_question 用一句话补全当前问题，不添加用户没说的事实。order_no、request_no、ticket_no 仅在本轮用户原文明确提供对应完整编号时填写；不要把订单号误当申请号或工单号，也不要从模型猜测、助手文字或工具结果中补造。没有完整编号填 null。不要输出工具名、用户身份、分析或解释。"""
