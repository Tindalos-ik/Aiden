"""评估边界回归：运行真实图节点，模型/工具仅用隔离fixture。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import graph, models
from app.agent.nodes import context
from evals import run_customer_rag as runner
from test_multi_requests import FakeModel, FakeToolNode, request


class OnlineEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, query, requests, threshold=0.35, history=None, payloads=None):
        FakeToolNode.calls = []
        FakeToolNode.payloads = payloads or {"search_faq": {"status": "ok", "results": [
            {"chunkId": "fixture", "answer": "型号:MH-LP50；不含 App 联网功能；不支持满溢推送提醒", "score": 0.5,
             "sourcePath": "product-specs.md", "sectionPath": "智能猫砂盆 Lite(型号 MH-LP50)"}]}}
        with (patch.object(models, "ChatOpenAI", return_value=FakeModel(requests)),
              patch.object(graph, "ToolNode", FakeToolNode),
              patch.object(graph.settings, "langfuse_enabled", False),
              patch.object(context, "recent_messages", side_effect=AssertionError("禁止数据库历史读取")),
              patch.object(context, "finish_assistant_message", side_effect=AssertionError("禁止保存"))):
            return await runner._online_case({"query": query, "workflow": {"history": history or []}},
                                             threshold, False, None, None)

    async def test_real_assess_threshold_changes_answer(self):
        query = "MH-LP50猫砂盆支持App满溢提醒吗？"
        requests = [request("product", "product", query)]
        answer, _, tokens, actual = await self.invoke(query, requests, threshold=0.2)
        self.assertIn("[1]", answer)
        self.assertIn("assess_knowledge", actual["node_trace"])
        self.assertIn("generate", actual["node_trace"])
        refused, _, _, _ = await self.invoke(query, requests, threshold=0.8)
        self.assertIn("证据不足", refused)
        self.assertEqual(tokens, {"input": None, "output": None, "total": None})

    async def test_handoff_is_decision_not_committed(self):
        _, _, _, actual = await self.invoke("帮我转人工", [request("human", "other", "帮我转人工", action_quote="帮我转人工")])
        self.assertTrue(actual["handoff"])
        self.assertFalse(actual["handoff_committed"])

    async def test_consent_cannot_write_and_invalid_history_cannot_authorize(self):
        history = [{"role": "assistant", "content": "退款待确认", "workflow_state": {"refund": {
            "stage": "confirm", "order_no": "EVAL-001", "reason": "破损", "policy_id": "eval-policy",
            "policy_snapshot": "fixture", "request_type": "refund"}}}]
        req = [request("refund_return", "create_ticket", "确认提交退款", action_quote="确认提交退款", refund_consent="agree")]
        _, _, _, actual = await self.invoke("确认提交退款", req, history=history)
        self.assertEqual(actual["blocked_tools"], ["submit_after_sale"])
        self.assertEqual(actual["actual_tools"], [])
        history[0]["workflow_state"]["refund"].pop("policy_snapshot")
        answer, _, _, actual = await self.invoke("确认提交退款", req, history=history)
        self.assertEqual(actual["blocked_tools"], [])
        self.assertIn("先确定订单", answer)

    async def test_write_opt_in_requires_environment_marker_and_existing_context(self):
        with patch.dict("os.environ", {"EVAL_ISOLATED_ENVIRONMENT": "other"}):
            with self.assertRaisesRegex(ValueError, "EVAL_ISOLATED_ENVIRONMENT"):
                await runner._online_case({"query": "确认提交"}, 0.35, True, "eval_owner", "fixture")
        with patch.dict("os.environ", {"EVAL_ISOLATED_ENVIRONMENT": "fixture"}):
            with self.assertRaisesRegex(ValueError, "既有隔离会话"):
                await runner._online_case({"query": "确认提交"}, 0.35, True, "eval_owner", "fixture")



class ReportBoundaryTests(unittest.TestCase):
    def test_conflict_with_gold_is_unanswerable_and_unknown_tokens_stay_null(self):
        rows = [{"expected_refusal": True, "ground_truth": [{"source_path": "a"}],
                 "refused": 0, "unsafe_answer": 1, "tokens": {"input": None, "output": None, "total": None}}]
        summary = runner._summary(rows)
        self.assertEqual(summary["answerable"], 0)
        self.assertEqual(summary["unsafe_answer"], 1)
        self.assertIsNone(summary["tokens"]["total"])
        self.assertIsNone(summary["judge_tokens"]["total"])

    def test_manual_review_lands_without_overwriting_judge(self):
        report = {"cases": [{"strategy": "online", "id": "x", "faithfulness": 0.2},
                            {"strategy": "online", "id": "y", "manual_review": {"status": "pending"}}]}
        runner.apply_manual_reviews(report, [{"strategy": "online", "id": "x", "status": "disagreed",
                                            "reviewer": "reviewer", "notes": "证据支持此答案"},
                                           {"strategy": "online", "id": "y", "status": "pending"}])
        self.assertEqual(report["cases"][0]["manual_review"]["status"], "disagreed")
        self.assertEqual(report["cases"][0]["faithfulness"], 0.2)
        self.assertEqual(report["cases"][1]["manual_review"]["status"], "pending")
        with self.assertRaises(ValueError):
            runner.apply_manual_reviews(report, [{"strategy": "online", "id": "x", "status": "unknown"}])

    def test_shared_judge_requires_explicit_override(self):
        with patch.dict("os.environ", {"EVAL_JUDGE_API_KEY": "fixture"}), patch.object(runner, "ChatOpenAI"):
            with self.assertRaises(ValueError):
                runner._judge(runner.settings.openai_model, runner.settings.openai_base_url)
            self.assertFalse(runner._judge(runner.settings.openai_model, runner.settings.openai_base_url, allow_shared=True)[2])

    def test_semantic_cluster_cannot_leak_into_holdout(self):
        case = {"id": "v", "query": "q", "type": "negation", "difficulty": "easy", "ground_truth": [],
                "answer_facts": ["not supported"], "acceptable_answers": ["not supported"],
                "refusal_conditions": [], "expected_refusal": False, "dataset_version": "fixture",
                "semantic_cluster": "same", "split": "validation"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(json.dumps(case) + "\n" + json.dumps({**case, "id": "h", "split": "holdout"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "semantic_cluster"):
                runner._cases(path)


if __name__ == "__main__":
    unittest.main()
