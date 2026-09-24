"""后端进程配置。

仅加载模型和 Cookie 相关设置；MySQL DATABASE_URL 由数据库模块按需从进程环境读取，
避免在模块导入时建立连接或把数据库凭据复制到配置对象。
"""

import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")


class Settings:
    """集中提供运行时环境变量和对话上下文上限。"""

    # OpenAI 兼容服务凭据和模型名；Base URL 为空时使用 SDK 默认地址。
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "").strip()
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "aiden_session").strip()
    session_cookie_secure: bool = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
    # MySQL URL 由数据库模块从环境读取；这里仅保留 Cookie 的本地/部署配置。
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", str(60 * 60 * 24 * 7)))
    # 三层上下文限制：历史条数、单条截断长度和最终送入模型的总字符数。
    max_history_messages: int = 16
    max_message_chars: int = 2000
    max_context_chars: int = 12000


settings = Settings()
