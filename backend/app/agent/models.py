from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from app.config.settings import settings


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


def create_models(
    *, evaluation: dict[str, Any] | None = None,
) -> tuple[ChatOpenAI, Runnable[LanguageModelInput, BaseMessage]]:
    """按运行配置创建流式答复与非流式 JSON 模型。

    沿用思考模式与 Langfuse usage 设置；评估模式额外开启答复 usage，
    但不在这里放宽工具写入权限。客户端创建不发起模型请求。
    """
    kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "api_key": settings.openai_api_key,
        "temperature": 0.2,
        "streaming": True,
        # 兼容接口的流式响应需要显式请求 usage，回调才能拿到准确的 token 消耗。
        "stream_usage": settings.langfuse_enabled or evaluation is not None,
        "max_tokens": 800,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    # DeepSeek 等思考型模型下，带 tools 的请求必须回传历史 reasoning_content，而本图的工具调用
    # 是服务端合成的、不带该字段，会让“工具结果后再生成回答”这一步被服务端拒绝（400）。
    # OPENAI_THINKING_MODE=disabled 可关闭思考模式绕开该约束；未配置时不传此参数，
    # 保持服务端默认行为。详见 app.config.settings 中的说明。
    thinking_body = settings.openai_thinking_body
    if thinking_body:
        kwargs["extra_body"] = thinking_body
    model = ChatOpenAI(**kwargs)
    # 结构化识别不向客户端逐字输出；非流式 JSON 请求降低兼容模型只返回空白片段的概率。
    json_model = ChatOpenAI(**{**kwargs, "streaming": False, "stream_usage": False}).bind(
        response_format={"type": "json_object"}
    )
    return model, json_model
