from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class SupportState(TypedDict, total=False):
    """LangGraph 节点间传递的最小状态，不把 ORM Session 放入图状态。

    messages 由 LangGraph 消息 reducer 追加模型与工具消息；用户身份只由服务端路由
    写入 user_id，再由工具节点注入只读工具，绝不属于模型可填写的工具参数。
    """
    # 当前请求目标会话，所有 SQL 查询仍需同时校验其所有者。
    conversation_id: str
    # 预先创建的助手占位记录 ID，用于完成时更新同一行。
    assistant_message_id: str
    # System Prompt、有限历史、模型工具调用和工具结果构成的对话消息序列。
    messages: Annotated[list[BaseMessage], add_messages]
    # 模型节点生成并交给保存节点的最终回答。
    answer: str
    # 本轮已检索证据的编号映射；仅最终回答实际引用的项会落到消息行。
    citations: list[dict[str, object]]
    # 知识不足时与回答在同一事务入低置信度问题池。
    low_confidence: dict[str, str]
    # 已认证用户 ID，供上下文查询和写回答时做归属校验。
    user_id: str
    # 当前请求的受限语义字段；完整有序列表与已完成结果分开保存。
    semantic_result: dict[str, object]
    requests: list[dict[str, object]]
    request_index: int
    request_results: list[dict[str, object]]
    # 当前项是否明确要求实时人工；最终回答入库时才提交状态转换。
    handoff_requested: bool
    handoff_required: bool
    request_tool_start: int
    query_cache: list[dict[str, str]]
    pending_cache_key: str
    cached_tool_hit: bool
    # 服务端依据固定意图集合选出的工具名；严禁直接采用模型返回的工具名。
    allowed_tools: list[str]
    # 传给最终回答模型的补全问题，不改写数据库中的原始用户消息。
    completed_question: str
    # 服务端可选的安全固定答复，经 generate 节点发送并保存。
    direct_reply: str
    # 先列本人近期订单或核验跨轮选择时，标记前置订单查询是否尚待处理。
    order_lookup_pending: bool
    order_lookup_mode: str
    # 用户从最近订单列表中选择的候选；下一步必须由本人近期订单结果再次核验。
    selection_order_no: str
    # 只允许对本轮本人订单查询结果中确定的订单发起详情或物流查询。
    authorized_order_no: str
