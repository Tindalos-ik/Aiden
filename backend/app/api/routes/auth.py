from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status

from app.api.deps import current_actor
from app.api.schemas import LoginRequest
from app.config.settings import settings
from app.persistence.mysql.chat import create_session, delete_session, verify_user


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, response: Response) -> dict:
    """验证普通用户并签发 HttpOnly Cookie；Cookie 明文不会写入 MySQL。"""
    if body.role == "staff":
        # 当前没有人工工作台，因此拒绝员工登录，避免把演示入口伪装成已接通。
        raise HTTPException(status_code=403, detail="第一版尚未接入人工客服工作台，请选择普通用户登录")
    actor = verify_user(body.account, body.password, body.role)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token, _ = create_session(actor["id"])
    # 浏览器自动携带此 Cookie；生产环境可通过配置强制 Secure，开发环境默认走 HTTP。
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return actor


@router.get("/me")
def me(actor: dict = Depends(current_actor)) -> dict:
    """返回认证依赖已经验证过的当前用户公开资料。"""
    return actor


@router.post("/logout", status_code=204)
def logout(response: Response, session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> Response:
    """按 Cookie 对应的令牌哈希撤销服务端会话，并让浏览器删除 Cookie。"""
    delete_session(session_token)
    response.delete_cookie(key=settings.session_cookie_name, path="/", httponly=True, samesite="lax")
    response.status_code = 204
    return response
