from typing import TypedDict

from langchain_core.messages import BaseMessage


class SupportState(TypedDict, total=False):
    """LangGraph 节点间传递的最小状态，不把 ORM Session 放入图状态。

    conversation_id/user_id 用于在每个仓储读写点重新约束所有权；消息列表仅包含
    已裁剪的模型上下文，answer 保存最终文本以供持久化节点使用。
    """
    # 当前请求目标会话，所有 SQL 查询仍需同时校验其所有者。
    conversation_id: str
    # 预先创建的助手占位记录 ID，用于完成时更新同一行。
    assistant_message_id: str
    # System Prompt 与有限历史构成的模型输入消息序列。
    context_messages: list[BaseMessage]
    # 模型节点生成并交给保存节点的最终回答。
    answer: str
    # 已认证用户 ID，供上下文查询和写回答时做归属校验。
    user_id: str
