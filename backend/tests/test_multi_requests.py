"""定向验证多请求编排的隔离、去重和写操作边界。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from app.agent import graph as support_graph


def request(intent, goal, question, order_no=None, confidence=0.98, clarify=False,
            action_quote=None):
    return {
        "intent": intent, "goal": goal, "cofidence": confidence,
        "completed_question": question,
        "entities": {"order_no": order_no, "order_reference": "explicit" if order_no else "none"},
        "request_no": None, "ticket_no": None, "action_quote": action_quote,
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
        suffix = " [1]" if self.cite_faq and any(
            item["name"] == "search_faq" for item in data["evidence"]
        ) else ""
        return AIMessage(content=f"已回答：{data['question']}{suffix}")


class FakeToolNode:
    calls = []
    payloads = {}

    def __init__(self, tools, **_kwargs):
        self.name = tools[0].name

    async def __call__(self, state):
        call = state["messages"][-1].tool_calls[0]
        self.calls.append((self.name, call["args"]))
        payload = self.payloads.get(self.name, {"status": "ok", "found": True})
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
             "query_policy": {"status": "ok", "found": True, "policies": [{"name": "退货政策"}]}},
        )
        self.assertEqual([name for name, _ in calls], ["query_after_sale", "query_policy"])
        self.assertEqual(calls[0][1], {"order_no": "TEST-001"})
        self.assertIn("【1】", result["answer"])
        self.assertIn("【2】", result["answer"])

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

    async def test_missing_target_does_not_block_policy_or_create_ticket(self):
        result, calls = await self.run_case(
            "这单物流到哪了，再告诉我退货政策",
            [request("logistics", "logistics", "查询这单物流", clarify=True),
             request("refund_return", "policy", "查询退货政策")],
            {"query_order": {"status": "ok", "orders": []},
             "query_policy": {"status": "ok", "found": False, "policies": [], "message": "没有现行政策"}},
        )
        self.assertEqual([name for name, _ in calls], ["query_order", "query_policy"])
        self.assertIn("没有现行政策", result["answer"])
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

    async def test_low_confidence_and_duplicate_write(self):
        result, calls = await self.run_case(
            "我要退款，再帮我登记退款处理；那个也查一下",
            [request("refund_return", "create_ticket", "我要退款", action_quote="我要退款"),
             request("refund_return", "create_ticket", "登记退款处理", action_quote="登记退款处理"),
             request("other", "other", "那个也查一下", confidence=0.2)],
            {"create_ticket": {"status": "ok", "ticket_no": "T-1"}},
        )
        self.assertEqual([name for name, _ in calls], ["create_ticket"])
        self.assertIn("【3】", result["answer"])
        self.assertIn("没能确定", result["answer"])

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
            "search_faq": {"status": "ok", "results": [{
                "chunkId": "chunk-1", "answer": "鸡肉味", "score": 1.0,
            }]},
            "query_policy": {"status": "ok", "found": False, "policies": [], "message": "没有现行政策"},
        }
        result, calls = await self.run_case("猫粮有哪些口味，再告诉我退货政策", requests, payloads)
        self.assertEqual([name for name, _ in calls], ["search_faq", "query_policy"])
        self.assertIn("[1]", result["answer"])
        self.assertEqual([row["number"] for row in result["citations"]], [1])
        refused, _ = await self.run_case(
            "猫粮有哪些口味，再告诉我退货政策", requests, payloads, cite_faq=False,
        )
        self.assertIn("证据不足", refused["answer"])
        self.assertIn("没有现行政策", refused["answer"])


if __name__ == "__main__":
    unittest.main()
