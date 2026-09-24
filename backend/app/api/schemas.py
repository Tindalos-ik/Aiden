from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """登录请求体；role 保持前端现有契约，服务当前只允许 user 登录。"""
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
