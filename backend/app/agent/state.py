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
    # 已认证用户 ID，供上下文查询和写回答时做归属校验。
    user_id: str
    # 语义识别节点的结构化字段；只保留意图、补全问题、实体、澄清标记和置信度。
    semantic_result: dict[str, object]
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
    # 非法工具调用被拒绝后，防止模型重新尝试并确保最终走安全答复。
    tool_rejected: bool
