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
