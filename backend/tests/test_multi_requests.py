"""定向验证多请求编排的隔离、去重和写操作边界。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from app.agent import graph as support_graph
from app.services.tools.ticket_create import create_ticket


def request(intent, goal, question, order_no=None, confidence=0.98, clarify=False,
            action_quote=None, refund_reason_quote=None, refund_consent="none"):
    return {
        "intent": intent, "goal": goal, "cofidence": confidence,
        "completed_question": question,
        "entities": {"order_no": order_no, "order_reference": "explicit" if order_no else "none"},
        "request_no": None, "ticket_no": None, "action_quote": action_quote,
        "refund_reason_quote": refund_reason_quote, "refund_consent": refund_consent,
        "application_type": "refund" if intent == "refund_return" and goal == "create_ticket" else "none",
        "needs_clarification": clarify, "clarification_question": None,
    }


class FakeModel:
    def __init__(self, requests, cite_faq=True):
        self.requests = requests
        self.cite_faq = cite_faq

    def bind(self, **_kwargs):
        return self

    async def ainvoke(self, messages):
        instruction = str(messages[0].content)
        if "请求拆分器" in instruction:
            return AIMessage(content=json.dumps({"requests": self.requests}, ensure_ascii=False))
        if "知识证据充分性检查器" in instruction:
            return AIMessage(content='{"sufficient":true,"reason":""}')
        data = json.loads(messages[-1].content)
        citations = [
            row["citation"]
            for item in data["evidence"] if item["name"] == "search_faq"
            for row in item["result"].get("results", []) if row.get("citation")
        ]
        suffix = f" {citations[0]}" if self.cite_faq and citations else ""
        return AIMessage(content=f"已回答：{data['question']}{suffix}")


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
    async def run_case(self, text, requests, payloads=None, history=None, cite_faq=True):
        FakeToolNode.calls = []
        FakeToolNode.payloads = payloads or {}
        model = FakeModel(requests, cite_faq=cite_faq)
        tools = {name: SimpleNamespace(name=name) for name in support_graph._TOOLS_BY_NAME}
        with (
            patch.object(support_graph, "ChatOpenAI", return_value=model),
            patch.object(support_graph, "ToolNode", FakeToolNode),
            patch.object(support_graph, "_TOOLS_BY_NAME", tools),
            patch.object(support_graph, "recent_messages", return_value=[
                *(history or []), {"role": "user", "content": text},
            ]),
            patch.object(support_graph, "finish_assistant_message"),
            patch.object(support_graph.settings, "langfuse_enabled", False),
        ):
            result = await support_graph.build_support_graph().ainvoke({
                "conversation_id": "c", "assistant_message_id": "m", "user_id": "u",
            })
        return result, list(FakeToolNode.calls)

    async def test_after_sale_and_policy_are_both_answered(self):
        result, calls = await self.run_case(
            "查订单 TEST-001 的退货申请进度，再告诉我退货政策",
            [request("refund_return", "after_sale_status", "查询订单 TEST-001 的退货申请进度", "TEST-001"),
             request("refund_return", "policy", "查询退货政策")],
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
            [request("logistics", "logistics", "查询 TEST-001 的物流", "TEST-001"),
             request("order", "order", "查询 TEST-001 的金额", "TEST-001"),
             request("order", "order", "查询 TEST-001 的金额", "TEST-001")],
            {"query_logistics": {"status": "ok", "found": True, "shipments": [{"status": "shipped"}]},
             "query_order": {"status": "ok", "found": True, "orders": [{"total_amount": "20.00"}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_logistics", "query_order"])
        self.assertEqual(calls[0][1], {"order_no": "TEST-001"})
        self.assertIn("【3】", result["answer"])

    async def test_partial_failure_and_write_boundary(self):
        result, calls = await self.run_case(
            "查我的工单进度，再帮我联系人工",
            [request("human", "ticket_status", "查询已有工单进度"),
             request("human", "other", "帮我联系人工", action_quote="帮我联系人工")],
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
            [request("logistics", "logistics", "查询这单物流", clarify=True),
             request("refund_return", "policy", "查询退货政策")],
            {"query_order": {"status": "ok", "orders": []},
             "search_faq": {"status": "ok", "results": []}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "search_faq"])
        self.assertIn("证据不足", result["answer"])
        _, calls = await self.run_case(
            "查退款申请进度和退货政策",
            [request("refund_return", "after_sale_status", "查询退款申请进度"),
             request("refund_return", "policy", "查询退货政策"),
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
        policy = {"status": "ok", "results": [{"chunkId": "refund-policy", "answer": "政策条件", "score": 1.0}]}
        prepared, calls = await self.run_case(
            "质量问题",
            [request("refund_return", "create_ticket", "订单 TEST-001 因质量问题退款",
                     refund_reason_quote="质量问题")],
            {"query_order": order, "search_faq": policy},
            history=[{"role": "assistant", "content": first["answer"],
                      "workflow_state": {"refund": first["refund_pending"]}}],
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "search_faq"])
        self.assertIn("退款处理待确认", prepared["answer"])
        self.assertIn("[1]", prepared["answer"])
        confirmed, calls = await self.run_case(
            "确认提交",
            [request("refund_return", "create_ticket", "确认提交订单退款申请",
                     action_quote="确认提交", refund_consent="agree")],
            {"submit_after_sale": {"status": "ok", "request_no": "AS-1"}},
            history=[{"role": "assistant", "content": prepared["answer"],
                      "workflow_state": {"refund": prepared["refund_pending"]}}],
        )
        self.assertEqual([name for name, _ in calls], ["submit_after_sale"])
        self.assertEqual(calls[0][1]["order_no"], "TEST-001")
        self.assertEqual(calls[0][1]["reason"], "质量问题")
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
            [request("refund_return", "create_ticket", "我要退款", action_quote="我要退款"),
             request("refund_return", "create_ticket", "帮我登记退款处理", action_quote="帮我登记退款处理")],
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
             "search_faq": {"status": "ok", "results": []}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "search_faq"])
        self.assertIn("证据不足", result["answer"])
        self.assertNotIn("退款处理待确认", result["answer"])

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
        requests = [request("product", "product", "猫粮有哪些口味"),
                    request("refund_return", "policy", "查询退货政策")]
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


if __name__ == "__main__":
    unittest.main()
