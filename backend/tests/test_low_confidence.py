"""验证真实路由及事务读写；仅使用进程内临时关系库，不连接配置中的 MySQL。"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import current_timestamp
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import current_identity
from app.main import app
from app.persistence.mysql import chat
from app.persistence.mysql.models import (
    Base, Conversation, LowConfidenceQuestion, Message, ReviewQueue, User, utc_now_naive,
)


@compiles(DATETIME, "sqlite")
def _compile_mysql_datetime_for_transient_tests(_element, _compiler, **_kwargs):
    """仅使 MySQL 微秒列可在进程内运行；不改变应用的 MySQL 方言。"""
    return "DATETIME"

@compiles(current_timestamp, "sqlite")
def _compile_mysql_timestamp_default_for_transient_tests(_element, _compiler, **_kwargs):
    return "CURRENT_TIMESTAMP"


class LowConfidencePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        # 显式列出的表必须覆盖这些测试会真正触碰到的外键父表：PRAGMA foreign_keys=ON 后，
        # SQLite 在写入 low_confidence_questions 时会去解析 matched_review_id 指向的 review_queue，
        # 父表缺失就直接报 “no such table”。这里只建夹具用到的表，不引入其余业务表。
        Base.metadata.create_all(self.engine, tables=[
            User.__table__, Conversation.__table__, Message.__table__,
            ReviewQueue.__table__, LowConfidenceQuestion.__table__,
        ])
        self.factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.db_patch = patch.object(chat, "get_session_factory", return_value=self.factory)
        self.db_patch.start()
        with self.factory.begin() as session:
            owner = User(name="甲", email="owner@test.invalid", role="user")
            other = User(name="乙", email="other@test.invalid", role="user")
            session.add_all([owner, other])
            session.flush()
            conversation = Conversation(user_id=owner.id)
            other_conversation = Conversation(user_id=other.id)
            session.add_all([conversation, other_conversation])
            session.flush()
            self.owner, self.other = owner.id, other.id
            self.conversation_id = conversation.id
            self.other_conversation_id = other_conversation.id
        app.dependency_overrides[current_identity] = lambda: {"id": self.owner, "role": "user"}
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.pop(current_identity, None)
        self.db_patch.stop()
        self.engine.dispose()

    def rows(self):
        with self.factory() as session:
            return session.scalars(select(LowConfidenceQuestion).order_by(LowConfidenceQuestion.id)).all()

    def feedback(self, message_id, rating, conversation_id=None):
        conversation_id = conversation_id or self.conversation_id
        return self.client.post(
            f"/api/conversations/{conversation_id}/messages/{message_id}/feedback",
            json={"rating": rating},
        )

    def test_transaction_links_every_item_and_rolls_back_invalid_record(self):
        pair = chat.create_message_pair(self.conversation_id, self.owner, "问题甲和问题乙", "first")
        with self.factory() as session:
            saved = session.scalars(select(Message).where(
                Message.conversation_id == self.conversation_id
            ).order_by(Message.created_at)).all()
            self.assertEqual(
                [(message.sender_role, message.status) for message in saved],
                [("user", "complete"), ("assistant", "streaming")],
            )
        incomplete = [{"original_question": "问题甲", "entrypoint": "retrieval_low_confidence"}]
        with self.assertRaises(KeyError):
            chat.finish_assistant_message(
                pair.assistant_message_id, self.conversation_id, self.owner, "拒答", "complete",
                low_confidence=incomplete,
            )
        with self.factory() as session:
            self.assertEqual(session.get(Message, pair.assistant_message_id).status, "streaming")
            self.assertEqual(session.scalars(select(LowConfidenceQuestion)).all(), [])
        items = [
            {"original_question": "问题甲", "entrypoint": "retrieval_low_confidence", "reason": "未找到证据"},
            {"original_question": "问题乙", "entrypoint": "generation_missing_citation", "reason": "未引用"},
        ]
        chat.finish_assistant_message(
            pair.assistant_message_id, self.conversation_id, self.owner, "两项均无法核实", "complete",
            low_confidence=items,
        )
        rows = self.rows()
        self.assertEqual(
            {(row.original_question, row.entrypoint, row.reason) for row in rows},
            {(item["original_question"], item["entrypoint"], item["reason"]) for item in items},
        )
        self.assertEqual(
            {(row.source_user_message_id, row.source_assistant_message_id) for row in rows},
            {(pair.user_message_id, pair.assistant_message_id)},
        )
        self.assertFalse(chat.finish_assistant_message(
            pair.assistant_message_id, self.conversation_id, self.owner, "不能覆盖", "complete",
            low_confidence=items,
        ))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.feedback(pair.assistant_message_id, "unsatisfied").status_code, 200)
        feedback_rows = self.rows()
        self.assertEqual(len(feedback_rows), 3)
        self.assertEqual(
            [(row.original_question, row.source_user_message_id) for row in feedback_rows
             if row.entrypoint == "user_feedback_unresolved"],
            [("问题甲和问题乙", pair.user_message_id)],
        )

    def test_feedback_route_rejects_cross_owner_non_assistant_and_non_complete(self):
        pair = chat.create_message_pair(self.conversation_id, self.owner, "询问", "pair")
        self.assertEqual(self.feedback(pair.assistant_message_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.feedback(pair.user_message_id, "unsatisfied").status_code, 409)
        chat.finish_assistant_message(pair.assistant_message_id, self.conversation_id, self.owner, "错误", "error")
        self.assertEqual(self.feedback(pair.assistant_message_id, "unsatisfied").status_code, 409)
        chat.create_message_pair(self.other_conversation_id, self.other, "私人问题", "private")
        with self.factory() as session:
            private_id = session.scalar(select(Message.id).where(
                Message.conversation_id == self.other_conversation_id,
                Message.sender_role == "assistant",
            ))
        self.assertEqual(self.feedback(private_id, "unsatisfied", self.other_conversation_id).status_code, 404)
        self.assertEqual(self.feedback(private_id, "unsatisfied").status_code, 404)
        self.assertEqual(self.rows(), [])

    def test_feedback_satisfied_only_persists_and_unsatisfied_records_once(self):
        satisfied = chat.create_message_pair(self.conversation_id, self.owner, "已经答完的问题", "yes")
        chat.finish_assistant_message(
            satisfied.assistant_message_id, self.conversation_id, self.owner, "有效回答", "complete"
        )
        self.assertEqual(self.feedback(satisfied.assistant_message_id, "satisfied").json(), {"rating": "satisfied"})
        self.assertEqual(self.feedback(satisfied.assistant_message_id, "satisfied").status_code, 200)
        self.assertEqual(self.feedback(satisfied.assistant_message_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.rows(), [])
        unsatisfied = chat.create_message_pair(self.conversation_id, self.owner, "仍未解决的问题", "no")
        chat.finish_assistant_message(
            unsatisfied.assistant_message_id, self.conversation_id, self.owner, "无效回答", "complete"
        )
        self.assertEqual(self.feedback(unsatisfied.assistant_message_id, "unsatisfied").status_code, 200)
        self.assertEqual(self.feedback(unsatisfied.assistant_message_id, "unsatisfied").status_code, 200)
        self.assertEqual(self.feedback(unsatisfied.assistant_message_id, "satisfied").status_code, 409)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].entrypoint, rows[0].original_question,
             rows[0].source_user_message_id, rows[0].source_assistant_message_id),
            ("user_feedback_unresolved", "仍未解决的问题",
             unsatisfied.user_message_id, unsatisfied.assistant_message_id),
        )
        with self.factory() as session:
            self.assertEqual(session.get(Message, satisfied.assistant_message_id).feedback, "satisfied")
            self.assertEqual(session.get(Message, unsatisfied.assistant_message_id).feedback, "unsatisfied")

    def test_feedback_does_not_attribute_answer_across_staff_or_system_message(self):
        now = utc_now_naive()
        with self.factory.begin() as session:
            messages = [
                Message(conversation_id=self.conversation_id, sender_role="user", content="旧问法",
                        status="complete", created_at=now),
                Message(conversation_id=self.conversation_id, sender_role="staff", content="人工介入",
                        status="complete", created_at=now + timedelta(microseconds=1)),
                Message(conversation_id=self.conversation_id, sender_role="assistant", content="孤立回答",
                        status="complete", created_at=now + timedelta(microseconds=2)),
                Message(conversation_id=self.conversation_id, sender_role="user", content="带键问法",
                        client_message_id="keyed", status="complete", created_at=now + timedelta(microseconds=3)),
                Message(conversation_id=self.conversation_id, sender_role="system", content="系统消息",
                        status="complete", created_at=now + timedelta(microseconds=4)),
                Message(conversation_id=self.conversation_id, sender_role="assistant", content="隔断的回答",
                        client_message_id="keyed", status="complete", created_at=now + timedelta(microseconds=5)),
            ]
            session.add_all(messages)
            session.flush()
            orphan_id, crossed_id = messages[2].id, messages[5].id
            staff_id, system_id = messages[1].id, messages[4].id
        self.assertEqual(self.feedback(orphan_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.feedback(crossed_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.feedback(staff_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.feedback(system_id, "unsatisfied").status_code, 409)
        self.assertEqual(self.rows(), [])

    def test_duplicate_send_replays_completed_pair_without_new_messages(self):
        first = chat.create_message_pair(self.conversation_id, self.owner, "同一请求", "retry")
        with self.assertRaises(ValueError):
            chat.create_message_pair(self.conversation_id, self.owner, "同一请求", "retry")
        chat.finish_assistant_message(
            first.assistant_message_id, self.conversation_id, self.owner, "结果", "complete"
        )
        replay = chat.create_message_pair(self.conversation_id, self.owner, "同一请求", "retry")
        self.assertEqual((replay.user_message_id, replay.assistant_message_id),
                         (first.user_message_id, first.assistant_message_id))
        self.assertFalse(replay.should_generate)
        self.assertEqual(replay.replay_content, "结果")
        with self.assertRaises(ValueError):
            chat.create_message_pair(self.conversation_id, self.owner, "不同请求", "retry")
        with self.factory() as session:
            self.assertEqual(len(session.scalars(select(Message)).all()), 2)

    def test_failed_retry_cannot_answer_a_later_user_message(self):
        first = chat.create_message_pair(self.conversation_id, self.owner, "原问题", "old")
        chat.finish_assistant_message(
            first.assistant_message_id, self.conversation_id, self.owner, "失败", "error"
        )
        later = chat.create_message_pair(self.conversation_id, self.owner, "后续问题", "new")
        chat.finish_assistant_message(
            later.assistant_message_id, self.conversation_id, self.owner, "后续答案", "complete"
        )
        with self.assertRaisesRegex(ValueError, "后续消息"):
            chat.create_message_pair(self.conversation_id, self.owner, "原问题", "old")
        self.assertEqual(
            chat.recent_messages(self.conversation_id, self.owner, first.assistant_message_id)[-1]["content"],
            "后续答案",
        )


if __name__ == "__main__":
    unittest.main()
