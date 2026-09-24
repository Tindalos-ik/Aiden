import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agent.graph import build_support_graph, model_configuration_error, needs_grounded_data_guard
from app.api.deps import current_actor
from app.api.schemas import CreateConversationRequest, StreamMessageRequest
from app.api.sse import sse_event
from app.persistence.mysql.chat import (
    create_conversation as create_mysql_conversation,
    create_message_pair,
    finish_assistant_message,
    get_owned_conversation,
    list_owned_messages,
    list_user_conversations,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


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


@router.post("/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    body: StreamMessageRequest,
    actor: dict = Depends(current_actor),
) -> StreamingResponse:
    """先原子落库用户消息和助手占位行，再在无 Session 的状态下生成 SSE。

    start 的 messageId 是 MySQL 助手行主键；delta 的 text 为每次新增文本；
    模型失败或配置缺失使用 error 事件，正常结束使用 done 事件，供 remote.ts 直接解析。
    """
    conversation = get_owned_conversation(conversation_id, actor["id"])
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    if conversation["status"] != "bot":
        raise HTTPException(status_code=409, detail="此会话当前不能发送智能客服消息")

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
                if pair.replay_content:
                    yield sse_event("delta", {"text": pair.replay_content})
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

            guarded = needs_grounded_data_guard(text)
            try:
                graph = build_support_graph(text, guarded)
                initial_state = {
                    "conversation_id": conversation_id,
                    "assistant_message_id": pair.assistant_message_id,
                    "user_id": actor["id"],
                }
                async for event in graph.astream_events(initial_state, version="v2"):
                    if (
                        event.get("event") == "on_chat_model_stream"
                        and event.get("metadata", {}).get("langgraph_node") == "generate"
                    ):
                        chunk = event.get("data", {}).get("chunk")
                        content = getattr(chunk, "content", "")
                        if isinstance(content, str) and content and not guarded:
                            answer_so_far += content
                            yield sse_event("delta", {"text": content})
                        elif isinstance(content, list) and not guarded:
                            content_text = "".join(
                                part.get("text", "") for part in content if isinstance(part, dict)
                            )
                            if content_text:
                                answer_so_far += content_text
                                yield sse_event("delta", {"text": content_text})
                    if event.get("event") == "on_chain_end" and event.get("name") == "generate":
                        output = event.get("data", {}).get("output", {})
                        answer = output.get("answer") if isinstance(output, dict) else None
                        if isinstance(answer, str):
                            answer_so_far = answer

                if not answer_so_far:
                    raise RuntimeError("模型没有生成可显示的回答。")
                if guarded:
                    yield sse_event("delta", {"text": answer_so_far})
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
            # 仅在本次 SSE 未正常完成或持久化错误时收尾，避免遗留 streaming 状态。
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
