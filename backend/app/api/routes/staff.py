"""站内人工客服工作台接口；全部使用现有员工 Cookie 身份。"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import current_staff
from app.api.schemas import HumanMessageRequest
from app.persistence.mysql.human_support import (
    accept_conversation, close_conversation, list_staff_messages,
    list_staff_queue, send_human_message,
)

router = APIRouter(prefix="/api/staff", tags=["staff"])


@router.get("/queue")
def queue(actor: dict = Depends(current_staff)) -> list[dict[str, Any]]:
    return list_staff_queue(actor["id"])


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: str, actor: dict = Depends(current_staff)) -> list[dict[str, Any]]:
    allowed, result = list_staff_messages(conversation_id, actor["id"])
    if not allowed:
        raise HTTPException(status_code=404, detail="会话不存在")
    return result


@router.post("/conversations/{conversation_id}/accept")
def accept(conversation_id: str, actor: dict = Depends(current_staff)) -> dict[str, Any]:
    try:
        return accept_conversation(conversation_id, actor["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/messages")
def send_message(conversation_id: str, body: HumanMessageRequest,
                 actor: dict = Depends(current_staff)) -> dict[str, Any]:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="消息内容不能为空")
    try:
        return send_human_message(conversation_id, actor["id"], "staff", body.text.strip())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/close")
def close(conversation_id: str, actor: dict = Depends(current_staff)) -> dict[str, Any]:
    try:
        return close_conversation(conversation_id, actor["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
