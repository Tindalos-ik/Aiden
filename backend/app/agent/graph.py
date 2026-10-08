from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.models import create_models
from app.agent.nodes import tools
from app.agent.nodes.answer import make_generate
from app.agent.nodes.context import make_load_context, make_save_answer
from app.agent.nodes.intent import make_recognize_intent, route_intent
from app.agent.nodes.knowledge import _faq_tool_payload, make_assess_knowledge
from app.agent.nodes.request import finish_request
from app.agent.nodes.tools import after_tools, make_dispatch_tool_call
from app.agent.state import SupportState
from app.config.settings import settings


def build_support_graph(*, evaluation: dict[str, Any] | None = None):
    """构建真实客服图；评估只替换历史/保存边界，默认禁止业务写工具。"""
    model, json_model = create_models(evaluation=evaluation)
    # 分开的 ToolNode 让一次通过校验的调用只能到达对应工具。
    tool_nodes = {
        name: ToolNode([tool], handle_tool_errors=False)
        for name, tool in tools._TOOLS_BY_NAME.items()
    }

    load_context = make_load_context(evaluation)
    save_answer = make_save_answer(evaluation)
    recognize_intent = make_recognize_intent(json_model)
    dispatch_tool_call = make_dispatch_tool_call(evaluation)
    assess_knowledge = make_assess_knowledge(model, evaluation)
    generate = make_generate(model)

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
        return tools._TOOL_NODE_BY_NAME.get(allowed_names[0], "finish_request")

    def route_after_tools(state: SupportState) -> str:
        """列表选择核验可继续查询；其余结果先做知识校验再收尾当前项。"""
        if state.get("allowed_tools"):
            return "dispatch_tool_call"
        return "assess_knowledge" if _faq_tool_payload(
            state.get("messages", []), state.get("request_tool_start", 0)
        ) else "finish_request"

    def route_next_request(state: SupportState) -> str:
        return "route_intent" if state.get("request_index", 0) < len(state.get("requests", [])) else "generate"

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
    for tool_name, node_name in tools._TOOL_NODE_BY_NAME.items():
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
            **{node_name: node_name for node_name in tools._TOOL_NODE_BY_NAME.values()},
            "after_tools": "after_tools",
            "finish_request": "finish_request",
        },
    )
    for node_name in tools._TOOL_NODE_BY_NAME.values():
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
        from app.services.observability.callback import SupportCallbackHandler

        return compiled.with_config({
            "callbacks": [SupportCallbackHandler(update_trace=True)],
            "metadata": {"aiden_source": "evaluation" if evaluation is not None else "online"},
        })
    return compiled
