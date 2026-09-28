from fastapi import Cookie, Depends, HTTPException, status

from app.config.settings import settings
from app.persistence.mysql.chat import get_user_for_session


def current_identity(session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    """校验项目现有 Cookie 会话，返回公开身份字段。"""
    actor = get_user_for_session(session_token)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return actor


def current_actor(actor: dict = Depends(current_identity)) -> dict:
    """普通用户对话路由还必须按 actor.id 过滤其数据归属。"""
    if actor["role"] != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前版本仅开放普通用户对话")
    return actor


def current_staff(actor: dict = Depends(current_identity)) -> dict:
    """员工路由仅允许持有有效 Cookie 且角色为 staff 的员工。"""
    if actor["role"] != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅员工可访问此功能")
    return actor
