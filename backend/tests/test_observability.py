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
    rows = [generation('missing', calculatedTotalCost=999), generation('zero', costDetails={'total': 0}),
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


def test_generation_token_trend_preserves_verified_usage_and_shared_bucket():
    root = {'id': 'root', 'traceId': 'trace', 'type': 'CHAIN', 'name': 'aiden_support',
            'isRootObservation': True, 'startTime': '2026-10-01T23:59:00Z',
            'metadata': {'aiden_source': 'online'}}
    rows = [
        root,
        generation('shared', 'cost_bucket_intent_classification', model='model-a',
                   startTime='2026-10-01T23:59:30Z', costDetails={'total': 1},
                   usageDetails={'input': 5, 'output': 1, 'total': 6}),
        generation('order', model='model-a', startTime='2026-10-02T00:00:00Z',
                   costDetails={'total': 2}, usageDetails={'input': 0, 'output': 2, 'total': 2}),
        generation('logistics', 'cost_intent_logistics', model='model-b',
                   startTime='2026-10-02T08:00:00+08:00', costDetails={'total': 3}, currency='EUR',
                   usageDetails={'input': 3}),
        generation('unattributed', 'unknown-name', model='model-b',
                   startTime='2026-10-03T00:00:00Z', costDetails={'total': 0}, currency='EUR',
                   usageDetails={'total': 999}, metadata={}),
    ]
    with patch.object(c, 'read_rows', return_value=(rows, False, None)):
        result = c.query(kind='overview', limit=20, intent='order')['data']
    trend = result['token_trend']
    assert sum(row['generations'] for row in trend) == result['counts']['generations'] == 4
    shared = [row for row in trend if row['bucket'] == 'intent_classification']
    assert len(shared) == 1 and shared[0]['generations'] == 1
    assert {row['date'] for row in trend if row['bucket'] == 'logistics'} == {'2026-10-02'}
    assert any(row['bucket'] == 'unattributed' for row in trend)
    assert 'cost_trend' not in result
    assert all(set(row) == {'date', 'model', 'bucket', 'generations', 'tokens'} for row in trend)
    for field, expected in [('input', 8), ('output', 3), ('total', 8)]:
        assert sum(row['tokens'][field] or 0 for row in trend) == result['tokens'][field] == expected
    assert next(row for row in trend if row['bucket'] == 'unattributed')['tokens']['total'] is None
    assert next(row for row in trend if row['bucket'] == 'logistics')['tokens']['total'] is None
    for dimension in ('models', 'attribution'):
        for total in result['costs']:
            subtotal = sum(cost['known_cost'] for row in result[dimension] for cost in row['costs']
                           if cost['currency'] == total['currency'] and cost['known_cost'] is not None)
            assert subtotal == total['known_cost']


def test_employee_insights_cover_whole_selection_and_traceable_slow_items():
    rows = []
    for index in range(7):
        tid = f'trace-{index}'
        root = {'id': f'root-{index}', 'traceId': tid, 'name': 'aiden_support',
                'startTime': '2026-10-01T00:00:00Z',
                'endTime': f'2026-10-01T00:00:0{index + 1}Z',
                'metadata': {'aiden_source': 'online'}}
        call = generation(f'call-{index}', traceId=tid, parentObservationId=root['id'],
                          model='model-a', startTime=root['startTime'], endTime=root['endTime'],
                          usageDetails={'input': 0, 'output': 0, 'total': 0},
                          costDetails={'total': index})
        rows.extend([root, call])
    with patch.object(c, 'read_rows', return_value=(rows, False, None)):
        overview = c.query(kind='overview', limit=100, page_size=1)['data']
        listing = c.query(kind='traces', limit=100, page_size=1)['data']
    assert len(listing['items']) == 1 and listing['total'] == 7
    assert overview['data_quality']['provider_verified'] == 7
    assert overview['data_quality']['cost_recorded'] == 7
    assert overview['tokens']['known_generations'] == 7
    assert overview['tokens']['total'] == 0
    assert set(overview['insights']) == {'facts', 'limitations'}
    assert overview['slow_items']['requests'][0]['trace_id'] == 'trace-6'
    assert overview['slow_items']['steps'][0]['observation_id'] == 'call-6'
    assert overview['slow_items']['steps'][0]['trace_id'] == 'trace-6'
    assert len(overview['slow_items']['requests']) == len(overview['slow_items']['steps']) == 5
    assert not any('trace-' in fact or 'call-' in fact for fact in overview['insights']['facts'])


def test_data_quality_missing_historical_partial_and_malformed_values():
    rows = [generation('missing', usageDetails=None, costDetails='invalid', metadata={}),
            generation('history', usageDetails={'total': 4}, metadata={}, costDetails={'total': 0}),
            generation('verified', usageDetails={'input': 0, 'output': 0, 'total': 0}),
            generation('partial', usageDetails={'input': 2}),
            generation('malformed', usageDetails=['invalid'], metadata={'aiden_usage_source': 'provider',
                                                                      'aiden_provider_usage': 'invalid'})]
    quality = c.data_quality(rows)
    assert quality == {'generations': 5, 'provider_verified': 1, 'usage_unrecorded': 2,
                       'usage_unverified': 1, 'cost_recorded': 1, 'cost_missing': 4}
    assert c.tokens(rows)['total'] == 0
    assert c.costs(rows)[0]['known_cost'] == 0
    assert c.costs(rows)[0]['completeness'] == 'partial'
    assert c.observation_summary(rows[0])['cost_details'] is None


def test_empty_reason_distinguishes_range_source_and_filters():
    root = {'id': 'root', 'traceId': 'trace', 'name': 'aiden_support',
            'metadata': {'aiden_source': 'online'}}
    other = {'id': 'other', 'traceId': 'other', 'name': 'aiden_support',
             'metadata': {'aiden_source': 'evaluation'}}
    for rows, kwargs, reason, source_count, source_excluded, filter_excluded in [
        ([], {}, 'no_observations', 0, 0, 0),
        ([other], {}, 'source_excluded', 0, 1, 0),
        ([root, other], {'model': 'missing'}, 'filters_excluded', 1, 1, 1),
    ]:
        with patch.object(c, 'read_rows', return_value=(rows, False, None)):
            result = c.query(kind='overview', limit=20, **kwargs)
        assert result['state'] == 'empty' and result['availability']['query_verified']
        coverage = result['coverage']
        assert coverage['empty_reason'] == reason
        assert coverage['source_matched_traces'] == source_count
        assert coverage['excluded_by_source'] == source_excluded
        assert coverage['excluded_by_filters'] == filter_excluded
        assert coverage['matched_observations'] == 0


def test_invalid_cursor_contract_rejects_partial_batch():
    class Client:
        meta = None
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, params):
            return httpx.Response(200, json={'data': [generation('a')], 'meta': self.meta},
                                  request=httpx.Request('GET', 'https://example.invalid'))
    with patch.object(c.settings, 'langfuse_enabled', True), patch.object(c.httpx, 'Client', Client):
        for invalid in [['not-a-mapping'], {'cursor': ['not-a-string']}]:
            Client.meta = invalid
            assert c.read_rows(limit=10) == ([], False, 'unsupported')


def test_v2_documented_total_cost_fallback_without_double_counting():
    rows = [generation('fallback', costDetails={'input': 1, 'output': 2}, totalCost=3),
            generation('priority', costDetails={'total': 0}, totalCost=999),
            generation('missing', costDetails={'input': 1, 'output': 2})]
    assert c.resolved_cost(rows[0]) == 3
    assert c.resolved_cost(rows[1]) == 0
    assert c.resolved_cost(rows[2]) is None
    assert c.costs(rows)[0]['known_cost'] == 3
    assert c.costs(rows)[0]['known_generations'] == 2
    assert c.data_quality(rows)['cost_missing'] == 1
    root = {'id': 'root', 'traceId': 'trace', 'name': 'aiden_support',
            'metadata': {'aiden_source': 'online'}}
    with patch.object(c, 'read_rows', return_value=([root, *rows], False, None)):
        data = c.query(kind='overview', limit=20)['data']
    assert data['costs'][0]['known_cost'] == 3
    assert sum(item['costs'][0]['known_cost'] for item in data['models']) == 3
    assert sum(item['costs'][0]['known_cost'] for item in data['attribution']) == 3


def test_documented_v2_total_is_usd_not_relabelled_by_currency_field():
    rows = [generation('v2-usd', totalCost=2, currency='EUR'),
            generation('explicit-eur', costDetails={'total': 3}, currency='EUR'),
            generation('zero-usd', totalCost=0, costCurrency='EUR')]
    values = {item['currency']: item for item in c.costs(rows)}
    assert values['USD']['known_cost'] == 2
    assert values['USD']['known_generations'] == 2
    assert values['EUR']['known_cost'] == 3
    root = {'id': 'root', 'traceId': 'trace', 'name': 'aiden_support',
            'totalCost': 999, 'metadata': {'aiden_source': 'online'}}
    with patch.object(c, 'read_rows', return_value=([root, *rows], False, None)):
        data = c.query(kind='overview', limit=20)['data']
    for dimension in ('models', 'attribution'):
        for total in data['costs']:
            assert sum(cost['known_cost'] for item in data[dimension] for cost in item['costs']
                       if cost['currency'] == total['currency']) == total['known_cost']
    assert data['data_quality']['cost_recorded'] == 3


def test_partial_provider_fields_never_claim_complete_usage_and_truncated_empty_is_unknown():
    row = generation('partial', usageDetails={'input': 0, 'output': 2})
    quality = c.data_quality([row])
    usage = c.tokens([row])
    assert quality['provider_verified'] == usage['known_generations'] == 0
    assert usage['input'] == 0 and usage['output'] == 2 and usage['total'] is None
    assert usage['field_coverage']['total'] == {'known': 0, 'missing': 1}
    assert quality['usage_unrecorded'] == quality['usage_unverified'] == 0
    other = {'id': 'other', 'traceId': 'other', 'name': 'aiden_support',
             'metadata': {'aiden_source': 'evaluation'}}
    with patch.object(c, 'read_rows', return_value=([other], True, None)):
        result = c.query(kind='overview', limit=1)
    assert result['coverage']['empty_reason'] is None
    assert result['coverage']['excluded_by_source'] == 1
    assert result['coverage']['truncated']


def test_slow_steps_exclude_real_graph_and_wrapper_containers_not_nested_steps():
    timing = {'traceId': 'trace', 'startTime': '2026-10-01T00:00:00Z',
              'endTime': '2026-10-01T00:00:10Z'}
    wrapper = {'id': 'wrapper', 'name': 'aiden_support', 'isRootObservation': True, **timing}
    root = {'id': 'graph', 'name': 'aiden_support', 'parentObservationId': 'wrapper',
            'metadata': {'is_langchain_root': True, 'aiden_source': 'online'}, **timing}
    node = {'id': 'node', 'name': 'generate', 'type': 'CHAIN', 'parentObservationId': 'graph', **timing}
    call = generation('call', model='model-a', parentObservationId='node', **timing)
    rows = [wrapper, root, node, call]
    with patch.object(c, 'read_rows', return_value=(rows, False, None)):
        data = c.query(kind='overview', limit=20)['data']
    assert {step['observation_id'] for step in data['slow_items']['steps']} == {'node', 'call'}
    assert {step['type'] for step in data['slow_items']['steps']} == {'CHAIN', 'GENERATION'}
    assert all(step['duration_ms'] == 10000 for step in data['slow_items']['steps'])
    assert data['duration']['mean_ms'] == 10000
    assert len(data['slow_items']['requests']) == 1


def test_rootless_record_group_is_not_confirmed_processing_or_slow_request():
    row = generation('history', parentObservationId='outside-window',
                     startTime='2026-10-01T00:00:00Z', endTime='2026-10-01T00:00:02Z',
                     usageDetails={'total': 5}, metadata={})
    with patch.object(c, 'read_rows', return_value=([row], False, None)):
        overview = c.query(kind='overview', source='other', limit=10)
        listing = c.query(kind='traces', source='other', limit=10)
    assert overview['coverage']['matched_traces'] == listing['data']['total'] == 1
    assert overview['data']['counts']['roots'] == 0
    assert overview['data']['duration']['samples'] == 0
    assert overview['data']['slow_items']['requests'] == []
    assert overview['data']['slow_items']['steps'][0]['observation_id'] == 'history'
    assert listing['data']['items'][0]['duration_ms'] is None
    assert listing['data']['items'][0]['status'] == 'unknown'


def test_usage_insights_require_verified_totals_even_when_all_costs_missing():
    high_model, low_model, unknown_model, zero_model = (
        'verified-high-model', 'verified-low-model', 'unverified-large-model', 'verified-zero-model')
    high = generation('high', 'cost_bucket_intent_classification',
                      model=high_model, usageDetails={'input': 0, 'output': 4, 'total': 4})
    low = generation('low', model=low_model, usageDetails={'input': 0, 'output': 2, 'total': 2})
    history = generation('history', model=unknown_model, usageDetails={'total': 999}, metadata={})
    unknown = c.insights([], [history], False)
    known = c.insights([], [high, low, history], False)
    zero = c.insights([], [generation('zero', model=zero_model,
                                      usageDetails={'input': 0, 'output': 0, 'total': 0})], False)
    assert any(high_model in fact for fact in known['facts'])
    assert not any(low_model in fact or unknown_model in fact for fact in known['facts'])
    assert not any(unknown_model in fact for fact in unknown['facts'])
    assert any(zero_model in fact for fact in zero['facts'])
    assert set(known) == set(zero) == {'facts', 'limitations'}
    assert c.tokens([high, low, history])['total'] == 6
    assert c.data_quality([high, low, history])['cost_missing'] == 3
