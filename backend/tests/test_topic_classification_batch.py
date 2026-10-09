"""旁路分类服务契约测试；所有持久化边界用受控替身，不连接业务数据库。"""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.persistence.mysql import topic_classification as repo
from app.services.topic_classification import batch
from app.services.topic_classification.data import HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT, input_hash
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION


class TopicClassificationBatchTests(unittest.TestCase):

    def predictor(self, data_mode=HUMAN_REVIEWED):
        predictor = Mock(
            model_version="fixture-real-version",
            taxonomy_version=TAXONOMY_VERSION,
            metadata={"data_mode": data_mode, "evaluation_source": (
                "synthetic_model_prelabels" if data_mode == SYNTHETIC_EXPERIMENT else "human_confirmed"
            )},
        )
        predictor.predict_batch.return_value = [{
            "scores": dict.fromkeys(LABEL_IDS, 0.1),
            "predicted_labels": [], "status": "uncertain",
        }]
        return predictor

    def source(self, source_id):
        return {"source_id": source_id, "raw_question": "商品坏了",
                "input_hash": input_hash("商品坏了"), "created_at": datetime(2026, 1, 1)}

    def test_default_batch_rejects_experiment_before_reading_or_writing(self):
        predictor = self.predictor(SYNTHETIC_EXPERIMENT)
        with patch.object(batch.repo, "list_source_batch") as read, patch.object(batch.repo, "persist_prediction") as persist:
            with self.assertRaisesRegex(ValueError, "explicit matching authorization"):
                batch.run_batch(artifact_dir="fixture", predictor=predictor)
        read.assert_not_called()
        predictor.predict_batch.assert_not_called()
        persist.assert_not_called()

    def test_explicit_experiment_propagates_to_loader_and_results(self):
        predictor = self.predictor(SYNTHETIC_EXPERIMENT)
        with patch.object(batch, "EncoderPredictor", return_value=predictor) as load, patch.object(batch.repo, "list_source_batch", return_value=[self.source("one")]), patch.object(batch.repo, "persist_prediction", return_value="persisted") as persist:
            result = batch.run_batch(artifact_dir="fixture", data_mode=SYNTHETIC_EXPERIMENT, limit=1)
        load.assert_called_once_with("fixture", device="cpu", data_mode=SYNTHETIC_EXPERIMENT)
        self.assertEqual(result[0]["data_mode"], SYNTHETIC_EXPERIMENT)
        self.assertEqual(result[0]["evaluation_source"], "synthetic_model_prelabels")
        self.assertEqual(persist.call_args.kwargs["model_version"], predictor.model_version)
        self.assertEqual(persist.call_args.kwargs["taxonomy_version"], TAXONOMY_VERSION)

    def test_snapshot_checked_before_every_inference_and_changed_content_blocks(self):
        predictor = self.predictor()
        guard = Mock(side_effect=[None, ValueError("artifact changed after job acceptance")])
        outcomes = []
        with patch.object(batch.repo, "list_source_batch", side_effect=[[self.source("one")], [self.source("two")]]), patch.object(batch.repo, "persist_prediction", return_value="unchanged") as persist:
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                batch.run_batch(
                    artifact_dir="fixture", predictor=predictor, limit=2, batch_size=1,
                    before_inference=guard, on_outcome=lambda row, cursor: outcomes.append(row),
                )
        self.assertEqual(guard.call_count, 2)
        predictor.predict_batch.assert_called_once_with(["商品坏了"])
        persist.assert_called_once()
        self.assertEqual(outcomes[0]["persistence_outcome"], "unchanged")
        self.assertEqual(outcomes[0]["evaluation_source"], "human_confirmed")

    def test_first_snapshot_failure_never_predicts_or_persists(self):
        predictor = self.predictor()
        guard = Mock(side_effect=ValueError("dataset changed after job acceptance"))
        with patch.object(batch.repo, "list_source_batch", return_value=[self.source("one")]), patch.object(batch.repo, "persist_prediction") as persist:
            with self.assertRaisesRegex(ValueError, "dataset changed"):
                batch.run_batch(artifact_dir="fixture", predictor=predictor, before_inference=guard)
        predictor.predict_batch.assert_not_called()
        persist.assert_not_called()

    def test_cli_batch_stays_human_reviewed_and_has_no_experiment_override(self):
        from scripts.topic_classifier import main
        with patch.object(batch, "run_batch", return_value=[]) as run, patch("builtins.print"):
            self.assertEqual(main(["batch", "--artifact-dir", "fixture"]), 0)
        self.assertEqual(run.call_args.kwargs["data_mode"], HUMAN_REVIEWED)
        with patch.object(batch, "run_batch") as run, patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                main(["batch", "--artifact-dir", "fixture", "--synthetic-experiment"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_export_redacts_original_question_for_external_annotation(self):
        question = "订单号 SO123456 怎么改收件人？"
        source = {
            "source_id": "src-1", "raw_question": question,
            "input_hash": input_hash(question), "created_at": datetime(2026, 10, 1),
            "conversation_id": "conv-1", "user_message_id": "msg-1",
            "user_message_created_at": datetime(2026, 10, 1), "matched_review_id": "review-1",
        }
        with patch.object(batch.repo, "list_source_batch", return_value=[source]):
            result = batch.export_source_batch(limit=1)
        self.assertNotIn("SO123456", result[0]["text"])
        self.assertNotIn("raw_question", result[0])

    def test_stale_source_refuses_prediction_write(self):
        source = SimpleNamespace(original_question="变更后的来源")

        class Session:
            def __init__(self):
                self.writes = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, _source_id, **_kwargs):
                return source

            def execute(self, _statement):
                self.writes += 1

        class Factory:
            def __init__(self, session):
                self.session = session

            def begin(self):
                return self.session

        scores = {label: 0.0 for label in LABEL_IDS}
        scores["order_change"] = 0.8
        session = Session()
        with patch.object(repo, "get_session_factory", return_value=Factory(session)):
            outcome = repo.persist_prediction(
                source_id="src-1", input_hash_value=input_hash("原问法"), scores=scores,
                predicted_labels=["order_change"], status="predicted",
                model_version="artifact-1", taxonomy_version=TAXONOMY_VERSION,
            )
        self.assertEqual(outcome, "stale_source")
        self.assertEqual(session.writes, 0)
    def test_statistics_exclude_stale_hash_and_taxonomy_and_separate_scope_counts(self):
        questions = {"a": "换收件人", "b": "查发票", "c": "物流到哪"}
        sources = [
            SimpleNamespace(id=key, original_question=question, matched_review_id="review-1" if key != "c" else "review-2")
            for key, question in questions.items()
        ]
        predictions = [
            SimpleNamespace(
                id="a", original_question=questions["a"], input_hash=input_hash(questions["a"]),
                scores={}, predicted_labels=["order_change"], status="predicted",
                taxonomy_version=TAXONOMY_VERSION,
            ),
            SimpleNamespace(
                id="b", original_question=questions["b"], input_hash=input_hash("changed"),
                scores={}, predicted_labels=["invoice"], status="predicted",
                taxonomy_version=TAXONOMY_VERSION,
            ),
            SimpleNamespace(
                id="c", original_question=questions["c"], input_hash=input_hash(questions["c"]),
                scores={}, predicted_labels=["logistics"], status="predicted",
                taxonomy_version="old-taxonomy",
            ),
        ]
        humans = [
            SimpleNamespace(
                source_id="a", input_hash=input_hash(questions["a"]),
                labels=["order_change"], status="confirmed",
            ),
            SimpleNamespace(
                source_id="b", input_hash=input_hash("changed"),
                labels=["invoice"], status="confirmed",
            ),
            SimpleNamespace(
                source_id="c", input_hash=input_hash(questions["c"]),
                labels=[], status="uncertain",
            ),
        ]

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def all(self):
                return self.rows

        class Session:
            def __init__(self):
                self.results = iter([sources, predictions, humans])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _statement):
                return Result(next(self.results))

            def scalar(self, _statement):
                return 9

        session = Session()
        with patch.object(repo, "get_session_factory", return_value=lambda: session):
            result = repo.statistics(model_version="artifact-1")

        self.assertEqual(result["source_unique_count"], 3)
        self.assertEqual(result["review_unique_count"], 2)
        self.assertEqual(result["linked_reviews_lifetime_occurrence_count"], 9)
        self.assertEqual(result["model"]["source_count"], 1)
        self.assertEqual(result["model"]["unclassified_count"], 2)
        self.assertEqual(result["model"]["status_counts"]["unclassified"], 2)
        self.assertEqual(result["human"]["source_count"], 2)
        self.assertEqual(result["human"]["unreviewed_count"], 1)
        self.assertEqual(result["merged_review_label_counts"]["model"]["order_change"], 1)
        self.assertEqual(result["merged_review_label_counts"]["human"]["order_change"], 1)

    def test_human_update_records_real_label_transition(self):
        question = "怎么开票"
        source = SimpleNamespace(original_question=question)
        staff = SimpleNamespace(role="staff")
        review = SimpleNamespace(
            id="human-1", labels=["quality"], status="confirmed",
            note="old", reviewed_by_id="staff-old",
        )

        class Session:
            def __init__(self):
                self.added = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, model, _id, **_kwargs):
                if model is repo.User:
                    return staff
                return source

            def scalar(self, _statement):
                return review

            def add(self, row):
                self.added.append(row)

        class Factory:
            def __init__(self, session):
                self.session = session

            def begin(self):
                return self.session

        session = Session()
        with patch.object(repo, "get_session_factory", return_value=Factory(session)):
            result = repo.record_human_review(
                source_id="src-1", input_hash_value=input_hash(question), staff_id="staff-new",
                labels=["invoice"], status="confirmed", note="checked",
            )
        self.assertEqual(review.labels, ["invoice"])
        self.assertEqual(review.status, "confirmed")
        self.assertEqual(review.note, "checked")
        self.assertEqual(review.reviewed_by_id, "staff-new")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.added[0].previous_labels, ["quality"])
        self.assertEqual(session.added[0].previous_status, "confirmed")
        self.assertEqual(session.added[0].labels, ["invoice"])
        self.assertEqual(session.added[0].status, "confirmed")
        self.assertEqual(session.added[0].staff_id, "staff-new")
