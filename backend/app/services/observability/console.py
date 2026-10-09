"""员工只读 Langfuse v2 适配；不返回输入、输出或任意 metadata。"""
from __future__ import annotations

import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config.settings import settings

INTENTS = frozenset({'logistics', 'order', 'product', 'refund_return', 'after_sales', 'complaint', 'smalltalk', 'human', 'other'})
SAFE_NAMES = frozenset({'aiden_support', 'load_context', 'recognize_intent', 'route_intent', 'dispatch_tool_call', 'generate', 'assess_knowledge', 'after_tools', 'finish_request', 'save_answer', 'query_order', 'query_logistics', 'search_faq', 'query_refund_policy', 'query_after_sale', 'query_ticket', 'create_ticket', 'cost_bucket_intent_classification'}) | {f'cost_intent_{i}' for i in INTENTS}
SAFE_NAMES |= {'submit_after_sale'} | {f'run_{name}' for name in ('query_order', 'query_logistics', 'search_faq', 'query_refund_policy', 'query_after_sale', 'query_ticket', 'create_ticket', 'submit_after_sale')}
FIELDS = 'basic,usage,model,metadata,trace_context'
logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: Any) -> str | None:
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def metadata(row: dict) -> dict:
    value = row.get('metadata')
    return value if isinstance(value, dict) else {}


def bucket(row: dict) -> str:
    meta = metadata(row)
    name = row.get('name', '')
    if name == 'cost_bucket_intent_classification' or meta.get('cost_bucket') == 'intent_classification':
        return 'intent_classification'
    intent = name.removeprefix('cost_intent_') if isinstance(name, str) and name.startswith('cost_intent_') else meta.get('cost_intent')
    return intent if intent in INTENTS else 'unattributed'


def generations(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get('type') == 'GENERATION']


def tokens(rows: list[dict]) -> dict:
    rows = generations(rows)
    def provider(row: dict, key: str) -> float | None:
        recorded = metadata(row).get('aiden_provider_usage')
        return number(recorded.get(key)) if metadata(row).get('aiden_usage_source') == 'provider' and isinstance(recorded, dict) else None
    values = {key: [provider(r, key) for r in rows] for key in ['input', 'output', 'total']}
    return {
        **{key: sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None for key, vals in values.items()},
        'known_generations': sum(all(provider(r, k) is not None for k in ['input', 'output', 'total']) for r in rows),
        'total_generations': len(rows),
        'field_coverage': {key: {'known': sum(v is not None for v in vals), 'missing': sum(v is None for v in vals)} for key, vals in values.items()},
        'source': '仅汇总供应商响应确认的原始用量；历史来源未核验的记录不计入，不使用估算用量。',
    }


def resolved_cost(row: dict) -> float | None:
    """v2 usage 字段组：优先明细 total，否则采用同 observation 的文档总额。"""
    details = row.get('costDetails') if isinstance(row.get('costDetails'), dict) else {}
    value = number(details.get('total'))
    return value if value is not None else number(row.get('totalCost'))


def cost_currency(row: dict) -> str:
    details = row.get('costDetails') if isinstance(row.get('costDetails'), dict) else {}
    if number(details.get('total')) is None and number(row.get('totalCost')) is not None:
        return 'USD'  # v2 totalCost 明确为 USD，不按无关币种字段换标签或换汇。
    currency = row.get('currency') or row.get('costCurrency') or 'USD'
    return currency if isinstance(currency, str) and 0 < len(currency) <= 12 else 'USD'


def costs(rows: list[dict]) -> list[dict]:
    rows = generations(rows)
    groups: dict[str, list[float]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        value = resolved_cost(row)
        # v2 costDetails 和 totalCost 均明确为 USD；只计唯一 generation，不叠加分项。
        currency = cost_currency(row)
        groups.setdefault(currency, [])
        totals[currency] = totals.get(currency, 0) + 1
        if value is not None:
            groups[currency].append(value)
    return [{
        'currency': currency,
        'known_cost': sum(values) if values else None,
        'known_generations': len(values),
        'total_generations': totals[currency],
        'completeness': 'complete' if len(values) == totals[currency] else 'partial' if values else 'unknown',
        'source': '观测平台记录的金额：优先采用上报金额，否则按模型价格计算；当前接口不区分两者，不是供应商账单。',
    } for currency, values in sorted(groups.items())]


def duration(row: dict) -> float | None:
    start, end = timestamp(row.get('startTime')), timestamp(row.get('endTime'))
    if start and end:
        value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
        return value if value >= 0 else None
    return None


def durations(rows: list[dict]) -> dict:
    vals = sorted(v for r in rows if (v := duration(r)) is not None)
    def percentile(p: float) -> float | None:
        if not vals:
            return None
        pos = (len(vals) - 1) * p
        lo = int(pos)
        return vals[lo] + (vals[min(lo + 1, len(vals) - 1)] - vals[lo]) * (pos - lo)
    return {'samples': len(vals), 'mean_ms': sum(vals) / len(vals) if vals else None, 'p50_ms': percentile(.5), 'p95_ms': percentile(.95)}


def status(row: dict) -> str:
    if row.get('level') == 'ERROR':
        return 'error'
    return 'success' if metadata(row).get('aiden_completion') == 'success' and timestamp(row.get('endTime')) else 'unknown'


def root_rows(rows: list[dict]) -> list[dict]:
    """优先真实 LangChain 图根，排除上游同名 trace 包装根的重复统计。"""
    graph_roots = [r for r in rows if r.get('name') == 'aiden_support'
                   and (metadata(r).get('is_langchain_root') is True or r.get('isRootObservation') is True or not r.get('parentObservationId'))]
    wrappers = {r.get('parentObservationId') for r in graph_roots if metadata(r).get('is_langchain_root') is True}
    if graph_roots:
        return [r for r in graph_roots if r['id'] not in wrappers]
    return [r for r in rows if r.get('isRootObservation') is True]


def is_online(rows: list[dict]) -> bool:
    roots = root_rows(rows)
    return bool(roots) and any(metadata(r).get('aiden_source') == 'online' for r in roots) and not any(metadata(r).get('aiden_source') == 'evaluation' or str(metadata(r).get('conversation_id', '')).startswith(('eval-', 'eval_')) or str(r.get('sessionId') or '').startswith('eval') or str(r.get('userId') or '').startswith('eval') or r.get('environment') in {'evaluation', 'eval'} for r in rows)


def intents(rows: list[dict]) -> list[str]:
    result: set[str] = set()
    for row in root_rows(rows):
        for item in metadata(row).get('recognized_intents', []) if isinstance(metadata(row).get('recognized_intents'), list) else []:
            if isinstance(item, dict) and item.get('intent') in INTENTS:
                result.add(item['intent'])
    return sorted(result)


def association(rows: list[dict], key: str) -> str | None:
    for row in root_rows(rows) + rows:
        value = metadata(row).get(key)
        if isinstance(value, str) and 0 < len(value) <= 100 and all(c.isalnum() or c in '-_' for c in value):
            return value
    return None


def trace_summary(trace_id: str, rows: list[dict]) -> dict:
    roots = root_rows(rows)
    root = min(roots, key=lambda r: timestamp(r.get('startTime')) or '') if roots else {}
    return {'id': trace_id, 'start_at': timestamp(root.get('startTime')) or min((timestamp(r.get('startTime')) for r in rows if timestamp(r.get('startTime'))), default=None), 'name': root.get('name') if root.get('name') in SAFE_NAMES else '未记录根节点', 'source': 'aiden_support' if is_online(rows) else 'other', 'source_detail': 'online' if is_online(rows) else 'evaluation' if any(metadata(r).get('aiden_source') == 'evaluation' for r in rows) else 'unrecorded', 'intents': intents(rows), 'intents_recorded': any(isinstance(metadata(r).get('recognized_intents'), list) for r in roots), 'models': sorted({str(r['model'])[:200] for r in generations(rows) if r.get('model')}), 'status': status(root), 'duration_ms': duration(root), 'tokens': tokens(rows), 'costs': costs(rows), 'conversation_id': association(rows, 'conversation_id'), 'message_id': association(rows, 'assistant_message_id'), 'external_url': None}


def envelope(state: str, *, rows: list[dict] | None = None, limit: int = 5000, truncated: bool = False, start: datetime | None = None, end: datetime | None = None, source: str = 'aiden_support', data: Any = None, matched: int = 0, matched_rows: list[dict] | None = None, source_matched: int = 0, excluded_by_source: int = 0) -> dict:
    rows = rows or []
    visible = matched_rows if matched_rows is not None else []
    messages = {'unconfigured': '观测服务未配置', 'configured_unverified': '已配置，尚未验证能否读取',
                'ready': '读取成功；近期记录可能仍在上报，尚未全部可见', 'empty': '读取成功，当前筛选没有匹配记录',
                'auth_failed': '观测服务认证或权限不足', 'connection_failed': '观测服务连接或读取失败',
                'unsupported': '观测服务不支持当前读取方式'}
    if state == 'empty':
        messages['empty'] = ('已读取的部分记录没有匹配项，尚不能判断整个范围。' if truncated else
                             '读取成功，整个时间范围没有观测记录。' if not rows else
                             '读取成功，但已读记录不属于所选来源。' if not source_matched else
                             '读取成功，但来源匹配记录被其他筛选条件排除。')
    return {
        'state': state, 'message': messages.get(state, state), 'queried_at': now(),
        'latest_record_at': max((timestamp(r.get('startTime')) for r in visible if timestamp(r.get('startTime'))), default=None),
        'coverage': {'observations_read': len(rows),
                     'generations_read': len(generations(rows)), 'matched_generations': len(generations(visible)),
                     'matched_traces': matched, 'truncated': truncated, 'limit': limit, 'scope': source,
                     'matched_observations': len(visible), 'source_matched_traces': source_matched,
                     'excluded_by_source': excluded_by_source, 'excluded_by_filters': source_matched - matched,
                     'empty_reason': ('no_observations' if not rows else 'source_excluded' if not source_matched else 'filters_excluded') if state == 'empty' and not truncated else None,
                     'start_at': start.isoformat() if start else None, 'end_at': end.isoformat() if end else None},
        'availability': {'configured': settings.langfuse_enabled, 'query_verified': state in {'ready', 'empty'},
                         'upstream': state, 'async_incomplete': True},
        'data': data,
    }


def read_rows(*, limit: int, start: datetime | None = None, end: datetime | None = None, trace_id: str | None = None) -> tuple[list[dict], bool, str | None]:
    """只读目标 v2，完整游标分页去重；失败不返回半批次或敏感异常。"""
    if not settings.langfuse_enabled:
        return [], False, 'unconfigured'
    params: dict[str, Any] = {'limit': min(limit + 1, 1000), 'fields': FIELDS, 'expandMetadata': 'recognized_intents,conversation_id,assistant_message_id,aiden_source,aiden_completion,aiden_usage_source,aiden_provider_usage'}
    if start:
        params['fromStartTime'] = start.isoformat()
    if end:
        params['toStartTime'] = end.isoformat()
    if trace_id:
        params['traceId'] = trace_id
    result: dict[str, dict] = {}
    cursors: set[str] = set()
    try:
        with httpx.Client(auth=(os.getenv('LANGFUSE_PUBLIC_KEY', ''), os.getenv('LANGFUSE_SECRET_KEY', '')), timeout=30) as client:
            while True:
                response = client.get(os.getenv('LANGFUSE_BASE_URL', '').rstrip('/') + '/api/public/v2/observations', params=params)
                if response.status_code in {401, 403}:
                    return [], False, 'auth_failed'
                if response.status_code in {404, 405, 410, 501}:
                    return [], False, 'unsupported'
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    return [], False, 'unsupported'
                for row in payload['data']:
                    if isinstance(row, dict) and isinstance(row.get('id'), str):
                        result[row['id']] = row
                        if len(result) > limit:
                            return list(result.values())[:limit], True, None
                meta = payload.get('meta')
                if meta is not None and not isinstance(meta, dict):
                    return [], False, 'unsupported'
                cursor = (meta or {}).get('cursor')
                if cursor is not None and not isinstance(cursor, str):
                    return [], False, 'unsupported'
                if not cursor:
                    return list(result.values()), False, None
                if cursor in cursors:
                    return list(result.values())[:limit], True, None
                cursors.add(cursor)
                params['cursor'] = cursor
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # 仅记录固定分类与状态码；异常文本可能包含 URL、凭据或上游内容。
        error_type = next((name for cls, name in (
            (httpx.HTTPStatusError, 'HTTPStatusError'),
            (httpx.TimeoutException, 'TimeoutException'),
            (httpx.ConnectError, 'ConnectError'),
            (httpx.HTTPError, 'HTTPError'),
            (ValueError, 'ValueError'),
            (TypeError, 'TypeError'),
        ) if isinstance(exc, cls)), 'HTTPError')
        http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning('Langfuse observation read failed: error_type=%s http_status=%s', error_type, http_status)
        return [], False, 'connection_failed'


def observation_summary(row: dict) -> dict:
    """仅投影允许展示的结构字段；记录 usage 细项不冒充 provider 来源。"""
    detail_keys = {'input', 'output', 'total', 'input_cached', 'input_cache_read',
                   'input_cache_creation', 'output_reasoning'}
    def details(field: str) -> dict | None:
        raw = row.get(field)
        return {k: v for k, v in raw.items() if k in detail_keys and number(v) is not None} or None if isinstance(raw, dict) else None
    return {
        'id': row['id'], 'parent_id': row.get('parentObservationId'),
        'name': row.get('name') if row.get('name') in SAFE_NAMES else 'observation',
        'type': row.get('type') if row.get('type') in {'SPAN', 'GENERATION', 'EVENT', 'AGENT', 'TOOL', 'CHAIN', 'RETRIEVER', 'EMBEDDING', 'EVALUATOR', 'GUARDRAIL'} else '未记录',
        'start_at': timestamp(row.get('startTime')), 'end_at': timestamp(row.get('endTime')),
        'status': status(row), 'duration_ms': duration(row),
        'model': str(row['model'])[:200] if row.get('model') else None,
        'tokens': tokens([row]), 'costs': costs([row]),
        'usage_details': details('usageDetails'), 'cost_details': details('costDetails'),
        'usage_details_source': '观测平台记录分项，历史来源未核验' if metadata(row).get('aiden_usage_source') != 'provider' or not isinstance(metadata(row).get('aiden_provider_usage'), dict) else '观测平台记录分项；供应商原始用量及缺失情况见用量汇总',
        'error_summary': 'upstream_observation_error' if status(row) == 'error' else None,
    }


def data_quality(rows: list[dict]) -> dict:
    calls = generations(rows)
    verified = tokens(calls)['known_generations']
    def recorded(row: dict) -> bool:
        for raw in (row.get('usageDetails'), metadata(row).get('aiden_provider_usage')):
            if isinstance(raw, dict) and any(number(raw.get(key)) is not None for key in ('input', 'output', 'total')):
                return True
        return False
    unrecorded = sum(not recorded(r) for r in calls)
    priced = sum(resolved_cost(r) is not None for r in calls)
    return {'generations': len(calls), 'provider_verified': verified,
            'usage_unrecorded': unrecorded, 'usage_unverified': sum(
                metadata(r).get('aiden_usage_source') != 'provider' and recorded(r)
                for r in calls), 'cost_recorded': priced, 'cost_missing': len(calls) - priced}


def insights(selected: list[tuple[dict, list[dict]]], rows: list[dict], truncated: bool) -> dict:
    quality = data_quality(rows)
    processing_count = sum(len(root_rows(group)) for _, group in selected)
    facts = [f'当前筛选匹配 {len(selected)} 组请求记录，可确认 {processing_count} 次处理，包含 {quality["generations"]} 次模型调用。',
             f'{quality["provider_verified"]} 次调用的完整用量已由供应商响应确认；{quality["usage_unrecorded"]} 次用量未记录，{quality["usage_unverified"]} 次历史用量来源未核验。']
    incomplete = quality['generations'] - quality['provider_verified'] - quality['usage_unrecorded'] - quality['usage_unverified']
    if incomplete:
        facts.append(f'另有 {incomplete} 次调用只记录了部分用量，不能据此补算完整用量。')
    purpose_names = {'intent_classification': '共享问题分类', 'logistics': '物流查询',
                     'order': '订单查询', 'product': '商品咨询', 'refund_return': '退款退货',
                     'after_sales': '售后服务', 'complaint': '投诉处理', 'smalltalk': '日常交流',
                     'human': '转人工', 'other': '其他问题', 'unattributed': '用途未记录'}
    usage_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in generations(rows):
        if tokens([row])['total'] is not None:
            usage_groups[('业务用途', purpose_names[bucket(row)])].append(row)
            usage_groups[('模型', str(row.get('model') or '未记录模型')[:200])].append(row)
    for dimension in ('业务用途', '模型'):
        candidates = [(label, group, tokens(group)['total']) for (kind, label), group in usage_groups.items() if kind == dimension]
        if candidates:
            label, group, total = min(candidates, key=lambda item: (-item[2], item[0]))
            facts.append(f'在已确认总用量的调用中，{dimension}“{label}”用量最多：{len(group)} 次调用、{total:,.0f} 个 Token。')
    limitations = ['用量按模型和业务用途统计；共享意图识别用量单独列出，不摊给业务用途。',
                   '耗时列表只表示本批次中耗时较长的请求和步骤，不代表异常或模型质量；步骤可能包含子步骤，并行或嵌套步骤耗时不能相加为请求耗时。']
    if truncated:
        limitations.insert(0, '读取已达到上限或分页未完成；统计和排序仅代表已读取的记录，不是全量结论。')
    return {'facts': facts, 'limitations': limitations}


def slow_items(selected: list[tuple[dict, list[dict]]], rows: list[dict]) -> dict:
    requests = [summary for summary, _ in selected if summary['duration_ms'] is not None]
    requests.sort(key=lambda item: (-item['duration_ms'], item['id']))
    roots = [root for _, group in selected for root in root_rows(group)]
    container_ids = {root['id'] for root in roots} | {root['parentObservationId'] for root in roots if root.get('parentObservationId')}
    container_ids.update(row['id'] for row in rows if row.get('isRootObservation') is True)
    steps = [row for row in rows if duration(row) is not None and row.get('id') not in container_ids]
    steps.sort(key=lambda row: (-duration(row), row['id']))
    return {'requests': [{'trace_id': item['id'], 'start_at': item['start_at'],
                          'duration_ms': item['duration_ms'], 'intents': item['intents'],
                          'models': item['models']} for item in requests[:5]],
            'steps': [{'trace_id': row['traceId'], 'observation_id': row['id'],
                       'name': row.get('name') if row.get('name') in SAFE_NAMES else 'observation',
                       'type': row.get('type') if row.get('type') in {'SPAN', 'GENERATION', 'EVENT', 'AGENT', 'TOOL', 'CHAIN', 'RETRIEVER', 'EMBEDDING', 'EVALUATOR', 'GUARDRAIL'} else '未记录',
                       'duration_ms': duration(row), 'model': str(row['model'])[:200] if row.get('model') else None}
                      for row in steps[:5]]}


def query(*, kind: str, limit: int, start: datetime | None = None, end: datetime | None = None, source: str = 'aiden_support', model: str | None = None, intent: str | None = None, root_status: str | None = None, conversation_id: str | None = None, message_id: str | None = None, trace_id: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """先选整条真实请求再统计；保留共享分类与真实父子结构。"""
    rows, truncated, error = read_rows(limit=limit, start=start, end=end, trace_id=trace_id)
    args = dict(rows=rows, limit=limit, truncated=truncated, start=start, end=end, source=source)
    if error:
        return envelope(error, **args)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if isinstance(row.get('traceId'), str):
            grouped[row['traceId']].append(row)
    selected: list[tuple[dict, list[dict]]] = []
    source_matched = 0
    for tid, observations in grouped.items():
        summary = trace_summary(tid, observations)
        if source != 'all' and summary['source'] != source:
            continue
        source_matched += 1
        if model and model not in summary['models'] or intent and intent not in (set(summary['intents']) | {bucket(r) for r in generations(observations)}) or root_status and root_status != summary['status']:
            continue
        if conversation_id and conversation_id != summary['conversation_id'] or message_id and message_id != summary['message_id']:
            continue
        selected.append((summary, observations))
    selected.sort(key=lambda pair: pair[0]['start_at'] or '', reverse=True)
    if kind == 'traces':
        data = {'items': [t for t, _ in selected][(page - 1) * page_size:page * page_size], 'total': len(selected), 'page': page, 'page_size': page_size}
    elif kind == 'detail':
        if not selected:
            data = None
        else:
            summary, obs = selected[0]
            ids = {r['id'] for r in obs}
            orphaned = sum(bool(r.get('parentObservationId')) and r['parentObservationId'] not in ids for r in obs)
            data = {
                'trace': summary, 'observations': [observation_summary(r) for r in obs],
                'structure': {'observations': len(obs), 'generations': len(generations(obs)),
                              'roots': len(root_rows(obs)), 'orphaned': orphaned,
                              'missing_parents': orphaned, 'cross_boundary': orphaned > 0,
                              'truncated': truncated},
                'notes': ['按记录的父子关联展示；缺失的上级步骤可能位于读取范围之外，不能确认其身份。',
                          '历史记录可能未关联站内会话或消息，不能据此推断会话数量。',
                          '分项用量是观测平台的记录值；只有来源已核验的供应商原始计数才进入用量汇总。',
                          '知识查询只记录整体耗时，未单独记录内部检索阶段。',
                          '上级步骤可能包含下级调用；嵌套或并行步骤耗时不能相加为总耗时。'],
            }
    else:
        observations = [r for _, obs in selected for r in obs]
        roots = [r for _, obs in selected for r in root_rows(obs)]
        by_token_dimension: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in generations(observations):
            start_time = timestamp(row.get('startTime'))
            dimension = (start_time[:10] if start_time else 'unknown',
                         str(row.get('model') or 'unknown')[:200], bucket(row))
            by_token_dimension[dimension].append(row)
        by_day: dict[str, list[dict]] = defaultdict(list)
        for _, obs in selected:
            root = root_rows(obs)
            if root and timestamp(root[0].get('startTime')):
                by_day[timestamp(root[0]['startTime'])[:10]].extend(obs)
        def breakdown(key: str) -> list[dict]:
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in generations(observations):
                groups[bucket(r) if key == 'bucket' else str(r.get('model') or 'unknown')[:200]].append(r)
            return [{key: value, 'generations': len(group), 'tokens': tokens(group), 'costs': costs(group)} for value, group in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))]
        data = {
            'counts': {'roots': len(roots), 'generations': len(generations(observations)),
                       **{s: sum(status(r) == s for r in roots) for s in ['success', 'error', 'unknown']}},
            'duration': durations(roots), 'tokens': tokens(observations), 'costs': costs(observations),
            'trend': [{'date': day, 'roots': len(root_rows(group)), 'generations': len(generations(group)),
                       'duration': durations(root_rows(group)), 'tokens': tokens(group),
                       'known_costs': [{'currency': cost['currency'], 'known_cost': cost['known_cost']}
                                       for cost in costs(group) if cost['known_cost'] is not None]} for day, group in sorted(by_day.items())],
            'token_trend': [{'date': day, 'model': model_name, 'bucket': token_bucket,
                             'generations': len(group), 'tokens': tokens(group)}
                            for (day, model_name, token_bucket), group in sorted(by_token_dimension.items())],
            'attribution': breakdown('bucket'), 'models': breakdown('model'),
            'insights': insights(selected, observations, truncated),
            'slow_items': slow_items(selected, observations),
            'data_quality': data_quality(observations),
        }
    return envelope('ready' if selected else 'empty', **args, data=data, matched=len(selected),
                    matched_rows=[r for _, obs in selected for r in obs],
                    source_matched=source_matched, excluded_by_source=len(grouped) - source_matched)
