from fastapi import Cookie, Depends, HTTPException, status

from app.config.settings import settings
from app.persistence.mysql.chat import get_user_for_session


def current_actor(session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    """把已登录的普通用户注入路由，并统一拒绝无效 Cookie 和员工账号。

    仓储只保存令牌哈希；这里将浏览器发来的 Cookie 交给 MySQL 仓储校验，
    路由随后必须把 actor.id 传入所有会话读写函数做归属过滤。
    """
    actor = get_user_for_session(session_token)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if actor["role"] != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前版本仅开放普通用户对话")
    return actor
