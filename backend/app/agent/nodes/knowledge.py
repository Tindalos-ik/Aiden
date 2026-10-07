import json
import math
import re
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.graph.state import StateNode

from app.agent.messages import _content_as_text, _latest_user_text
from app.agent.prompt import _KNOWLEDGE_REFUSAL
from app.agent.state import SupportState
from app.config.rag import rag_settings
from app.config.settings import settings


def _faq_tool_payload(messages: list[BaseMessage], start: int = 0) -> dict[str, Any] | None:
    """只读取本轮 search_faq 的工具结果；历史消息不会加载为 ToolMessage。"""
    for message in reversed(messages[start:]):
        if isinstance(message, ToolMessage) and message.name == "search_faq":
            try:
                payload = json.loads(_content_as_text(message.content))
            except (TypeError, ValueError):
                return {"status": "error", "results": []}
            return payload if isinstance(payload, dict) else {"status": "error", "results": []}
    return None

def _citation_rows(results: list[dict[str, Any]]) -> list[dict[str, object]]:
    """把工具结果收窄为可持久化、可在历史消息中展示的来源快照。"""
    return [
        {
            "number": index,
            "chunkId": str(item["chunkId"]),
            "sectionPath": str(item.get("sectionPath") or ""),
            "content": str(item.get("answer") or ""),
            "sourcePath": item.get("sourcePath"),
            "sourceUrl": item.get("sourceUrl"),
        }
        for index, item in enumerate(results, 1)
        if item.get("chunkId")
    ]


def make_assess_knowledge(model: Runnable[LanguageModelInput, BaseMessage], evaluation: dict[str, Any] | None) -> StateNode[SupportState, None]:
    """绑定答复模型与评估阈值，证据不足时关闭知识答复权限。"""
    async def assess_knowledge(state: SupportState) -> dict[str, Any]:
        """生成前让模型核对召回证据是否足够，失败问法进入问题池。"""
        payload = _faq_tool_payload(state.get("messages", []), state.get("request_tool_start", 0))
        if payload is None:
            return {}
        await adispatch_custom_event(
            "support_progress", {"stage": "validation", "message": "正在核验证据是否足以回答"}
        )
        original = (
            state["requests"][state["request_index"]]["completed_question"].strip()
            or _latest_user_text(state.get("messages", []))
        )

        def refuse(entrypoint: str, reason: str) -> dict[str, Any]:
            return {
                "direct_reply": _KNOWLEDGE_REFUSAL,
                "citations": [],
                "request_low_confidence": {
                    "original_question": original,
                    "entrypoint": entrypoint,
                    "reason": reason,
                },
            }

        if payload.get("status") != "ok":
            return {"direct_reply": "抱歉，知识检索暂时不可用，请稍后重试。", "citations": []}
        results = [item for item in payload.get("results", []) if isinstance(item, dict)]
        if not results:
            return refuse("retrieval_low_confidence", "知识库没有召回可用的证据")
        # 非重排检索的 score 为 None；只对可用的 rerank 分数应用该阈值。
        scores = [
            item["score"] for item in results
            if isinstance(item.get("score"), (int, float))
            and not isinstance(item["score"], bool)
            and math.isfinite(item["score"])
        ]
        highest_score = max(scores) if scores else None
        threshold = (evaluation.get("threshold", rag_settings.online_min_rerank_score)
                     if evaluation is not None else rag_settings.online_min_rerank_score)
        if highest_score is not None and highest_score < threshold:
            return refuse(
                "retrieval_low_confidence",
                f"最高重排分数 {highest_score:.3f} 低于阈值 {threshold:.2f}",
            )

        question = original
        check_messages = [
            SystemMessage(content=(
                "你是知识证据充分性检查器。只判断给定证据能否直接回答用户问题，不能使用常识或模型记忆补足。"
                "型号、时间、金额、条件不一致时必须判不能。只输出合法 JSON，"
                '包含布尔字段 "sufficient" 和简短原因字段 "reason"。'
            )),
            HumanMessage(content=json.dumps({"question": question, "evidence": results}, ensure_ascii=False)),
        ]
        try:
            cost_intent = state["requests"][state["request_index"]]["intent"]
            check = await model.ainvoke(
                check_messages,
                config=(
                    {
                        "metadata": {"cost_intent": cost_intent},
                        "run_name": f"cost_intent_{cost_intent}",
                    }
                    if settings.langfuse_enabled else None
                ),
            )
            raw = _content_as_text(check.content).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
            verdict = json.loads(raw)
            if not isinstance(verdict, dict) or verdict.get("sufficient") is not True:
                reason = str(verdict.get("reason") or "模型判断证据不足") if isinstance(verdict, dict) else "模型判断证据不足"
                return refuse("generation_insufficient_knowledge", reason)
        except (TypeError, ValueError):
            return refuse("generation_insufficient_knowledge", "证据充分性自评结果无法解析")
        return {"citations": _citation_rows(results)}

    return assess_knowledge
