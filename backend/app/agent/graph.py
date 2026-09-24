from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.state import SupportState
from app.config.settings import settings
from app.persistence.mysql.chat import finish_assistant_message, recent_messages
from app.services.tools import READ_ONLY_CUSTOMER_TOOLS


# FAQ 就是常见问题
SYSTEM_PROMPT = """你是「喵购商城」的智能客服 Aiden，语气亲切、回答简洁，不用网络烂梗。

你的职责范围：商品咨询、订单与物流查询、退换货政策解答。

工具使用和事实约束：
1. 订单内容或状态必须调用 query_order；用户给出订单号时按该订单号查询，没有指定时可查询当前账号最近订单。
2. 物流必须调用 query_logistics，并使用当前账号下的订单号；没有订单号且无法从近期订单确定时，先向用户询问。
3. 常见问题必须调用 search_faq。只能根据工具返回的 FAQ 问题、答案和分类回答，不要用模型记忆补写商城政策。
4. 只转述工具返回的数据。订单或物流未找到时，如实说明当前账号下没有匹配记录；FAQ 无匹配时说明没有检索到相关条目；工具报告查询错误时说明暂时无法查询。
5. 工具未覆盖的售后申请进度、退款到账或政策条款不得猜测；FAQ 没有提供依据时，明确说当前无法核实。
6. 不编造订单、包裹、物流节点、FAQ 答案、时间或承诺；不声称已转接人工客服。
7. 用户聊到职责之外的话题时，礼貌地把话题引回客服业务。
8. 只依据商城政策作答，拿不准就说「这边帮您转人工核实」，严禁编造；"""


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
    if role in {"assistant", "staff"}: # 客服和人工客服都算作助手
        return AIMessage(content=content)
    if role == "system": # 系统消息
        return SystemMessage(content=content)
    return None


def _content_as_text(content: Any) -> str:
    """提取模型文本内容，兼容字符串和多模态响应中的文本块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def build_support_graph():
    """构建带只读工具循环的单次客服图，并保持每次数据库访问的 Session 生命周期短暂。"""
    kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.2,
        "streaming": True,
        "max_tokens": 800,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    model = ChatOpenAI(**kwargs).bind_tools(READ_ONLY_CUSTOMER_TOOLS)
    tool_node = ToolNode(
        READ_ONLY_CUSTOMER_TOOLS,
        # 工具函数会返回已清理的错误结果；未预期异常交由 SSE 路由转换为通用错误。
        handle_tool_errors=False,
    )

    async def load_context(state: SupportState) -> dict[str, Any]:
        """加载当前用户最近的有限历史，并在模型 await 前关闭 MySQL Session。"""
        rows = recent_messages(
            state["conversation_id"], state["user_id"], state["assistant_message_id"]
        )
        history = [
            msg
            for row in rows
            if (msg := _message_for_row(row["role"], row["content"])) is not None
        ]
        return {"messages": [SystemMessage(content=SYSTEM_PROMPT), *history]}

    async def generate(state: SupportState) -> dict[str, Any]:
        """累积一次模型响应；工具调用轮不作为最终回答写入或流给客户端。"""
        response = None
        async for chunk in model.astream(state["messages"]):
            response = chunk if response is None else response + chunk
        if response is None:
            raise RuntimeError("模型没有生成回答。")

        has_tool_calls = bool(getattr(response, "tool_calls", []))
        answer = "" if has_tool_calls else _content_as_text(response.content).strip()
        return {"messages": [response], "answer": answer}

    def route_after_generate(state: SupportState) -> str:
        """只在模型明确生成工具调用时执行工具，其余情况进入最终回答保存节点。"""
        message = state["messages"][-1]
        return "tools" if getattr(message, "tool_calls", []) else "save_answer"

    def save_answer(state: SupportState) -> dict[str, str]:
        """模型完成工具循环后，用独立短事务保存最后一轮完整回答。"""
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
    graph.add_node("generate", generate)
    graph.add_node("tools", tool_node)
    graph.add_node("save_answer", save_answer)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "generate")
    graph.add_conditional_edges(
        "generate",
        route_after_generate,
        {"tools": "tools", "save_answer": "save_answer"},
    )
    graph.add_edge("tools", "generate")
    graph.add_edge("save_answer", END)
    return graph.compile()
