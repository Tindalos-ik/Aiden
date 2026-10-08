"""观测统计边界回归，不连接模型、业务数据库或 Langfuse。"""
from unittest.mock import patch

import httpx

from app.services.observability import console as c


def generation(oid, bucket='cost_intent_order', **fields):
    if 'usageDetails' in fields:
        fields.setdefault('metadata', {'aiden_usage_source': 'provider',
                                       'aiden_provider_usage': fields['usageDetails']})
    return {'id': oid, 'traceId': 'trace', 'type': 'GENERATION', 'name': bucket,
            'usageDetails': {}, 'costDetails': {}, **fields}


def test_missing_usage_zero_and_per_field_coverage():
    rows = [generation('missing'), generation('zero', usageDetails={'input': 0, 'output': 0, 'total': 0}),
            generation('partial', usageDetails={'input': 4})]
    value = c.tokens(rows)
    assert value['input'] == 4 and value['output'] == 0
    assert value['field_coverage']['input'] == {'known': 2, 'missing': 1}
    assert value['field_coverage']['total'] == {'known': 1, 'missing': 2}
    assert c.tokens([rows[0]])['total'] is None
    assert c.tokens([generation('historical', usageDetails={'total': 999}, metadata={})])['total'] is None


def test_cost_only_resolved_details_currency_and_zero():
    rows = [generation('missing', totalCost=999), generation('zero', costDetails={'total': 0}),
            generation('eur', costDetails={'total': 2}, currency='EUR')]
    by_currency = {v['currency']: v for v in c.costs(rows)}
    assert by_currency['USD']['known_cost'] == 0
    assert by_currency['USD']['known_generations'] == 1
    assert by_currency['USD']['total_generations'] == 2
    assert by_currency['USD']['completeness'] == 'partial'
    assert by_currency['EUR']['known_cost'] == 2
    assert by_currency['EUR']['known_generations'] == by_currency['EUR']['total_generations'] == 1
    assert by_currency['EUR']['completeness'] == 'complete'
    eur_unknown = c.costs([generation('eur-missing', currency='EUR')])[0]
    assert eur_unknown['currency'] == 'EUR' and eur_unknown['known_cost'] is None
    assert eur_unknown['known_generations'] == 0 and eur_unknown['total_generations'] == 1
    assert eur_unknown['completeness'] == 'unknown'
    multiple_known = {row['currency']: row for row in c.costs([
        generation('usd-known', costDetails={'total': 1}),
        generation('eur-known', costDetails={'total': 2}, currency='EUR'),
    ])}
    assert multiple_known['USD']['completeness'] == 'complete'
    assert multiple_known['EUR']['completeness'] == 'complete'


def test_root_status_missing_and_child_failure():
    root = {'id': 'root', 'type': 'CHAIN', 'name': 'aiden_support', 'isRootObservation': True,
            'endTime': '2026-10-01T00:01:00Z', 'level': 'DEFAULT'}
    child = generation('child', parentObservationId='root', level='ERROR')
    assert c.status(root) == 'unknown'
    root['metadata'] = {'aiden_completion': 'success', 'aiden_source': 'online'}
    assert c.trace_summary('trace', [root, child])['status'] == 'success'
    assert c.is_online([root, child])
    root['metadata']['aiden_source'] = 'evaluation'
    assert not c.is_online([root, child])


def test_cursor_deduplication_and_explicit_truncation():
    responses = [httpx.Response(200, json={'data': [generation('a')], 'meta': {'cursor': 'next'}}),
                 httpx.Response(200, json={'data': [generation('a'), generation('b')], 'meta': {}})]
    class Client:
        def __init__(self, **kwargs):
            self.calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, params):
            response = responses[self.calls]
            response.request = httpx.Request('GET', 'https://example.invalid')
            self.calls += 1
            return response
    with patch.object(c.settings, 'langfuse_enabled', True), patch.object(c.httpx, 'Client', Client):
        rows, truncated, error = c.read_rows(limit=2)
        assert {r['id'] for r in rows} == {'a', 'b'} and not truncated and error is None
        rows, truncated, error = c.read_rows(limit=1)
        assert len(rows) == 1 and truncated and error is None


def test_multi_intent_request_keeps_shared_generation_once():
    root = {'id': 'root', 'traceId': 'trace', 'type': 'CHAIN', 'name': 'aiden_support',
            'isRootObservation': True, 'metadata': {'aiden_source': 'online'}}
    rows = [root, generation('shared', 'cost_bucket_intent_classification', costDetails={'total': 1}),
            generation('order', costDetails={'total': 2}),
            generation('logistics', 'cost_intent_logistics', costDetails={'total': 3})]
    with patch.object(c, 'read_rows', return_value=(rows, False, None)):
        result = c.query(kind='overview', limit=20, intent='order')['data']
    assert result['counts']['generations'] == 3
    assert result['costs'][0]['known_cost'] == 6
    attribution = {r['bucket']: r for r in result['attribution']}
    assert attribution['intent_classification']['generations'] == 1
    assert sum(r['costs'][0]['known_cost'] for r in attribution.values()) == 6
    assert c.trace_summary('trace', rows)['intents'] == []


def test_availability_without_fake_fallback():
    class Client:
        code = 401
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, params):
            if self.code == 0:
                raise httpx.ConnectError('private upstream secret')
            return httpx.Response(self.code, json={}, request=httpx.Request('GET', 'https://example.invalid'))
    with patch.object(c.settings, 'langfuse_enabled', False):
        assert c.read_rows(limit=10) == ([], False, 'unconfigured')
    with patch.object(c.settings, 'langfuse_enabled', True), patch.object(c.httpx, 'Client', Client):
        for code, state in [(401, 'auth_failed'), (403, 'auth_failed'), (410, 'unsupported'), (404, 'unsupported'), (0, 'connection_failed')]:
            Client.code = code
            response = c.query(kind='overview', limit=10)
            assert response['state'] == state and response['data'] is None
            assert 'private' not in str(response)
    with patch.object(c, 'read_rows', return_value=([], False, None)):
        assert c.query(kind='overview', limit=10)['state'] == 'empty'


def test_real_graph_root_not_trace_wrapper_and_missing_parent():
    wrapper = {'id': 'wrapper', 'traceId': 'trace', 'name': 'aiden_support', 'isRootObservation': True}
    root = {'id': 'graph', 'traceId': 'trace', 'name': 'aiden_support', 'parentObservationId': 'wrapper',
            'metadata': {'is_langchain_root': True}, 'startTime': '2026-10-01T00:00:00Z',
            'endTime': '2026-10-01T00:00:02Z'}
    assert c.root_rows([wrapper, root]) == [root]
    assert c.durations([root]) == {'samples': 1, 'mean_ms': 2000, 'p50_ms': 2000, 'p95_ms': 2000}
    with patch.object(c, 'read_rows', return_value=([root], True, None)):
        detail = c.query(kind='detail', source='all', limit=10)['data']
    assert detail['structure']['missing_parents'] == 1 and detail['structure']['truncated']


def test_provider_response_metadata_preserves_missing_and_confirmed_zero():
    from types import SimpleNamespace
    from app.services.observability.callback import SupportCallbackHandler
    from langfuse.langchain import CallbackHandler
    captured = []
    observation = SimpleNamespace(update=lambda **kwargs: captured.append(kwargs))
    handler = object.__new__(SupportCallbackHandler)
    handler.runs = {'generation': observation}
    response = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
        usage_metadata={'input_tokens': 0, 'output_tokens': 4}))]], llm_output=None)
    with patch.object(CallbackHandler, 'on_llm_end'):
        handler.on_llm_end(response, run_id='generation')
    marker = captured[0]['metadata']
    row = generation('generation', metadata=marker)
    assert c.tokens([row])['input'] == 0
    assert c.tokens([row])['output'] == 4
    assert c.tokens([row])['total'] is None


def test_detail_projection_never_exposes_payload_or_arbitrary_metadata():
    row = generation('safe-id', name='sensitive unknown name', input='sensitive input',
                     output='sensitive output', prompt='sensitive prompt',
                     statusMessage='sensitive upstream error', level='ERROR',
                     metadata={'phone': 'sensitive phone', 'arbitrary': 'sensitive metadata'},
                     usageDetails={'total': 0, 'private_field': 999},
                     costDetails={'total': 0, 'private_field': 999})
    projected = c.observation_summary(row)
    assert 'sensitive' not in str(projected)
    assert projected['error_summary'] == 'upstream_observation_error'
    assert projected['usage_details'] == {'total': 0}
    assert projected['costs'][0]['known_cost'] == 0
    assert projected['tokens']['total'] is None


def test_latest_record_follows_matched_scope_not_read_batch():
    root = {'id': 'root', 'traceId': 'trace', 'type': 'CHAIN', 'name': 'aiden_support',
            'isRootObservation': True, 'startTime': '2026-10-01T00:00:00Z',
            'metadata': {'aiden_source': 'online'}}
    other = generation('other')
    other['traceId'] = 'other'
    other['startTime'] = '2026-10-02T00:00:00Z'
    with patch.object(c, 'read_rows', return_value=([root, other], False, None)):
        online = c.query(kind='overview', limit=20)
        unmatched = c.query(kind='overview', limit=20, model='missing')
    assert online['latest_record_at'] == '2026-10-01T00:00:00+00:00'
    assert online['coverage']['generations_read'] == 1
    assert online['coverage']['matched_generations'] == 0
    assert unmatched['state'] == 'empty' and unmatched['latest_record_at'] is None


def test_generation_cost_trend_preserves_each_currency_and_shared_bucket():
    root = {'id': 'root', 'traceId': 'trace', 'type': 'CHAIN', 'name': 'aiden_support',
            'isRootObservation': True, 'startTime': '2026-10-01T23:59:00Z',
            'metadata': {'aiden_source': 'online'}}
    rows = [
        root,
        generation('shared', 'cost_bucket_intent_classification', model='model-a',
                   startTime='2026-10-01T23:59:30Z', costDetails={'total': 1}),
        generation('order', model='model-a', startTime='2026-10-02T00:00:00Z',
                   costDetails={'total': 2}),
        generation('logistics', 'cost_intent_logistics', model='model-b',
                   startTime='2026-10-02T08:00:00+08:00', costDetails={'total': 3}, currency='EUR'),
        generation('unattributed', 'unknown-name', model='model-b',
                   startTime='2026-10-03T00:00:00Z', costDetails={'total': 0}, currency='EUR'),
    ]
    with patch.object(c, 'read_rows', return_value=(rows, False, None)):
        result = c.query(kind='overview', limit=20, intent='order')['data']
    trend = result['cost_trend']
    assert sum(row['generations'] for row in trend) == result['counts']['generations'] == 4
    shared = [row for row in trend if row['bucket'] == 'intent_classification']
    assert len(shared) == 1 and shared[0]['generations'] == 1
    assert {row['date'] for row in trend if row['bucket'] == 'logistics'} == {'2026-10-02'}
    assert any(row['bucket'] == 'unattributed' for row in trend)
    for total in result['costs']:
        subtotal = sum(cost['known_cost'] for row in trend for cost in row['costs']
                       if cost['currency'] == total['currency'] and cost['known_cost'] is not None)
        assert subtotal == total['known_cost']
