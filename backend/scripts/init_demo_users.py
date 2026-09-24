"""显式初始化本地两个普通用户演示账号，不在服务启动时重置任何用户数据。"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import func, select

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import User


DEMO_USERS = (
    ("user-maya", "林沐言", "maya@aiden.demo", "aiden123"),
    ("user-chen", "陈屿", "chen@aiden.demo", "aiden123"),
)


def password_hash(password: str, salt: str) -> str:
    """使用 PBKDF2-SHA256 计算演示账号密码摘要，不保存或回显明文密码。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000
    ).hex()


def initialize_demo_users() -> tuple[int, int]:
    """只新增完全不存在的演示身份；身份冲突时整个事务回滚。

    已有账号必须同时匹配 ID、邮箱、姓名和普通用户角色，且已存在密码盐与密码哈希，
    才会被视为已经初始化。现有密码不重新生成或覆盖。
    """
    added = 0
    unchanged = 0
    with get_session_factory().begin() as session:
        pending: list[User] = []
        for user_id, name, email, password in DEMO_USERS:
            by_id = session.get(User, user_id)
            by_email = session.scalar(
                select(User).where(func.lower(User.email) == email.lower())
            )
            if by_id is not None or by_email is not None:
                if (
                    by_id is None
                    or by_email is None
                    or by_id.id != by_email.id
                    or by_id.name != name
                    or by_id.role != "user"
                    or not by_id.password_salt
                    or not by_id.password_hash
                ):
                    raise RuntimeError(
                        f"演示账号 {email} 与 MySQL 中已有用户冲突；未写入任何账号。"
                    )
                unchanged += 1
                continue

            salt = secrets.token_hex(16)
            pending.append(
                User(
                    id=user_id,
                    name=name,
                    email=email,
                    role="user",
                    password_salt=salt,
                    password_hash=password_hash(password, salt),
                )
            )
        session.add_all(pending)
        session.flush()
        added = len(pending)
    return added, unchanged


if __name__ == "__main__":
    try:
        created, kept = initialize_demo_users()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"演示用户初始化完成：新增 {created}，保留未改动 {kept}。")
    print("本地登录账号密码均为 aiden123。")
