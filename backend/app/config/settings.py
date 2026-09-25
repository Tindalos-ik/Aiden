"""后端进程配置。

仅加载模型和 Cookie 相关设置；MySQL DATABASE_URL 由数据库模块按需从进程环境读取，
避免在模块导入时建立连接或把数据库凭据复制到配置对象。
"""

import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")

# OPENAI_THINKING_MODE 的合法取值：enabled / disabled。
# 这两个词直接进 OpenAI 兼容请求体的 thinking.type，因此不做大小写或同义词兼容，
# 写错就在启动时报错，避免带着一个无效值静默运行。
_THINKING_MODES = ("enabled", "disabled")


class Settings:
    """集中提供运行时环境变量和对话上下文上限。"""

    # OpenAI 兼容服务凭据和模型名；Base URL 为空时使用 SDK 默认地址。
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "").strip()
    # 思考模式开关。留空表示不传该参数，由服务端默认行为决定（DeepSeek 目前默认开启）。
    #
    # 为什么在线 Agent 需要它：DeepSeek 文档规定，携带 tools 参数的请求“必须完整回传
    # reasoning_content，否则返回 400”。而本项目的工具调用是服务端合成的
    # （dispatch_tool_call 构造 AIMessage），这条合成消息不带 reasoning_content，
    # 于是“工具结果之后再由模型生成回答”这一步必然被拒。关掉思考模式后模型不再产生
    # reasoning_content，该约束自然消失；同时省掉思维链的 token 开销。
    #
    # 只作用于在线对话（agent/graph.py 的 ChatOpenAI）。离线对话挖掘走自己的裸 OpenAI
    # 客户端（services/rag/extraction.py，读 MINING_LLM_*），不受这里影响。
    openai_thinking_mode: str = os.getenv("OPENAI_THINKING_MODE", "").strip().lower()
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "aiden_session").strip()
    session_cookie_secure: bool = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
    # MySQL URL 由数据库模块从环境读取；这里仅保留 Cookie 的本地/部署配置。
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 7)))
    # 三层上下文限制：历史条数、单条截断长度和最终送入模型的总字符数。
    max_history_messages: int = 16
    max_message_chars: int = 2000
    max_context_chars: int = 12000

    @property
    def openai_thinking_body(self) -> dict[str, dict[str, str]] | None:
        """构造请求体里的 thinking 参数；未配置或取值非法时返回 None。

        返回 None 表示调用方不应传该参数，保持服务端默认行为，而不是硬编码一个猜测值。
        """
        if not self.openai_thinking_mode:
            return None
        if self.openai_thinking_mode not in _THINKING_MODES:
            raise ValueError(
                f"OPENAI_THINKING_MODE 只能是 {' 或 '.join(_THINKING_MODES)}，"
                f"当前值：{self.openai_thinking_mode!r}"
            )
        return {"thinking": {"type": self.openai_thinking_mode}}


settings = Settings()
