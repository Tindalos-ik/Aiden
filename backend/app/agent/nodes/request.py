import json
import math
from typing import Any

from langchain_core.messages import ToolMessage

from app.agent.messages import _content_as_text, _latest_user_text
from app.agent.state import SupportState




def finish_request(state: SupportState) -> dict[str, Any]:
    """冻结当前项的事实与澄清结果，随后继续下一项。"""
    index = state.get("request_index", 0)
    requests = state.get("requests", [])
    if index >= len(requests):
        return {}
    tool_results = []
    for message in state.get("messages", [])[state.get("request_tool_start", 0):]:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(_content_as_text(message.content))
        except (TypeError, ValueError):
            payload = {"status": "error"}
        tool_results.append({
            "name": message.name,
            "payload": payload if isinstance(payload, dict) else {"status": "error"},
        })
    faq_payload = next((
        tool["payload"] for tool in reversed(tool_results) if tool["name"] == "search_faq"
    ), None)
    source_text = _latest_user_text(state.get("messages", []))
    quote = requests[index].get("original_question_quote")
    raw_question = (quote.strip() if isinstance(quote, str) and quote.strip()
                    and quote.strip() in source_text else
                    source_text if len(requests) == 1 else requests[index]["completed_question"].strip())
    retrieval = {
        "original_question": raw_question,
        "retrieval_query": requests[index]["completed_question"].strip()[:500] if faq_payload is not None else None,
        "retrieval_status": ("not_searched" if faq_payload is None else
                             "searched" if faq_payload.get("status") == "ok" else "error"),
        "retrieval_snapshot": [
            {"rank": rank, "chunk_id": str(row.get("chunkId") or ""),
             "content": str(row.get("answer") or ""),
             "source": row.get("sourcePath") or row.get("sourceUrl") or "FAQ",
             "section": str(row.get("sectionPath") or ""),
             "score": row.get("score") if isinstance(row.get("score"), (int, float))
             and not isinstance(row.get("score"), bool) and math.isfinite(row["score"]) else None}
            for rank, row in enumerate(faq_payload.get("results", []), 1)
            if isinstance(row, dict)
        ] if faq_payload is not None else [],
    }
    result = {
        "question": requests[index]["completed_question"],
        "intent": requests[index]["intent"],
        "goal": requests[index]["goal"],
        "direct_reply": state.get("direct_reply", ""),
        "tools": tool_results,
        "retrieval": retrieval,
        "citations": state.get("citations", []),
        "handoff": state.get("handoff_requested", False),
        "low_confidence": state.get("request_low_confidence"),
        "refund_phase": state.get("refund_phase", ""),
        "refund_order_no": state.get("refund_order_no", ""),
        "refund_reason": state.get("refund_reason", ""),
        "refund_policy_id": state.get("refund_policy_id", ""),
        "refund_policy_snapshot": state.get("refund_policy_snapshot", ""),
        "refund_request_type": state.get("refund_request_type", "refund"),
    }
    return {
        "request_results": [*state.get("request_results", []), result],
        "request_index": index + 1,
        "allowed_tools": [],
        "direct_reply": "",
        "citations": [],
        "request_low_confidence": None,
        "handoff_requested": False,
        "refund_authorized": False,
        "refund_phase": "",
        "refund_policy_id": "",
    }
