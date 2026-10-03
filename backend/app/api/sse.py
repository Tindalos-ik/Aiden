import json
from typing import Any


_TOOL_PROGRESS = {
    "query_order": ("query", "正在查询本人订单"),
    "query_logistics": ("query", "正在查询物流进度"),
    "query_after_sale": ("query", "正在查询售后进度"),
    "query_refund_policy": ("query", "正在查询适用售后政策"),
    "query_ticket": ("query", "正在查询工单进度"),
    "submit_after_sale": ("query", "正在提交已确认的售后申请"),
    "create_ticket": ("query", "正在登记工单"),
    "search_faq": ("retrieval", "正在检索知识库证据"),
}


def progress_payload(event: dict[str, Any]) -> dict[str, str] | None:
    """只转发本图实际阶段与顶层工具调用，不映射嵌套模型或缓存节点。

    进度不包含模型草稿、工具参数或原始结果；同阶段可因不同诉求真实执行多次。
    """
    node = event.get("metadata", {}).get("langgraph_node")
    name = event.get("name")
    if event.get("event") == "on_custom_event" and name == "support_progress":
        if node in {"recognize_intent", "assess_knowledge", "generate"}:
            data = event.get("data", {})
            return {"stage": data["stage"], "message": data["message"]}
    if event.get("event") == "on_tool_start" and name in _TOOL_PROGRESS:
        if node == f"run_{name}":
            stage, message = _TOOL_PROGRESS[name]
            return {"stage": stage, "message": message}
    return None


def sse_event(name: str, payload: dict[str, Any]) -> str:
    """编码单条 SSE 事件，保持 remote.ts 所需的 event/data 双行格式。

    JSON 紧凑编码可安全承载换行和中文；末尾空行是 SSE 的事件边界，不能省略。
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n"
