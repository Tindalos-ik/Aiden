import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agent.graph import build_support_graph
from app.agent.models import model_configuration_error
from app.api.deps import current_actor
from app.api.schemas import (
    CreateConversationRequest, HumanMessageRequest, MessageFeedbackRequest, StreamMessageRequest,
)
from app.api.sse import progress_payload, sse_event
from app.persistence.mysql.chat import (
    create_conversation as create_mysql_conversation,
    create_message_pair,
    finish_assistant_message,
    get_owned_conversation,
    list_owned_messages,
    list_user_conversations,
    submit_message_feedback,
)
from app.persistence.mysql.human_support import request_handoff, send_human_message


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("/{conversation_id}/handoff")
def handoff(conversation_id: str, actor: dict = Depends(current_actor)) -> dict[str, Any]:
    """用户申请实时人工队列，和登记工单是独立操作。"""
    try:
        return request_handoff(conversation_id, actor["id"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{conversation_id}/messages")
def send_user_human_message(conversation_id: str, body: HumanMessageRequest,
                            actor: dict = Depends(current_actor)) -> dict[str, Any]:
    """waiting/staff 阶段在原会话写入用户留言。"""
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="消息内容不能为空")
    try:
        return send_human_message(conversation_id, actor["id"], "user", body.text.strip())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
def conversations(actor: dict = Depends(current_actor)) -> list[dict[str, Any]]:
    """只列出当前 Cookie 用户拥有的会话。"""
    return list_user_conversations(actor["id"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_conversation(
    _body: CreateConversationRequest, actor: dict = Depends(current_actor)
) -> dict[str, Any]:
    """创建新的 MySQL 会话，并保持 remoteApi 需要的 201 响应契约。"""
    return create_mysql_conversation(actor["id"])


@router.get("/{conversation_id}/messages")
def messages(conversation_id: str, actor: dict = Depends(current_actor)) -> list[dict[str, Any]]:
    """读取历史时由仓储在 SQL 条件中同时校验会话 ID 和用户 ID。"""
    owned, result = list_owned_messages(conversation_id, actor["id"])
    if not owned:
        raise HTTPException(status_code=404, detail="会话不存在")
    return result


@router.post("/{conversation_id}/messages/{message_id}/feedback")
def message_feedback(
    conversation_id: str,
    message_id: str,
    body: MessageFeedbackRequest,
    actor: dict = Depends(current_actor),
) -> dict[str, str]:
    """反馈只绑定当前用户会话中已完成的助手回答；相同评分重试幂等。"""
    try:
        return submit_message_feedback(conversation_id, actor["id"], message_id, body.rating)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话或消息不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    body: StreamMessageRequest,
    actor: dict = Depends(current_actor),
) -> StreamingResponse:
    """先原子落库用户消息和助手占位行，再在无 Session 的状态下生成 SSE。

    start 的 messageId 是 MySQL 助手行主键；delta 是未核验的实时正文草稿，
    final 是核验并落库后的权威正文和引用。error/done 互斥；断线只收尾数据库。
    """
    conversation = get_owned_conversation(conversation_id, actor["id"])
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="消息内容不能为空")
    try:
        pair = create_message_pair(conversation_id, actor["id"], text, body.clientMessageId)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc

    async def event_stream():
        answer_so_far = ""
        finished = False
        try:
            # start 也放进 try/finally；客户端若在收到首个事件后立即断开，仍会标记 stopped。
            yield sse_event("start", {"messageId": pair.assistant_message_id})

            # 完全相同的重试直接重放数据库中已完成的回答，不再插入第二条用户消息。
            if not pair.should_generate:
                # 重放不拥有这条已完成记录的状态，播放中断也不能把原回答改成 stopped。
                finished = True
                yield sse_event("final", {
                    "text": pair.replay_content, "citations": pair.replay_citations or [],
                })
                yield sse_event("done", {})
                return

            config_error = model_configuration_error()
            if config_error:
                finish_assistant_message(
                    pair.assistant_message_id,
                    conversation_id,
                    actor["id"],
                    config_error,
                    "error",
                )
                finished = True
                yield sse_event("error", {"error": config_error})
                return

            try:
                graph = build_support_graph()
                initial_state = {
                    "conversation_id": conversation_id,
                    "assistant_message_id": pair.assistant_message_id,
                    "user_id": actor["id"],
                }
                async for event in graph.astream_events(initial_state, version="v2"):
                    progress = progress_payload(event)
                    if progress is not None:
                        yield sse_event("progress", progress)
                    if (event.get("event") == "on_custom_event"
                            and event.get("name") == "support_answer_delta"
                            and event.get("metadata", {}).get("langgraph_node") == "generate"
                            and not finished):
                        delta = event.get("data", {}).get("text")
                        if isinstance(delta, str) and delta:
                            yield sse_event("delta", {"text": delta})
                    if (event.get("event") == "on_chain_end" and event.get("name") == "save_answer"
                            and event.get("metadata", {}).get("langgraph_node") == "save_answer"
                            and not finished):
                        output = event.get("data", {}).get("output", {})
                        answer = output.get("answer") if isinstance(output, dict) else None
                        if isinstance(answer, str):
                            answer_so_far = answer
                            # 保存已完成；从此不再拥有取消收尾权，避免迟到断线降级 complete。
                            finished = True
                            yield sse_event("final", {
                                "text": answer, "citations": output.get("citations", []),
                            })
                            if output.get("handoff_committed"):
                                yield sse_event("handoff", {})

                if not answer_so_far:
                    raise RuntimeError("模型没有生成可显示的回答。")
                finished = True
                yield sse_event("done", {})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Agent failed for conversation %s", conversation_id)
                raw_detail = str(exc).strip().lower()
                if "api_key" in raw_detail or "api key" in raw_detail or "authentication" in raw_detail:
                    detail = "模型认证失败：请检查后端模型 API Key 配置。"
                elif "connection error" in raw_detail or "connecterror" in raw_detail:
                    detail = "模型连接失败：请检查后端模型 Base URL 和本机网络连接。"
                elif "404" in raw_detail or "model_not_found" in raw_detail:
                    detail = "模型不可用：请检查后端配置的模型名称。"
                else:
                    detail = "模型暂时无法回答，请检查服务日志后重试。"
                finish_assistant_message(
                    pair.assistant_message_id,
                    conversation_id,
                    actor["id"],
                    detail,
                    "error",
                )
                finished = True
                yield sse_event("error", {"error": detail})
        finally:
            # 草稿不落库：取消前未收到保存结果时仍用空正文 stopped；仓储终态闸门
            # 保护已提交但保存事件尚未送达的 complete，不会被迟到取消降级。
            if not finished:
                finish_assistant_message(
                    pair.assistant_message_id,
                    conversation_id,
                    actor["id"],
                    answer_so_far,
                    "stopped",
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
