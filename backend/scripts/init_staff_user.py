"""人工显式创建一个员工登录账号；应用启动时不会调用本脚本。

在 backend 目录运行 ``python -m scripts.init_staff_user``。账号信息只从运行环境
读取，脚本不显示密码，不覆盖或重置已有用户。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets

from sqlalchemy import func, select

from app.config import settings as _settings  # noqa: F401 读取本地 backend/.env
from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import User


def create_staff_user() -> str:
    """仅新增不存在的 staff；邮箱冲突时整个事务不写入任何用户数据。"""
    email = os.getenv("AIDEN_STAFF_EMAIL", "").strip().lower()
    name = os.getenv("AIDEN_STAFF_NAME", "").strip()
    password = os.getenv("AIDEN_STAFF_PASSWORD", "")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 255:
        raise ValueError("请设置有效的 AIDEN_STAFF_EMAIL")
    if not name or len(name) > 100:
        raise ValueError("请设置 1–100 字符的 AIDEN_STAFF_NAME")
    if len(password) < 12:
        raise ValueError("AIDEN_STAFF_PASSWORD 至少需要 12 个字符")

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000).hex()
    with get_session_factory().begin() as session:
        if session.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
            raise ValueError("该邮箱已存在；不会覆盖已有用户或密码")
        session.add(User(name=name, email=email, role="staff", password_salt=salt, password_hash=digest))
        session.flush()
    return email


if __name__ == "__main__":
    try:
        created_email = create_staff_user()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"员工账号已创建：{created_email}。密码未显示或保存为明文。")
