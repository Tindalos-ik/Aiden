import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.agent.state import SupportState
from app.config.settings import settings
from app.persistence.mysql.chat import finish_assistant_message, recent_messages


SYSTEM_PROMPT = """你是「喵购商城」的智能客服小喵，语气亲切、回答简洁，不用网络烂梗。

你的职责范围：商品咨询、订单与物流查询、退换货政策解答。

必须遵守：
1. 只依据商城政策作答，拿不准就说「这边帮您转人工核实」，严禁编造；
2. 不承诺具体的退款到账时间，不对商品质量打包票；
3. 用户聊到职责之外的话题，礼貌把话拉回客服业务。

当前部署没有接入订单、物流、售后申请或商城政策数据，也没有接入人工客服。对具体订单状态、物流轨迹、退款进度或商城政策条款，必须明确说当前无法核实，绝不能猜测或编造编号、状态、时间、进度或政策内容，也不能声称已转接人工。可以简短邀请用户稍后提供一般商品咨询。不得把不存在的数据说成已查询结果。"""


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


def needs_grounded_data_guard(text: str) -> bool:
    """保守识别需要订单、物流、售后或政策数据支撑的问题。

    这些数据源尚未接入；命中时仍执行真实模型调用，但不向用户转发模型可能作出
    的无依据断言，最终改用明确的“当前无法核实”说明。
    """
    normalized = text.casefold()
    return any(term in normalized for term in (
        "订单", "物流", "快递", "运单", "发货", "配送", "签收", "退款", "退货", "售后",
        "到账", "进度", "政策", "规则", "运费险", "退换", "保修", "包裹", "单号",
        "order", "tracking", "shipment", "shipping", "delivery", "refund", "return",
        "policy", "after-sale", "package",
    )) or bool(re.search(r"\b(?:SO|AD)-?\d{6,}\b", text, re.IGNORECASE))


def _message_for_row(role: str, content: str) -> BaseMessage | None:
    """把数据库角色映射到 LangChain 消息类型，跳过不支持的角色值。"""
    if role == "user":
        return HumanMessage(content=content)
    if role in {"assistant", "staff"}:
        return AIMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    return None


def _content_as_text(content: Any) -> str:
    """提取模型文本内容，兼容字符串和多模态响应中的文本块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def build_support_graph(user_text: str, guard_sensitive_data: bool):
    """构建一次请求使用的 LangGraph：加载上下文、调用模型、保存最终回答。

    节点按顺序执行。数据库节点只在自身短事务中读取或更新，并返回普通 Python
    状态；模型的异步流式 await 期间不会持有同步 SQLAlchemy Session。
    """
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

    async def load_context(state: SupportState) -> dict[str, Any]:
        """加载当前用户最近的有限历史，并在模型 await 前关闭 MySQL Session。"""
        rows = recent_messages(
            state["conversation_id"], state["user_id"], state["assistant_message_id"]
        )
        history = [msg for row in rows if (msg := _message_for_row(row["role"], row["content"])) is not None]
        return {"context_messages": [SystemMessage(content=SYSTEM_PROMPT), *history]}

    async def generate(state: SupportState) -> dict[str, str]:
        """调用 OpenAI 兼容聊天模型并汇总 token 片段供图状态和 SSE 使用。"""
        parts: list[str] = []
        async for chunk in model.astream(state["context_messages"]):
            parts.append(_content_as_text(chunk.content))
        model_answer = "".join(parts).strip()
        if not model_answer:
            raise RuntimeError("模型返回了空回答，请检查模型名称和接口配置。")

        # 所有轮次仍真实调用模型；需要实时账户或政策数据的问题不暴露无依据的模型断言，
        # 而是返回明确的当前数据接入状态说明。
        answer = (
            "为避免误导，我目前没有接入订单、物流、售后或商城政策数据，因此无法核实你提到的具体信息，也不能确认状态或进度。"
            if guard_sensitive_data else model_answer
        )
        return {"answer": answer}

    def save_answer(state: SupportState) -> dict[str, str]:
        """模型完成后用独立短事务保存完整回答，并将助手消息状态设为 complete。"""
        answer = state["answer"]
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
    graph.add_node("generate", generate)
    graph.add_node("save_answer", save_answer)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "generate")
    graph.add_edge("generate", "save_answer")
    graph.add_edge("save_answer", END)
    return graph.compile()
