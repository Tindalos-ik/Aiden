from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求体；role 同时用于员工和普通用户登录校验。"""
    account: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    role: Literal["user", "staff"] = "user"


class CreateConversationRequest(BaseModel):
    """新建会话的请求体占位；当前前端不需要传入额外字段。"""
    pass


class StreamMessageRequest(BaseModel):
    """流式发送请求；幂等键长度按前端契约校验，不用作数据库消息主键。"""
    text: str = Field(min_length=1, max_length=4000)
    clientMessageId: str | None = Field(default=None, min_length=1, max_length=100)


class HumanMessageRequest(BaseModel):
    """人工阶段的普通留言；不触发智能客服生成。"""
    text: str = Field(min_length=1, max_length=4000)


class MessageFeedbackRequest(BaseModel):
    """只接受评价结果，问题正文与触发入口由后端根据已保存消息决定。"""
    rating: Literal["satisfied", "unsatisfied"]
