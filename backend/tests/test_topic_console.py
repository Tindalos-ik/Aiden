"""仅消费端边界fixture，不代表真实模型质量、业务写入或会话验收。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from app.services.topic_classification import console, batch
from app.services.topic_classification.data import prepare_dataset, fingerprint, INPUT_VERSION
from app.services.topic_classification.evaluation import evaluate
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION
from app.services.rag.runtime_control import MiningRuntimeControl, AlreadyRunningError

class FixturePredictor:
    model_version = 'a' * 64
    taxonomy_version = TAXONOMY_VERSION
    def predict_batch(self, texts):
        return [{'scores': dict.fromkeys(LABEL_IDS, 0.1), 'predicted_labels': [], 'status': 'uncertain'} for _ in texts]

def fixture_dataset():
    texts = ['商品坏了', '想开电子发票', '何时补货', '忘记登录密码']
    labels = ['quality', 'invoice', 'stock', 'account']
    rows = [{'id': str(i), 'original_question': text, 'source_refs': [{'conversation_id': str(i)}],
             'privacy_confirmed': True, 'privacy_reviewer': 'fixture', 'taxonomy_version': TAXONOMY_VERSION,
             'input_version': INPUT_VERSION, 'labels': [labels[i]], 'classification_status': 'predicted',
             'annotation_status': 'human_confirmed', 'annotation_revision': 'fixture', 'annotator': 'fixture',
             'annotation_method': 'fixture', 'annotation_audit': [], 'data_version': 'fixture-only'} for i, text in enumerate(texts)]
    try:
        prepare_dataset(rows, semantic_review={})
    except ValueError as exc:
        digest = str(exc).split('dataset_hash=')[1]
    return prepare_dataset(rows, semantic_review={'dataset_hash': digest, 'confirmed': True, 'reviewer': 'fixture', 'revision': 'fixture'})

class ConsoleConsumerTests(unittest.TestCase):
    def test_static_diagnostics_never_echo_exception_text(self):
        self.assertEqual(console.validation_error(ValueError('artifact content checksum mismatch or missing weights'), 'artifact')['code'], 'artifact_checksum_invalid')
        self.assertEqual(console.validation_error(ValueError('human semantic review required for dataset_hash=private'), 'dataset')['code'], 'dataset_semantic_unreviewed')
        self.assertEqual(console.validation_error(ValueError('human privacy confirmation required'), 'dataset')['code'], 'dataset_privacy_unreviewed')
        self.assertNotIn('password=secret', json.dumps(console.validation_error(RuntimeError('password=secret'), 'artifact')))

    def test_database_missing_columns_reason_survives(self):
        with patch.object(console.repo, 'readiness', return_value={'ready': False, 'missing_tables': [], 'missing_columns': {'topic_classification_predictions': ['scores']}}), patch.object(console.repo, 'model_versions', return_value=['old']), patch.object(console, 'config', return_value={'artifact': None, 'dataset': None, 'runtime': Path(tempfile.gettempdir())/'topic-test-none', 'reports': Path(tempfile.gettempdir())/'topic-test-none', 'device': 'cpu'}), patch.object(console, 'jobs', return_value=[]):
            result = console.availability()
        self.assertEqual(result['checks'][1]['code'], 'schema_missing')
        self.assertEqual(result['model_versions'], ['old'])
        self.assertFalse(result['can_classify'])

    def test_current_filters_ignore_latest_stale_and_separate_human(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from datetime import datetime
        from app.services.topic_classification.data import input_hash
        source = SimpleNamespace(id='source', original_question='姓名：张三，鞋坏了', created_at=datetime(2026, 1, 1),
                                 conversation_id='conversation', source_user_message_id='message', matched_review_id='review')
        common = {'source_id': source.id, 'scores': dict.fromkeys(LABEL_IDS, 0.1), 'model_version': 'v',
                  'taxonomy_version': TAXONOMY_VERSION, 'created_at': source.created_at, 'updated_at': source.created_at}
        stale = SimpleNamespace(**common, id='stale', input_hash='old', predicted_labels=['invoice'], status='predicted')
        current = SimpleNamespace(**common, id='current', input_hash=input_hash(source.original_question), predicted_labels=['quality'], status='predicted')
        human = SimpleNamespace(id='human', source_id=source.id, input_hash=input_hash(source.original_question),
                                labels=[], status='insufficient_context', reviewed_by_id='staff',
                                created_at=source.created_at, updated_at=source.created_at)
        def run(**filters):
            session = MagicMock()
            session.__enter__.return_value = session
            session.scalars.side_effect = [MagicMock(all=lambda: [source]), MagicMock(all=lambda: [stale, current]),
                                          MagicMock(all=lambda: [human])]
            with patch.object(console.repo, 'get_session_factory', return_value=lambda: session):
                return console.repo.result_rows(model_version='v', **filters)
        result = run(topic='quality', current_prediction=True, has_human=True)
        self.assertEqual(result[0]['current_prediction']['id'], 'current')
        self.assertFalse(result[0]['latest_prediction']['current'])
        self.assertEqual(result[0]['human_current']['status'], 'insufficient_context')
        self.assertNotIn('张三', result[0]['question'])
        self.assertEqual(run(topic='invoice'), [])
        self.assertEqual(run(current_prediction=False), [])

    def test_accepted_model_version_gate_precedes_batch_reads(self):
        with patch.object(batch, 'EncoderPredictor', return_value=FixturePredictor()), patch.object(batch.repo, 'list_source_batch') as read:
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                batch.run_batch(artifact_dir='fixture', expected_model_version='different')
        read.assert_not_called()

    def test_lock_released_without_unlink_and_duplicate_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'task.lock'; first = MiningRuntimeControl(path); second = MiningRuntimeControl(path)
            first.acquire()
            try:
                with self.assertRaises(AlreadyRunningError): second.acquire()
            finally: console.release(first)
            self.assertTrue(path.exists())
            second.acquire(); console.release(second)
            self.assertTrue(path.exists())

    def test_partial_outcomes_preserved_before_predictor_failure(self):
        from datetime import datetime
        rows = [{'source_id': 'one', 'raw_question': '商品坏了', 'input_hash': 'hash', 'created_at': datetime(2026, 1, 1)}]
        outcomes = []
        with patch.object(batch.repo, 'list_source_batch', side_effect=[rows, RuntimeError('fixture-private')]), patch.object(batch.repo, 'persist_prediction', return_value='persisted'):
            with self.assertRaises(RuntimeError):
                batch._run_with_predictor(FixturePredictor(), start_at=None, end_at=None, after=None, limit=2, batch_size=1, on_outcome=lambda row, cursor: outcomes.append((row, cursor)))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0]['persistence_outcome'], 'persisted')
        self.assertEqual(outcomes[0][1]['source_id'], 'one')

    def test_report_recomputed_and_extras_not_exposed(self):
        dataset = fixture_dataset(); report = evaluate(FixturePredictor(), dataset['splits']['test'], split_hash=dataset['split_hashes']['test'])
        report.update(dataset_hash=dataset['dataset_hash'], split_hashes=dataset['split_hashes'], baseline_configuration={'base_url': 'secret-endpoint'})
        identifier = str(uuid4()); payload = {'id': identifier, 'created_at': '2026-10-08', 'dataset': dataset, 'report': report, 'predictions': report['predictions']}
        payload['evidence_hash'] = fingerprint({k: payload[k] for k in ('report', 'dataset', 'predictions')})
        with tempfile.TemporaryDirectory() as tmp, patch.object(console, 'config', return_value={'reports': Path(tmp)}):
            path = Path(tmp)/(identifier+'.json'); path.write_text(json.dumps(payload), encoding='utf-8')
            value = console.report_detail(identifier)
            self.assertNotIn('secret-endpoint', json.dumps(value))
            self.assertIn('证据不足', value['conclusions']['quality'])
            tampered = copy.deepcopy(payload); tampered['report']['metrics']['micro_f1'] = 0.99
            tampered['evidence_hash'] = fingerprint({k: tampered[k] for k in ('report', 'dataset', 'predictions')})
            path.write_text(json.dumps(tampered), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'evaluation_payload'): console.report_detail(identifier)
            tampered = copy.deepcopy(payload); tampered['dataset']['splits']['test'][0]['privacy_confirmed'] = False
            path.write_text(json.dumps(tampered), encoding='utf-8')
            with self.assertRaises(ValueError): console.report_detail(identifier)

if __name__ == '__main__': unittest.main()
