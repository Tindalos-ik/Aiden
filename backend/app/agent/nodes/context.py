from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.graph.state import StateNode

from app.agent.messages import _message_for_row, _valid_identifier
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.state import SupportState
from app.persistence.mysql.chat import finish_assistant_message, recent_messages




def make_load_context(evaluation: dict[str, Any] | None) -> StateNode[SupportState, None]:
    """绑定历史来源；评估模式只读取给定历史，不访问数据库。"""
    async def load_context(state: SupportState) -> dict[str, Any]:
        """加载归属当前用户的有限历史，并在模型 await 前关闭 MySQL Session。"""
        rows = ([*evaluation.get("history", []),
                 {"role": "user", "content": evaluation["query"]}]
                if evaluation is not None else recent_messages(
                    state["conversation_id"], state["user_id"], state["assistant_message_id"]
                ))
        history = [
            msg
            for row in rows
            if (msg := _message_for_row(row["role"], row["content"])) is not None
        ]
        context: dict[str, str] = {}
        if len(rows) >= 2 and rows[-1]["role"] == "user" and rows[-2]["role"] == "assistant":
            workflow = rows[-2].get("workflow_state")
            pending = workflow.get("refund") if isinstance(workflow, dict) else None
            if isinstance(pending, dict) and pending.get("stage") == "order":
                context = {"stage": "order", "request_type": pending.get("request_type", "none"),
                           "reason": pending.get("reason", "")}
            candidate = workflow.get("refund") if isinstance(workflow, dict) else None
            if isinstance(candidate, dict) and candidate.get("stage") in {"reason", "confirm"}:
                order_no = candidate.get("order_no")
                reason = candidate.get("reason", "")
                policy_id = candidate.get("policy_id", "")
                confirmed = candidate["stage"] == "confirm"
                # 确认阶段必须同时带上一轮核验过的政策 ID，否则不重建可写入的上下文。
                if (_valid_identifier(order_no)
                        and isinstance(reason, str) and len(reason) <= 160
                        and (not confirmed or (reason and _valid_identifier(policy_id)
                                              and candidate.get("policy_snapshot")))):
                    context = {"stage": candidate["stage"], "order_no": order_no,
                               "reason": reason}
                    context["request_type"] = candidate.get("request_type", "refund")
                    if confirmed:
                        context["policy_id"] = policy_id
                        context["policy_snapshot"] = candidate["policy_snapshot"]
        return {"messages": [SystemMessage(content=SYSTEM_PROMPT), *history],
                "refund_context": context}

    return load_context


def make_save_answer(evaluation: dict[str, Any] | None) -> StateNode[SupportState, None]:
    """绑定保存边界；评估模式返回答复但不写数据库或提交人工接管。"""
    def save_answer(state: SupportState) -> dict[str, Any]:
        """模型完成工具循环或澄清/兜底后，仅保存 generate 产出的最终回答。"""
        answer = state["answer"]
        if not answer:
            raise RuntimeError("模型没有生成可显示的回答。")
        if evaluation is not None:
            return {"answer": answer, "handoff_committed": False}
        handoff_committed = finish_assistant_message(
            state["assistant_message_id"],
            state["conversation_id"],
            state["user_id"],
            answer,
            "complete",
            citations=state.get("citations", []),
            retrieval_snapshots=state.get("retrieval_snapshots", []),
            low_confidence=state.get("low_confidence", []),
            handoff=state.get("handoff_required", False),
            workflow_state=({"refund": state["refund_pending"]}
                            if state.get("refund_pending") else None),
        )
        return {"answer": answer, "citations": state.get("citations", []), "handoff_committed": handoff_committed}

    return save_answer
