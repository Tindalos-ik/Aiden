"""定向验证多请求编排的隔离、去重和写操作边界。"""

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from app.agent import graph as support_graph
from app.services.tools.ticket_create import create_ticket


def request(intent, goal, question, order_no=None, confidence=0.98, clarify=False,
            action_quote=None, refund_reason_quote=None, refund_consent="none", quote=None):
    return {
        "intent": intent, "goal": goal, "cofidence": confidence,
        "completed_question": question,
        # 拆分器必须为每项给出本轮原文片段；多诉求时服务端用它核对证据归属。
        "original_question_quote": quote,
        "entities": {"order_no": order_no, "order_reference": "explicit" if order_no else "none"},
        "request_no": None, "ticket_no": None, "action_quote": action_quote,
        "refund_reason_quote": refund_reason_quote, "refund_consent": refund_consent,
        "application_type": "refund" if intent == "refund_return" and goal == "create_ticket" else "none",
        "needs_clarification": clarify, "clarification_question": None,
    }


class FakeModel:
    def __init__(self, requests, cite_faq=True, adequacy='{"sufficient":true,"reason":""}',
                 answer_suffix=""):
        self.requests = requests
        self.cite_faq = cite_faq
        self.adequacy = adequacy
        # 仅用于定向用例：把额外文本（例如悬空的 [1] 标记）追加到生成回答末尾。
        self.answer_suffix = answer_suffix
        self.configs = []

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, messages, config=None):
        self.configs.append(config)
        instruction = str(messages[0].content)
        if "请求拆分器" in instruction:
            return AIMessage(content=json.dumps({"requests": self.requests}, ensure_ascii=False))
        if "知识证据充分性检查器" in instruction:
            return AIMessage(content=self.adequacy)
        data = json.loads(messages[-1].content)
        citations = [
            row["citation"]
            for item in data["evidence"] if item["name"] == "search_faq"
            for row in item["result"].get("results", []) if row.get("citation")
        ]
        should_cite = self.cite_faq(data["question"]) if callable(self.cite_faq) else self.cite_faq
        suffix = f" {citations[0]}" if should_cite and citations else ""
        return AIMessage(content=f"已回答：{data['question']}{suffix}{self.answer_suffix}")


class FakeToolNode:
    calls = []
    payloads = {}

    def __init__(self, tools, **_kwargs):
        self.name = tools[0].name

    async def __call__(self, state):
        call = state["messages"][-1].tool_calls[0]
        self.calls.append((self.name, call["args"]))
        payload = self.payloads.get(
            (self.name, call["args"].get("question")),
            self.payloads.get(self.name, {"status": "ok", "found": True}),
        )
        return {"messages": [ToolMessage(
            content=json.dumps(payload, ensure_ascii=False),
            name=self.name, tool_call_id=call["id"],
        )]}


class MultiRequestTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, text, requests, payloads=None, history=None, cite_faq=True,
                       langfuse=False, adequacy='{"sufficient":true,"reason":""}',
                       answer_suffix=""):
        FakeToolNode.calls = []
        FakeToolNode.payloads = payloads or {}
        model = FakeModel(requests, cite_faq=cite_faq, adequacy=adequacy,
                          answer_suffix=answer_suffix)
        self.last_model = model
        self.last_trace_client = Mock()
        tools = {name: SimpleNamespace(name=name) for name in support_graph._TOOLS_BY_NAME}
        with (
            patch.object(support_graph, "ChatOpenAI", return_value=model),
            patch.object(support_graph, "ToolNode", FakeToolNode),
            patch.object(support_graph, "_TOOLS_BY_NAME", tools),
            patch.object(support_graph, "recent_messages", return_value=[
                *(history or []), {"role": "user", "content": text},
            ]),
            patch.object(support_graph, "finish_assistant_message") as finish,
            patch.object(support_graph.settings, "langfuse_enabled", langfuse),
            patch("langfuse.langchain.CallbackHandler", return_value=BaseCallbackHandler())
            if langfuse else nullcontext(),
            patch("langfuse.get_client", return_value=self.last_trace_client)
            if langfuse else nullcontext(),
        ):
            self.last_finish = finish
            result = await support_graph.build_support_graph().ainvoke({
                "conversation_id": "c", "assistant_message_id": "m", "user_id": "u",
            })
        return result, list(FakeToolNode.calls)

    async def test_after_sale_and_policy_are_both_answered(self):
        result, calls = await self.run_case(
            "查订单 TEST-001 的退货申请进度，再告诉我退货政策",
            [request("refund_return", "after_sale_status", "查询订单 TEST-001 的退货申请进度", "TEST-001",
                     quote="查订单 TEST-001 的退货申请进度"),
             request("refund_return", "policy", "查询退货政策", quote="再告诉我退货政策")],
            {"query_after_sale": {"status": "ok", "found": True, "requests": [{"status": "pending"}]},
             "search_faq": {"status": "ok", "results": [{
                 "chunkId": "return-policy", "answer": "退货政策条款", "score": 1.0,
             }]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_after_sale", "search_faq"])
        self.assertEqual(calls[0][1], {"order_no": "TEST-001"})
        self.assertEqual(calls[1][1], {"question": "查询退货政策"})
        self.assertIn("【1】", result["answer"])
        self.assertIn("【2】", result["answer"])
        self.assertEqual([row["chunkId"] for row in result["citations"]], ["return-policy"])

    async def test_logistics_order_and_duplicate_query(self):
        result, calls = await self.run_case(
            "查订单 TEST-001 的物流和订单金额，再查一次订单金额",
            [request("logistics", "logistics", "查询 TEST-001 的物流", "TEST-001",
                     quote="查订单 TEST-001 的物流和订单金额"),
             request("order", "order", "查询 TEST-001 的金额", "TEST-001", quote="订单金额"),
             request("order", "order", "查询 TEST-001 的金额", "TEST-001", quote="再查一次订单金额")],
            {"query_logistics": {"status": "ok", "found": True, "shipments": [{"status": "shipped"}]},
             "query_order": {"status": "ok", "found": True, "orders": [{"total_amount": "20.00"}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_logistics", "query_order"])
        self.assertEqual(calls[0][1], {"order_no": "TEST-001"})
        self.assertIn("【3】", result["answer"])

    async def test_partial_failure_and_write_boundary(self):
        result, calls = await self.run_case(
            "查我的工单进度，再帮我联系人工",
            [request("human", "ticket_status", "查询已有工单进度", quote="查我的工单进度"),
             request("human", "other", "帮我联系人工", action_quote="帮我联系人工",
                     quote="再帮我联系人工")],
            {"query_ticket": {"status": "error"}},
        )
        self.assertEqual([name for name, _ in calls], ["query_ticket"])
        self.assertIn("查询暂时失败", result["answer"])
        self.assertIn("人工客服队列", result["answer"])
        self.assertTrue(result["handoff_required"])

    async def test_ticket_progress_always_names_the_status(self):
        result, calls = await self.run_case(
            "我的工单进度怎么样了",
            [request("complaint", "ticket_status", "查询已有工单进度")],
            {"query_ticket": {"status": "ok", "found": True, "tickets": [
                {"ticket_no": "TK-001", "status": "in_progress"},
                {"ticket_no": "TK-002", "status": "open"},
            ]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_ticket"])
        self.assertIn("TK-001：当前状态为处理中", result["answer"])
        self.assertIn("TK-002：当前状态为待接单", result["answer"])

    async def test_missing_target_does_not_block_policy_or_create_ticket(self):
        result, calls = await self.run_case(
            "这单物流到哪了，再告诉我退货政策",
            [request("logistics", "logistics", "查询这单物流", clarify=True,
                     quote="这单物流到哪了"),
             request("refund_return", "policy", "查询退货政策", quote="再告诉我退货政策")],
            {"query_order": {"status": "ok", "orders": []},
             "search_faq": {"status": "ok", "results": []}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "search_faq"])
        self.assertIn("证据不足", result["answer"])
        _, calls = await self.run_case(
            "查退款申请进度和退货政策",
            [request("refund_return", "after_sale_status", "查询退款申请进度",
                     quote="查退款申请进度"),
             request("refund_return", "policy", "查询退货政策", quote="退货政策"),
             # 第三项在本轮原话里没有对应片段：用户只问了政策和进度，并未要求办理退货，
             # 因此保留无引用，交给多诉求原文守卫判定，不为了跑通而编造原话。
             request("refund_return", "create_ticket", "办理退货")],
        )
        self.assertNotIn("create_ticket", [name for name, _ in calls])
        _, calls = await self.run_case(
            "查投诉配送服务的工单进度",
            [request("complaint", "create_ticket", "投诉配送服务")],
        )
        self.assertNotIn("create_ticket", [name for name, _ in calls])
        _, calls = await self.run_case(
            "我之前要求投诉配送服务，现在查工单进度",
            [request("complaint", "create_ticket", "投诉配送服务")],
        )
        self.assertNotIn("create_ticket", [name for name, _ in calls])

    async def test_refund_request_without_order_lists_options_without_writing(self):
        result, calls = await self.run_case(
            "我要退款",
            [request("refund_return", "create_ticket", "办理退款", action_quote="我要退款")],
            {"query_order": {"status": "ok", "orders": [{"order_no": "TEST-001", "items": [{"product_name": "商品"}]}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order"])
        self.assertIn("TEST-001", result["answer"])

    async def test_return_and_exchange_route_to_form_without_refund_write(self):
        for kind in ("return", "exchange"):
            with self.subTest(kind=kind):
                item = request("refund_return", "create_ticket", "办理售后申请", action_quote="办理售后申请")
                item["application_type"] = kind
                result, calls = await self.run_case("办理售后申请", [item])
                self.assertEqual(calls, [])
                self.assertIn("售后与工单", result["answer"])

    async def test_refund_policy_and_existing_application_keep_read_permissions(self):
        policy, calls = await self.run_case(
            "查询退款政策",
            [request("refund_return", "policy", "查询退款政策")],
            {"search_faq": {"status": "ok", "results": [{"chunkId": "policy", "answer": "退款规则", "score": 1.0}]}},
        )
        self.assertEqual([name for name, _ in calls], ["search_faq"])
        self.assertIn("[1]", policy["answer"])
        status, calls = await self.run_case(
            "退款申请到哪了",
            [request("refund_return", "after_sale_status", "查询已有退款申请进度")],
            {"query_after_sale": {"status": "ok", "found": True, "requests": [{"status": "pending"}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_after_sale"])
        self.assertIn("已回答", status["answer"])

    async def test_refund_order_selection_is_rechecked_for_owner(self):
        previous = support_graph._order_list_reply(
            [{"order_no": "TEST-001", "items": []}], refund=True,
        )
        selected = request("refund_return", "create_ticket", "办理第一笔订单退款", "TEST-001")
        selected["entities"].update({"order_reference": "listed_selection",
                                     "reference_quote": "第一个", "list_index": 1})
        result, calls = await self.run_case(
            "第一个",
            [selected],
            {"query_order": {"status": "ok", "orders": [{"order_no": "TEST-001", "items": [{"product_name": "商品"}]}]}},
            history=[{"role": "assistant", "content": previous}],
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_order"])
        self.assertEqual(calls[-1][1], {"order_no": "TEST-001"})
        self.assertIn("请说明这笔订单的退款原因", result["answer"])

    async def test_latest_refund_order_is_resolved_from_owned_list(self):
        selected = request("refund_return", "create_ticket", "办理最近一笔订单退款")
        selected["entities"].update({"order_reference": "latest", "reference_quote": "最近一笔"})
        result, calls = await self.run_case(
            "最近一笔订单我要退款",
            [selected],
            {"query_order": {"status": "ok", "orders": [{"order_no": "TEST-001", "items": [{"product_name": "商品"}]}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_order"])
        self.assertEqual(calls[-1][1], {"order_no": "TEST-001"})
        self.assertIn("退款原因", result["answer"])

    async def test_refund_reason_policy_and_confirmed_application(self):
        order = {"status": "ok", "orders": [{"order_no": "TEST-001", "status": "delivered", "items": [{"product_name": "商品"}]}]}
        first, calls = await self.run_case(
            "帮我登记订单 TEST-001 的退款处理",
            [request("refund_return", "create_ticket", "办理 TEST-001 退款", "TEST-001",
                     action_quote="帮我登记订单 TEST-001 的退款处理")],
            {"query_order": order},
        )
        self.assertEqual([name for name, _ in calls], ["query_order"])
        self.assertIn("请说明这笔订单的退款原因", first["answer"])
        policy = {"status": "ok", "policies": [{
            "id": "66666666-6666-4666-8666-000000000001", "name": "退款政策", "version": "1.0",
            "content": "商品存在质量问题的，可在签收后 7 天内申请退款。",
            "snapshot": "a" * 64,
        }]}
        prepared, calls = await self.run_case(
            "质量问题",
            [request("refund_return", "create_ticket", "订单 TEST-001 因质量问题退款",
                     refund_reason_quote="质量问题")],
            {"query_order": order, "query_refund_policy": policy},
            history=[{"role": "assistant", "content": first["answer"],
                      "workflow_state": {"refund": first["refund_pending"]}}],
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_refund_policy"])
        self.assertEqual(calls[-1][1], {"order_no": "TEST-001"})
        self.assertIn("退款处理待确认", prepared["answer"])
        self.assertEqual(prepared["citations"], [])
        self.assertEqual(prepared["refund_pending"]["policy_id"],
                         "66666666-6666-4666-8666-000000000001")
        confirmed, calls = await self.run_case(
            "确认提交",
            [request("refund_return", "create_ticket", "确认提交订单退款申请",
                     action_quote="确认提交", refund_consent="agree")],
            {"submit_after_sale": {"status": "ok", "request_no": "AS-1"}},
            history=[{"role": "assistant", "content": prepared["answer"],
                      "workflow_state": {"refund": prepared["refund_pending"]}}],
        )
        self.assertEqual([name for name, _ in calls], ["submit_after_sale"])
        self.assertIn("已提交退款申请", confirmed["answer"])
        self.assertIn("审核通过不代表款项已退", confirmed["answer"])
        declined, calls = await self.run_case(
            "取消",
            [request("refund_return", "create_ticket", "取消登记退款工单",
                     action_quote="取消", refund_consent="decline")],
            history=[{"role": "assistant", "content": prepared["answer"],
                      "workflow_state": {"refund": prepared["refund_pending"]}}],
        )
        self.assertEqual(calls, [])
        self.assertIn("不会提交", declined["answer"])
        repeated, calls = await self.run_case(
            "确认提交",
            [request("refund_return", "create_ticket", "确认提交订单退款申请",
                     action_quote="确认提交", refund_consent="agree")],
            {"submit_after_sale": {"status": "ok", "request_no": "AS-1", "already_exists": True}},
            history=[{"role": "assistant", "content": prepared["answer"],
                      "workflow_state": {"refund": prepared["refund_pending"]}}],
        )
        self.assertEqual([name for name, _ in calls], ["submit_after_sale"])
        self.assertIn("已有待处理申请", repeated["answer"])

    async def test_refund_policy_reply_cannot_keep_dangling_citation_marker(self):
        """商家政策路径没有编号证据；模型即使写出 [1]，保存的回答里也不能留下悬空引用。"""
        result, calls = await self.run_case(
            "订单 TEST-001 因质量问题我要退款",
            [request("refund_return", "create_ticket", "办理 TEST-001 退款", "TEST-001",
                     action_quote="我要退款", refund_reason_quote="质量问题")],
            {"query_order": {"status": "ok", "orders": [
                {"order_no": "TEST-001", "status": "delivered",
                 "items": [{"product_name": "商品", "quantity": 1}]}]},
             "query_refund_policy": {"status": "ok", "policies": [{
                 "id": "66666666-6666-4666-8666-000000000003", "name": "退款政策", "version": "1.0",
                 "snapshot": "a" * 64,
                 "content": "商品存在质量问题的，可在签收后 7 天内申请退款。"}]}},
            answer_suffix=" 见政策条款 [1]",
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_refund_policy"])
        self.assertIn("退款处理待确认", result["answer"])
        self.assertNotIn("[1]", result["answer"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["refund_pending"]["policy_id"],
                         "66666666-6666-4666-8666-000000000003")
        self.assertEqual(self.last_finish.call_args.args[3], result["answer"])

    async def test_refund_consent_without_prepared_context_cannot_write(self):
        result, calls = await self.run_case(
            "确认登记",
            [request("refund_return", "create_ticket", "确认登记退款处理",
                     action_quote="确认登记", refund_consent="agree")],
        )
        self.assertEqual(calls, [])
        self.assertIn("请先确定订单", result["answer"])
        _, calls = await self.run_case(
            "确认登记",
            [request("refund_return", "create_ticket", "确认登记退款处理",
                     action_quote="确认登记", refund_consent="agree")],
            history=[{"role": "assistant", "content": support_graph._REFUND_CONFIRM_PROMPT.format(
                order_no="TEST-001", reason="质量问题") }],
        )
        self.assertEqual(calls, [])

    async def test_repeated_refund_request_still_cannot_write(self):
        _, calls = await self.run_case(
            "我要退款，再帮我登记退款处理",
            [request("refund_return", "create_ticket", "我要退款", action_quote="我要退款",
                     quote="我要退款"),
             request("refund_return", "create_ticket", "帮我登记退款处理",
                     action_quote="帮我登记退款处理", quote="帮我登记退款处理")],
            {"query_order": {"status": "ok", "orders": []}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order"])

    def test_refund_ticket_tool_is_disabled(self):
        with patch("app.services.tools.ticket_create.create_user_ticket") as writer:
            payload = json.loads(create_ticket.func(
                issue_type="refund_return", description="订单 TEST-001 的退款处理请求",
                user_id="u", conversation_id="c", assistant_message_id="m",
            ))
        self.assertEqual(payload["status"], "invalid")
        writer.assert_not_called()

    async def test_refund_policy_missing_cannot_offer_confirmation(self):
        result, calls = await self.run_case(
            "订单 TEST-001 因质量问题我要退款",
            [request("refund_return", "create_ticket", "办理 TEST-001 退款", "TEST-001",
                     action_quote="我要退款", refund_reason_quote="质量问题")],
            {"query_order": {"status": "ok", "orders": [{"order_no": "TEST-001", "items": [{"product_name": "商品"}]}]},
             "query_refund_policy": {"status": "ok", "policies": []}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_refund_policy"])
        self.assertIn("TEST-001", result["answer"])
        self.assertIn("质量问题", result["answer"])
        self.assertIn("售后与工单", result["answer"])
        self.assertNotIn("退款处理待确认", result["answer"])
        self.assertFalse(result["refund_pending"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(
            [row["entrypoint"] for row in result["low_confidence"]],
            ["refund_policy_unavailable"],
        )

    async def test_selected_owned_order_with_irrelevant_faq_needs_manual_guidance(self):
        previous = support_graph._order_list_reply([
            {"order_no": "TEST-001", "items": [{"product_name": "第一件", "quantity": 1}]},
            {"order_no": "TEST-002", "items": [{"product_name": "第二件", "quantity": 1}]},
        ], refund=True)
        selected = request("refund_return", "create_ticket", "第二个订单因货不对板申请退款", "TEST-002",
                           action_quote="我要退款第二个订单", refund_reason_quote="货不对板")
        selected["entities"].update({"order_reference": "listed_selection",
                                     "reference_quote": "第二个订单", "list_index": 2})
        result, calls = await self.run_case(
            "我要退款第二个订单，原因：货不对板", [selected],
            {"query_order": {"status": "ok", "orders": [
                {"order_no": "TEST-001", "items": [{"product_name": "第一件", "quantity": 1}]},
                {"order_no": "TEST-002", "status": "paid", "items": [{"product_name": "第二件", "quantity": 1}]},
            ]}, "query_refund_policy": {"status": "ok", "policies": [{
                "id": "policy-generic", "name": "退款政策", "version": "1.0",
                "content": "签收后 7 天内可申请退货，是否符合条件需审核订单与商品状态。",
            }]}, "search_faq": {"status": "ok", "results": [
                {"chunkId": "generic-return", "answer": "七天无理由退货条件", "score": 0.95,
                 "category": "退货政策", "sourcePath": "退货政策.md"},
            ]}},
            history=[{"role": "assistant", "content": previous}],
        )
        # 通用政策条款和 FAQ 都不能成为提交依据：只查本人订单和本人可用的有效商家政策。
        self.assertEqual(
            [name for name, _ in calls],
            ["query_order", "query_order", "query_refund_policy"],
        )
        self.assertEqual(calls[1][1], {"order_no": "TEST-002"})
        self.assertEqual(calls[2][1], {"order_no": "TEST-002"})
        self.assertIn("TEST-002", result["answer"])
        self.assertIn("货不对板", result["answer"])
        self.assertIn("售后与工单", result["answer"])
        self.assertNotIn("退款处理待确认", result["answer"])
        self.assertFalse(result["refund_pending"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(
            [row["entrypoint"] for row in result["low_confidence"]],
            ["refund_policy_unavailable"],
        )

    async def test_invented_order_number_is_not_queried(self):
        _, calls = await self.run_case(
            "查我的订单金额",
            [request("order", "order", "查询 TEST-999 的金额", "TEST-999")],
            {"query_order": {"status": "ok", "orders": []}},
        )
        self.assertEqual(calls, [("query_order", {})])

    async def test_request_limit_closes_tools(self):
        result, calls = await self.run_case(
            "查询五项服务",
            [request("order", "order", f"查询第{i}项") for i in range(5)],
        )
        self.assertEqual(calls, [])
        self.assertIn("最多四项", result["answer"])

    async def test_low_confidence_smalltalk_stays_tool_free(self):
        result, calls = await self.run_case(
            "你好", [request("smalltalk", "smalltalk", "你好", confidence=0.2)],
        )
        self.assertEqual(calls, [])
        self.assertIn("已回答", result["answer"])

    def test_order_list_remains_selectable_inside_multi_reply(self):
        answer = (
            "【1】 已回答政策。\n"
            f"【2】 {support_graph._ORDER_LIST_HEADER}\n"
            "1. TEST-001 — 猫粮 ×1\n"
            "【3】 已回答其他问题。"
        )
        self.assertEqual(support_graph._parse_order_list(answer), ["TEST-001"])

    async def test_selection_from_previous_multi_reply_is_rechecked(self):
        previous = f"【1】 已回答政策。\n【2】 {support_graph._ORDER_LIST_HEADER}\n1. TEST-001 — 猫粮 ×1"
        item = request("logistics", "logistics", "查询列表第一个订单的物流", "TEST-001")
        item["entities"]["order_reference"] = "listed_selection"
        item["entities"]["reference_quote"] = "第一个"
        item["entities"]["list_index"] = 1
        _, calls = await self.run_case(
            "第一个的物流", [item],
            {"query_order": {"status": "ok", "orders": [{"order_no": "TEST-001"}]},
             "query_logistics": {"status": "ok", "found": True, "shipments": []}},
            history=[{"role": "assistant", "content": previous}],
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_logistics"])
        self.assertEqual(calls[-1][1], {"order_no": "TEST-001"})

    async def test_product_citation_is_checked_per_item(self):
        requests = [request("product", "product", "猫粮有哪些口味", quote="猫粮有哪些口味"),
                    request("refund_return", "policy", "查询退货政策", quote="再告诉我退货政策")]
        payloads = {
            ("search_faq", "猫粮有哪些口味"): {"status": "ok", "results": [{
                "chunkId": "chunk-1", "answer": "鸡肉味", "score": 1.0,
            }]},
            ("search_faq", "查询退货政策"): {"status": "ok", "results": [{
                "chunkId": "policy-1", "answer": "七天内可申请退货", "score": 1.0,
            }]},
        }
        result, calls = await self.run_case("猫粮有哪些口味，再告诉我退货政策", requests, payloads)
        self.assertEqual([name for name, _ in calls], ["search_faq", "search_faq"])
        self.assertIn("[1]", result["answer"])
        self.assertIn("[2]", result["answer"])
        self.assertEqual([row["number"] for row in result["citations"]], [1, 2])
        refused, _ = await self.run_case(
            "猫粮有哪些口味，再告诉我退货政策", requests, payloads, cite_faq=False,
        )
        self.assertIn("证据不足", refused["answer"])
        self.assertEqual(refused["citations"], [])

    async def test_retrieval_empty_and_low_score_record_each_original_question(self):
        questions = ["猫粮的冷藏条件", "退货是否需要包装"]
        payloads = {
            ("search_faq", questions[0]): {"status": "ok", "results": []},
            ("search_faq", questions[1]): {"status": "ok", "results": [{
                "chunkId": "weak", "answer": "不相关证据", "score": -999.0,
            }]},
        }
        result, calls = await self.run_case(
            "请问猫粮的冷藏条件，还有退货是否需要包装",
            [request("product", "product", questions[0], quote=questions[0]),
             request("refund_return", "policy", questions[1], quote=questions[1])],
            payloads,
        )
        self.assertEqual([name for name, _ in calls], ["search_faq", "search_faq"])
        self.assertEqual(result["answer"].count(support_graph._KNOWLEDGE_REFUSAL), 2)
        self.assertEqual(result["citations"], [])
        records = self.last_finish.call_args.kwargs["low_confidence"]
        self.assertEqual(
            [(row["original_question"], row["entrypoint"]) for row in records],
            [(question, "retrieval_low_confidence") for question in questions],
        )
        self.assertIn("没有召回", records[0]["reason"])
        self.assertIn("低于阈值", records[1]["reason"])

    async def test_retrieval_failure_does_not_misclassify_it_as_missing_knowledge(self):
        result, _ = await self.run_case(
            "查猫粮保质期",
            [request("product", "product", "猫粮保质期")],
            {"search_faq": {"status": "error", "message": "检索服务暂不可用"}},
        )
        self.assertIn("知识检索暂时不可用", result["answer"])
        self.assertEqual(self.last_finish.call_args.kwargs["low_confidence"], [])

    async def test_insufficient_and_unparseable_self_review_refuse_before_generation(self):
        payloads = {"search_faq": {"status": "ok", "results": [{
            "chunkId": "policy", "answer": "需要审核", "score": 1.0,
        }]}}
        for verdict, reason in [
            ('{"sufficient":false,"reason":"证据与问题不符"}', "证据与问题不符"),
            ("invalid JSON", "自评结果无法解析"),
        ]:
            with self.subTest(verdict=verdict):
                result, _ = await self.run_case(
                    "查退货政策", [request("refund_return", "policy", "查退货政策")],
                    payloads, adequacy=verdict,
                )
                self.assertEqual(result["answer"], support_graph._KNOWLEDGE_REFUSAL)
                self.assertEqual(result["citations"], [])
                records = self.last_finish.call_args.kwargs["low_confidence"]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["entrypoint"], "generation_insufficient_knowledge")
                self.assertIn(reason, records[0]["reason"])

    async def test_missing_citation_records_each_failed_item(self):
        questions = ["猫粮口味", "退货时间"]
        payloads = {
            ("search_faq", questions[0]): {"status": "ok", "results": [{
                "chunkId": "flavors", "answer": "鸡肉味", "score": 1.0,
            }]},
            ("search_faq", questions[1]): {"status": "ok", "results": [{
                "chunkId": "return", "answer": "七天", "score": 1.0,
            }]},
        }
        result, _ = await self.run_case(
            "猫粮口味和退货时间",
            [request("product", "product", questions[0], quote=questions[0]),
             request("refund_return", "policy", questions[1], quote=questions[1])],
            payloads, cite_faq=False,
        )
        self.assertEqual(result["answer"].count(support_graph._KNOWLEDGE_REFUSAL), 2)
        self.assertEqual(result["citations"], [])
        self.assertEqual(
            [(row["original_question"], row["entrypoint"]) for row in
             self.last_finish.call_args.kwargs["low_confidence"]],
            [(question, "generation_missing_citation") for question in questions],
        )

    async def test_missing_citation_in_second_item_keeps_first_answer_and_source(self):
        first, second = "猫粮口味", "退货时间"
        result, _ = await self.run_case(
            "猫粮口味和退货时间",
            [request("product", "product", first, quote=first),
             request("refund_return", "policy", second, quote=second)],
            {
                ("search_faq", first): {"status": "ok", "results": [{
                    "chunkId": "flavors", "answer": "鸡肉味", "score": 1.0,
                }]},
                ("search_faq", second): {"status": "ok", "results": [{
                    "chunkId": "return", "answer": "七天", "score": 1.0,
                }]},
            },
            cite_faq=lambda question: question == first,
        )
        self.assertIn("【1】 已回答：猫粮口味 [1]", result["answer"])
        self.assertIn(f"【2】 {support_graph._KNOWLEDGE_REFUSAL}", result["answer"])
        self.assertEqual([row["chunkId"] for row in result["citations"]], ["flavors"])
        self.assertEqual(
            [(row["original_question"], row["entrypoint"]) for row in
             self.last_finish.call_args.kwargs["low_confidence"]],
            [(second, "generation_missing_citation")],
        )

    async def test_langfuse_metadata_is_ordered_minimal_and_cost_is_attributed_per_item(self):
        requests = [
            request("smalltalk", "smalltalk", "你好", quote="你好"),
            request("product", "product", "查询猫粮口味 TEST-999", "TEST-999",
                    quote="再查询猫粮口味"),
        ]
        result, _ = await self.run_case(
            "你好，再查询猫粮口味，订单号 TEST-999",
            requests,
            {"search_faq": {"status": "ok", "results": [{
                "chunkId": "flavors", "answer": "鸡肉味", "score": 1.0,
            }]}},
            langfuse=True,
        )
        self.last_trace_client.update_current_trace.assert_called_once()
        metadata = self.last_trace_client.update_current_trace.call_args.kwargs["metadata"]
        self.assertEqual(metadata, {"recognized_intents": [
            {"intent": "smalltalk", "goal": "smalltalk"},
            {"intent": "product", "goal": "product"},
        ]})
        self.assertNotIn("TEST-999", json.dumps(metadata))
        self.assertEqual(
            [item["intent"] for item in result["request_results"]],
            ["smalltalk", "product"],
        )
        call_configs = [config for config in self.last_model.configs if config]
        self.assertEqual(
            sum(config["metadata"] == {"cost_bucket": "intent_classification"}
                for config in call_configs),
            1,
        )
        self.assertEqual(
            [config["metadata"]["cost_intent"] for config in call_configs
             if "cost_intent" in config["metadata"]],
            ["product", "smalltalk", "product"],
        )
        self.assertEqual(
            [config["run_name"] for config in call_configs],
            [
                "cost_bucket_intent_classification",
                "cost_intent_product",
                "cost_intent_smalltalk",
                "cost_intent_product",
            ],
        )

    async def test_contextual_classifier_retry_uses_shared_cost_bucket(self):
        result, _ = await self.run_case(
            "这个呢",
            [request("other", "other", "需要澄清")],
            history=[
                {"role": "assistant", "content": "请说明您指什么？"},
                {"role": "user", "content": "刚才那个"},
            ],
            langfuse=True,
        )
        call_configs = [config for config in self.last_model.configs if config]
        self.assertEqual(
            call_configs,
            [
                {
                    "metadata": {"cost_bucket": "intent_classification"},
                    "run_name": "cost_bucket_intent_classification",
                },
                {
                    "metadata": {"cost_bucket": "intent_classification"},
                    "run_name": "cost_bucket_intent_classification",
                },
            ],
        )
        self.assertEqual(result["request_results"][0]["intent"], "other")


if __name__ == "__main__":
    unittest.main()
