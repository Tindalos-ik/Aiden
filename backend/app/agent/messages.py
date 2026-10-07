from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage


def _message_for_row(role: str, content: str) -> BaseMessage | None:
    """把数据库角色映射到 LangChain 消息类型，跳过不支持的角色值。"""
    if role == "user":
        return HumanMessage(content=content)
    if role in {"assistant", "staff"}:  # 客服和人工客服都算作助手
        return AIMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    return None

def _content_as_text(content: Any) -> str:
    """提取模型或工具的文本内容，兼容字符串和多模态响应中的文本块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""

def _latest_user_text(messages: list[BaseMessage]) -> str:
    """取本轮原始用户消息；解析失败时仅用于安全兜底，不写回对话历史。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _content_as_text(message.content).strip()
    return ""

def _contains_exact_identifier(text: str, identifier: str) -> bool:
    """核对完整业务编号的原文来源，避免把更长编号的子串当作来源。"""
    if not text or not identifier:
        return False
    def is_identifier_char(char: str) -> bool:
        return char.isascii() and (char.isalnum() or char in "_-")

    offset = 0
    while (start := text.find(identifier, offset)) != -1:
        end = start + len(identifier)
        if (
            (start == 0 or not is_identifier_char(text[start - 1]))
            and (end == len(text) or not is_identifier_char(text[end]))
        ):
            return True
        offset = start + 1
    return False

def _valid_identifier(value: object) -> bool:
    """退款跨轮状态中的订单号与政策 ID 沿用订单列表的长度及控制字符边界。"""
    return (isinstance(value, str) and 0 < len(value) <= 64
            and value.strip() == value
            and not any(ord(char) < 32 or char in "\u2028\u2029" for char in value))
