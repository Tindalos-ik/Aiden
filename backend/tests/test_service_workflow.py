"""在独立 MySQL 测试库验证售后及工单闭环；不接触开发数据。"""

import os
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from app.api.deps import current_identity  # noqa: E402
from app.main import app  # noqa: E402
from app.persistence.mysql.database import get_session_factory  # noqa: E402
from app.persistence.mysql.models import (  # noqa: E402
    AfterSaleRequest, Conversation, Message, Order, OrderItem, Policy, Ticket, User, utc_now_naive,
)
from app.persistence.mysql.service_workflow import (  # noqa: E402
    change_request, change_ticket, preview_request, submit_request,
)
from app.services.tools.after_sale_submit import submit_after_sale  # noqa: E402


class ServiceWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_url = os.environ["DATABASE_URL"]
        os.environ["DATABASE_URL"] = make_url(cls.original_url).set(
            database="aiden_service_flow_test").render_as_string(hide_password=False)
        config = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        config.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
        command.upgrade(config, "head")

    @classmethod
    def tearDownClass(cls):
        os.environ["DATABASE_URL"] = cls.original_url

    def setUp(self):
        suffix = uuid4().hex[:10]
        with get_session_factory().begin() as session:
            owner = User(name="申请用户", email=f"owner-{suffix}@test.invalid", role="user")
            other = User(name="其他用户", email=f"other-{suffix}@test.invalid", role="user")
            staff = User(name="处理员工", email=f"staff-{suffix}@test.invalid", role="staff")
            session.add_all((owner, other, staff))
            session.flush()
            order = Order(order_no=f"TEST-{suffix}", user_id=owner.id,
                          total_amount=Decimal("99.00"), status="delivered")
            session.add(order)
            session.flush()
            item = OrderItem(order_id=order.id, product_name_snapshot="测试商品",
                             quantity=1, unit_price=Decimal("99.00"), line_total=Decimal("99.00"))
            policy = Policy(policy_key=f"refund-{suffix}", name="退款测试政策", version="1",
                            content="需核对订单及原因，由员工审核。",
                            effective_from=utc_now_naive() - timedelta(days=1))
            return_policy = Policy(policy_key=f"return-{suffix}", name="退货测试政策", version="1",
                                   content="核实商品问题后办理退货。",
                                   effective_from=utc_now_naive() - timedelta(days=1))
            exchange_policy = Policy(policy_key=f"exchange-{suffix}", name="换货测试政策", version="1",
                                     content="核实商品问题后办理换货。",
                                     effective_from=utc_now_naive() - timedelta(days=1))
            conversation = Conversation(user_id=owner.id, subject="测试会话")
            other_conversation = Conversation(user_id=other.id, subject="其他用户会话")
            session.add_all((item, policy, return_policy, exchange_policy, conversation, other_conversation))
            session.flush()
            ticket = Ticket(ticket_no=f"TK-{suffix}", conversation_id=conversation.id,
                            user_id=owner.id, issue_type="complaint", description="投诉需核实", status="open")
            other_ticket = Ticket(ticket_no=f"TK-OTHER-{suffix}", conversation_id=other_conversation.id,
                                  user_id=other.id, issue_type="complaint", description="其他用户工单", status="open")
            session.add_all((ticket, other_ticket))
            session.flush()
            self.owner, self.other, self.staff = owner.id, other.id, staff.id
            self.order_no, self.item_id, self.policy_id = order.order_no, item.id, policy.id
            self.return_policy_id, self.exchange_policy_id = return_policy.id, exchange_policy.id
            self.ticket_id, self.ticket_no, self.conversation_id = ticket.id, ticket.ticket_no, conversation.id
            self.other_ticket_id, self.other_ticket_no = other_ticket.id, other_ticket.ticket_no

    def submit(self, *, key=None, user=None, reason="商品故障", item=None, policy=None, kind="refund",
               confirmed=True, ticket=None):
        with get_session_factory().begin() as session:
            row, repeated = submit_request(
                session, user_id=user or self.owner, order_no=self.order_no,
                order_item_id=item or self.item_id, request_type=kind, reason=reason,
                policy_reference=policy or self.policy_id,
                submission_key=key or uuid4().hex, confirmed=confirmed,
                source_ticket_no=ticket,
            )
            return row.id, row.request_no, repeated

    def test_first_submission_and_missing_information(self):
        with get_session_factory()() as session:
            preview = preview_request(session, self.owner, self.order_no, "refund")
            self.assertEqual(preview["items"][0]["id"], self.item_id)
            self.assertTrue(preview["policies"])
        for change in ({"reason": " "}, {"confirmed": False}, {"item": str(uuid4())}, {"policy": "expired"}):
            with self.subTest(change=change), self.assertRaises((ValueError, LookupError)):
                self.submit(**change)
        request_id, request_no, repeated = self.submit()
        self.assertTrue(request_no.startswith("AS"))
        self.assertFalse(repeated)
        with get_session_factory()() as session:
            self.assertEqual(session.get(AfterSaleRequest, request_id).status, "pending")

    def test_user_cancel_repeat_and_concurrent_submission(self):
        key = uuid4().hex
        first = self.submit(key=key)
        self.assertEqual(self.submit(key=key), (first[0], first[1], True))
        self.assertEqual(self.submit(key=uuid4().hex)[0], first[0])
        with get_session_factory().begin() as session:
            cancelled = change_request(session, first[0], self.owner, "cancelled", user_cancel=True)
            self.assertEqual(cancelled.resolved_by_id, self.owner)
        with get_session_factory()() as session:
            self.assertEqual(session.get(AfterSaleRequest, first[0]).status, "cancelled")
        # 新幂等键可以重新申请；同一键仍代表原提交，不会暗中复活。
        self.assertNotEqual(self.submit()[0], first[0])

    def test_parallel_requests_create_only_one_active_application(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(results[0][0], results[1][0])
        with get_session_factory()() as session:
            self.assertEqual(len(list(session.scalars(select(AfterSaleRequest).where(
                AfterSaleRequest.user_id == self.owner,
                AfterSaleRequest.order_item_id == self.item_id,
                AfterSaleRequest.status == "pending")))), 1)

    def test_legacy_order_level_request_is_not_duplicated(self):
        with get_session_factory().begin() as session:
            order = session.scalar(select(Order).where(Order.order_no == self.order_no))
            legacy = AfterSaleRequest(request_no=f"AS-LEGACY-{uuid4().hex[:12]}",
                                      user_id=self.owner, order_id=order.id,
                                      order_item_id=None, request_type="refund", reason="旧申请",
                                      status="pending")
            session.add(legacy)
            session.flush()
            legacy_id = legacy.id
        request_id, _, repeated = self.submit()
        self.assertEqual(request_id, legacy_id)
        self.assertTrue(repeated)

    def test_return_exchange_and_cross_type_conflict(self):
        return_id, _, _ = self.submit(kind="return", policy=self.return_policy_id)
        with self.assertRaises(ValueError):
            self.submit(kind="exchange", policy=self.exchange_policy_id)
        with get_session_factory().begin() as session:
            change_request(session, return_id, self.staff, "approved")
        with get_session_factory().begin() as session:
            change_request(session, return_id, self.staff, "processing", "安排退货")
        with get_session_factory().begin() as session:
            completed = change_request(session, return_id, self.staff, "completed", "商品已处理")
            self.assertEqual(completed.resolved_by_id, self.staff)
        exchange_id, _, repeated = self.submit(kind="exchange", policy=self.exchange_policy_id)
        self.assertNotEqual(exchange_id, return_id)
        self.assertFalse(repeated)

    def test_cross_user_access_and_staff_permission(self):
        request_id, _, _ = self.submit()
        with get_session_factory()() as session:
            with self.assertRaises(LookupError):
                preview_request(session, self.other, self.order_no, "refund")
        with get_session_factory().begin() as session:
            with self.assertRaises(LookupError):
                change_request(session, request_id, self.other, "cancelled", user_cancel=True)
        client = TestClient(app)
        app.dependency_overrides[current_identity] = lambda: {"id": self.other, "role": "user"}
        try:
            self.assertEqual(client.get("/api/service/after-sales").json(), [])
            self.assertEqual(client.get("/api/staff/service/tickets").status_code, 403)
        finally:
            app.dependency_overrides.clear()

    def test_staff_review_external_refund_and_invalid_transitions(self):
        request_id, _, _ = self.submit()
        client = TestClient(app)
        app.dependency_overrides[current_identity] = lambda: {"id": self.staff, "role": "staff"}
        try:
            listed = client.get("/api/staff/service/after-sales")
            self.assertEqual(listed.status_code, 200)
            current = next(row for row in listed.json() if row["id"] == request_id)
            self.assertEqual(current["itemName"], "测试商品")
            self.assertIn("需核对订单", current["policyEvidence"])
        finally:
            app.dependency_overrides.clear()
        with get_session_factory().begin() as session:
            with self.assertRaises(ValueError):
                change_request(session, request_id, self.staff, "completed", "无支付动作")
            row = change_request(session, request_id, self.staff, "approved")
            self.assertEqual(row.reviewed_by_id, self.staff)
            self.assertIsNotNone(row.reviewed_at)
        with get_session_factory().begin() as session:
            row = change_request(session, request_id, self.staff, "awaiting_external_refund", "转交支付系统")
            self.assertIsNotNone(row.processing_at)
            self.assertEqual(row.processing_by_id, self.staff)
            with self.assertRaises(ValueError):
                change_request(session, request_id, self.staff, "completed", "款项已退")

    def test_ticket_accept_process_close_and_link_application(self):
        request_id, _, _ = self.submit(ticket=self.ticket_no)
        with get_session_factory().begin() as session:
            row = change_ticket(session, self.ticket_id, self.staff, "in_progress")
            self.assertEqual(row.after_sale_request_id, request_id)
            self.assertEqual(row.assigned_staff_id, self.staff)
            self.assertIsNotNone(row.accepted_at)
        with get_session_factory().begin() as session:
            with self.assertRaises(ValueError):
                change_ticket(session, self.ticket_id, self.other, "resolved", "无权处理")
            row = change_ticket(session, self.ticket_id, self.staff, "resolved", "已跟进申请")
            self.assertIsNotNone(row.resolved_at)
        with get_session_factory().begin() as session:
            row = change_ticket(session, self.ticket_id, self.staff, "closed")
            self.assertEqual(row.closed_by_id, self.staff)
            self.assertIsNotNone(row.closed_at)
            with self.assertRaises(ValueError):
                change_ticket(session, self.ticket_id, self.staff, "in_progress")

    def test_ticket_and_application_link_must_share_owner(self):
        with self.assertRaises(LookupError):
            self.submit(ticket=self.other_ticket_no)
        request_id, _, _ = self.submit()
        with get_session_factory().begin() as session:
            with self.assertRaises(LookupError):
                change_ticket(session, self.other_ticket_id, self.staff, "in_progress",
                              after_sale_request_id=request_id)
        with get_session_factory()() as session:
            self.assertIsNone(session.get(Ticket, self.other_ticket_id).after_sale_request_id)

    def test_dialogue_requires_saved_policy_and_exact_confirmation(self):
        now = utc_now_naive()
        with get_session_factory().begin() as session:
            previous = Message(conversation_id=self.conversation_id, sender_role="assistant",
                               content="政策已核对，请确认提交", status="complete",
                               workflow_state={"refund": {"stage": "confirm", "order_no": self.order_no,
                                                          "reason": "商品故障",
                                                          "policy_id": self.policy_id}}, created_at=now)
            user = Message(conversation_id=self.conversation_id, sender_role="user",
                           content="先不要提交", status="complete", created_at=now + timedelta(microseconds=1))
            current = Message(conversation_id=self.conversation_id, sender_role="assistant",
                              content="", status="streaming", created_at=now + timedelta(microseconds=2))
            session.add_all((previous, user, current))
            session.flush()
            current_id, user_id = current.id, user.id
        def invoke():
            return json.loads(submit_after_sale.func(
                order_no=self.order_no, reason="商品故障", policy_id=self.policy_id,
                user_id=self.owner, conversation_id=self.conversation_id,
                assistant_message_id=current_id, refund_authorized=True))
        self.assertEqual(invoke()["status"], "invalid")
        with get_session_factory().begin() as session:
            session.get(Message, user_id).content = "确认提交"
        result = invoke()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["request_status"], "pending")
        self.assertTrue(invoke()["already_exists"])


if __name__ == "__main__":
    unittest.main()
