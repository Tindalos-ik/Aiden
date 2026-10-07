import json
import re
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langgraph.graph.state import StateNode

from app.agent.messages import _content_as_text
from app.agent.order_selection import _parse_order_list
from app.agent.prompt import (
    SYSTEM_PROMPT,
    _FALLBACK_REPLY,
    _KNOWLEDGE_REFUSAL,
    _REFUND_CONFIRM_PROMPT,
    _REFUND_REASON_PROMPT,
)
from app.agent.state import SupportState
from app.config.settings import settings


def _edge_ordered_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """最高相关放开头，次高相关放结尾，降低长上下文中段的信息损失。"""
    if len(results) < 3:
        return results
    return [results[0], *results[2:], results[1]]


def make_generate(model: Runnable[LanguageModelInput, BaseMessage]) -> StateNode[SupportState, None]:
    """绑定流式答复模型，按单项证据生成并核验全轮引用。"""
    async def generate(state: SupportState) -> dict[str, Any]:
        """逐项生成并组合答复，单项失败不影响其他项；引用按全轮重新编号。"""
        direct_reply = state.get("direct_reply", "").strip()
        items = state.get("request_results", [])
        if direct_reply and not items:
            return {"messages": [AIMessage(content=direct_reply)], "answer": direct_reply}
        answers: list[str] = []
        all_citations: list[dict[str, object]] = []
        low_confidence: list[dict[str, object]] = []
        refund_pending: dict[str, str] = {}
        retrieval_snapshots: list[dict[str, object]] = []
        for item in items:
            snapshot = item["retrieval"]
            retrieval_snapshots.append(snapshot)
            if item.get("low_confidence"):
                low_confidence.append({**item["low_confidence"], **snapshot})
            reply = item.get("direct_reply", "")
            tools = item.get("tools", [])
            if not reply and tools:
                latest = tools[-1]
                payload = latest["payload"]
                if payload.get("status") != "ok":
                    reply = str(payload.get("message") or ("申请提交暂时失败，请稍后重试。" if latest["name"] == "submit_after_sale" else "查询暂时失败，请稍后重试。"))
                    if latest["name"] == "submit_after_sale" and payload.get("status") == "policy_changed":
                        # 仅保留订单与原因用于重新核验，旧政策 ID、快照及提交确认全部失效。
                        refund_pending = {"stage": "reason", "order_no": item["refund_order_no"],
                                          "reason": item["refund_reason"], "request_type": "refund"}
                        reply += (f"。订单号 {item['refund_order_no']}；原因 {item['refund_reason']}。"
                                  "旧提交确认已失效，请回复“重新核验”，我会重新核对本人订单和当前政策，"
                                  "核验通过后需再次明确确认；也可取消办理。")
                elif latest["name"] == "submit_after_sale":
                    prefix = "已有待处理申请" if payload.get("already_exists") else "已提交退款申请"
                    reply = f"{prefix}，申请号：{payload['request_no']}。当前待审核；审核通过不代表款项已退，后续进度可查询申请号。"
                elif latest["name"] == "search_faq" and not item.get("citations"):
                    reply = _KNOWLEDGE_REFUSAL
                elif latest["name"] == "create_ticket" and not payload.get("ticket_no"):
                    reply = "工单登记未返回工单号，请稍后核实。"
                elif latest["name"] == "create_ticket":
                    prefix = "已有待处理工单" if payload.get("already_exists") else "已登记待处理工单"
                    reply = f"{prefix}，工单号：{payload['ticket_no']}。可查询工单进度。"
                elif latest["name"] == "query_ticket" and payload.get("tickets"):
                    labels = {"open": "待接单", "in_progress": "处理中", "resolved": "已解决，待关闭", "closed": "已关闭"}
                    reply = "\n".join(
                        f"工单 {row['ticket_no']}：当前状态为{labels.get(row['status'], row['status'])}。"
                        for row in payload["tickets"] if isinstance(row, dict)
                        and isinstance(row.get("ticket_no"), str)
                        and isinstance(row.get("status"), str)
                    )
                elif payload.get("found") is False or (
                    latest["name"] == "search_faq" and not payload.get("results")
                ):
                    reply = str(payload.get("message") or "当前没有查到这项请求的结果。")
            if not reply and item.get("goal") == "smalltalk":
                # 闲聊没有工具证据；沿用近期对话语境，但只回答当前这一项。
                conversation = [
                    message for message in state.get("messages", [])
                    if isinstance(message, (HumanMessage, AIMessage))
                    and not (isinstance(message, AIMessage) and message.tool_calls)
                ]
                if conversation and isinstance(conversation[-1], HumanMessage):
                    conversation.pop()  # 当前完整用户消息由下方的单项问题替代。
                response = await model.ainvoke([
                    SystemMessage(content=(
                        SYSTEM_PROMPT + "\n当前只回答一项闲聊。自然、简短地回应问候、感谢、"
                        "自我介绍或能力范围问题；无需工具证据，不要声称查到了订单、"
                        "政策或其他业务结果，也不要编造未接入的能力。"
                    )),
                    *conversation[-6:],
                    HumanMessage(content=json.dumps({
                        "question": item["question"], "evidence": [],
                    }, ensure_ascii=False)),
                ], config=(
                    {
                        "metadata": {"cost_intent": item["intent"]},
                        "run_name": f"cost_intent_{item['intent']}",
                    }
                    if settings.langfuse_enabled else None
                ))
                reply = _content_as_text(response.content).strip()
            if not reply:
                evidence = []
                local_citations = item.get("citations", [])
                offset = max((int(row["number"]) for row in all_citations), default=0)
                for tool_result in tools:
                    payload = dict(tool_result["payload"])
                    if tool_result["name"] == "search_faq":
                        results = [dict(row) for row in payload.get("results", []) if isinstance(row, dict)]
                        for index, row in enumerate(results, 1):
                            row["citation"] = f"[{offset + index}]"
                        payload["results"] = _edge_ordered_results(results)
                    evidence.append({"name": tool_result["name"], "result": payload})
                # 只有服务端核验到覆盖本次原因的有效商家政策时才允许给出提交确认。
                refund_policy = (item.get("goal") == "create_ticket"
                                 and bool(item.get("refund_policy_id")))
                response = await model.ainvoke([
                    SystemMessage(content=SYSTEM_PROMPT + "\n只回答这一项请求。必须以给出的本轮结果为准，简短说明查到的事实；不要输出 JSON 或工具名。"
                                  # 只有检索到带编号的知识块时才要求引用编号；订单/政策工具结果没有编号，
                                  # 若继续要求编号，模型会凭空写出 [1] 这类悬空标记。
                                  + ("商品知识和政策规则的每条事实必须引用给出的编号。"
                                     if local_citations else
                                     "本轮证据没有编号，禁止输出 [1] 这类引用标记，直接陈述事实。")
                                  + ("\n这是退款申请提交前说明：指出已核对的订单事实、政策证据能确认的条件、仍缺少或无法判断的信息；提交后待员工审核，不得说退款资格、金额或时效已经确定，也不要自行要求用户确认。"
                                     "本项的订单事实和适用政策都来自本轮工具结果（query_order、query_refund_policy），本轮按设计没有检索知识库，不得以缺少知识库检索为由拒答。" if refund_policy else "")),
                    HumanMessage(content=json.dumps({"question": item["question"], "evidence": evidence}, ensure_ascii=False)),
                ], config=(
                    {
                        "metadata": {"cost_intent": item["intent"]},
                        "run_name": f"cost_intent_{item['intent']}",
                    }
                    if settings.langfuse_enabled else None
                ))
                reply = _content_as_text(response.content).strip()
                # 引用检查发生在草稿生成后；进度只带阶段，不泄露尚未通过检查的文字。
                await adispatch_custom_event(
                    "support_progress", {"stage": "validation", "message": "正在核验回答引用"}
                )
                if local_citations:
                    used_numbers = {int(value) for value in re.findall(r"\[(\d+)\]", reply)}
                    permitted = {offset + int(row["number"]) for row in local_citations}
                    if not used_numbers or not used_numbers <= permitted:
                        reply = _KNOWLEDGE_REFUSAL
                        low_confidence.append({
                            "original_question": item["question"].strip(),
                            "entrypoint": "generation_missing_citation",
                            "reason": "生成答案没有引用召回的知识块",
                            **snapshot,
                        })
                    else:
                        all_citations.extend(
                            {**row, "number": offset + int(row["number"])}
                            for row in local_citations if offset + int(row["number"]) in used_numbers
                        )
                else:
                    # 没有待引用编号时，模型写出的 [n] 没有对应来源，保存前直接去掉；
                    # 带编号的知识问答仍走上面的子集校验，不受影响。
                    reply = re.sub(r"[ \t]*(?:\[[0-9]+\]|［[0-9]+］)", "", reply)
                if refund_policy and reply and reply != _KNOWLEDGE_REFUSAL:
                    reply += "\n" + _REFUND_CONFIRM_PROMPT.format(
                        order_no=item["refund_order_no"], reason=item["refund_reason"],
                    )
                    refund_pending = {"stage": "confirm", "order_no": item["refund_order_no"],
                                      "reason": item["refund_reason"],
                                      "policy_id": item["refund_policy_id"],
                                      "policy_snapshot": item["refund_policy_snapshot"],
                                      "request_type": "refund"}
            if (item.get("goal") == "create_ticket" and item.get("refund_order_no")
                    and reply == _REFUND_REASON_PROMPT.format(order_no=item["refund_order_no"])):
                refund_pending = {"stage": "reason", "order_no": item["refund_order_no"],
                                  "reason": "", "request_type": "refund"}
            if (item.get("goal") == "create_ticket" and item.get("refund_request_type")
                    and _parse_order_list(reply) is not None):
                refund_pending = {"stage": "order", "request_type": item["refund_request_type"],
                                  "reason": item.get("refund_reason", "")}
            answers.append(reply or _FALLBACK_REPLY)
        answer = answers[0] if len(answers) == 1 else "\n".join(
            f"【{index}】 {reply}" for index, reply in enumerate(answers, 1)
        )
        output: dict[str, Any] = {"messages": [AIMessage(content=answer)], "answer": answer, "citations": all_citations,
                                  "handoff_required": any(item.get("handoff") for item in items),
                                  "refund_pending": refund_pending}
        output["low_confidence"] = low_confidence
        output["retrieval_snapshots"] = retrieval_snapshots
        return output

    return generate
