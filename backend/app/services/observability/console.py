"""员工只读 Langfuse v2 适配；不返回输入、输出或任意 metadata。"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config.settings import settings

INTENTS = frozenset({'logistics', 'order', 'product', 'refund_return', 'after_sales', 'complaint', 'smalltalk', 'human', 'other'})
SAFE_NAMES = frozenset({'aiden_support', 'load_context', 'recognize_intent', 'route_intent', 'dispatch_tool_call', 'generate', 'assess_knowledge', 'after_tools', 'finish_request', 'save_answer', 'query_order', 'query_logistics', 'search_faq', 'query_refund_policy', 'query_after_sale', 'query_ticket', 'create_ticket', 'cost_bucket_intent_classification'}) | {f'cost_intent_{i}' for i in INTENTS}
FIELDS = 'basic,usage,model,metadata,trace_context'


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
        'source': '仅计入已确认 provider 响应原始计数；历史来源未核验即 unknown，不采用上游 tokenizer 推算',
    }


def costs(rows: list[dict]) -> list[dict]:
    rows = generations(rows)
    groups: dict[str, list[float]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        details = row.get('costDetails') or {}
        value = number(details.get('total'))
        # 上游 Observation.costDetails 合同明确 USD；不使用来源不明的 totalCost。
        currency = row.get('currency') or row.get('costCurrency') or 'USD'
        if not isinstance(currency, str) or len(currency) > 12:
            currency = 'USD'
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
        'source': 'Langfuse resolved costDetails.total（上报优先，否则模型价格计算；读取接口不提供逐行来源标记），非供应商账单',
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


def envelope(state: str, *, rows: list[dict] | None = None, limit: int = 5000, truncated: bool = False, start: datetime | None = None, end: datetime | None = None, source: str = 'aiden_support', data: Any = None, matched: int = 0, matched_rows: list[dict] | None = None) -> dict:
    rows = rows or []
    visible = matched_rows if matched_rows is not None else []
    messages = {'unconfigured': 'Langfuse 未配置', 'configured_unverified': '已配置，尚未执行读取验证',
                'ready': '真实观测读取成功；异步上报可能尚未完整', 'empty': '读取成功，当前范围没有匹配观测',
                'auth_failed': '上游认证或权限失败', 'connection_failed': '上游连接或读取失败',
                'unsupported': '上游不支持此观测读取接口'}
    return {
        'state': state, 'message': messages.get(state, state), 'queried_at': now(),
        'latest_record_at': max((timestamp(r.get('startTime')) for r in visible if timestamp(r.get('startTime'))), default=None),
        'coverage': {'observations_read': len(rows),
                     'generations_read': len(generations(rows)), 'matched_generations': len(generations(visible)),
                     'matched_traces': matched, 'truncated': truncated, 'limit': limit, 'scope': source,
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
                cursor = (payload.get('meta') or {}).get('cursor')
                if not cursor:
                    return list(result.values()), False, None
                if cursor in cursors:
                    return list(result.values())[:limit], True, None
                cursors.add(cursor)
                params['cursor'] = cursor
    except (httpx.HTTPError, ValueError, TypeError):
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
        'usage_details_source': 'Langfuse记录值，provider来源未核验' if metadata(row).get('aiden_usage_source') != 'provider' else 'provider响应原始计数与上游usage分项',
        'error_summary': 'upstream_observation_error' if status(row) == 'error' else None,
    }


def analysis(selected: list[tuple[dict, list[dict]]], rows: list[dict], truncated: bool) -> list[str]:
    notes = ['按模型/意图筛选整条请求并保留全部 generation，共享分类不分摊；金额仅 Langfuse 聊天模型计价而非账单，不含工具/DB/GPU/embedding/rerank。']
    coverage = tokens(rows)
    notes.append('Usage 覆盖：' + '；'.join(f'{key} 已知 {value["known"]} / 缺失 {value["missing"]}' for key, value in coverage['field_coverage'].items()))
    priced = sum(number((r.get('costDetails') or {}).get('total')) is not None for r in generations(rows))
    notes.append(f'费用已知 {priced}/{len(generations(rows))}；缺失价格或费用不视为零。旧读取记录不能区分 provider usage 与上游推算，亦不能区分逐行上报费用与模型计价。')
    for dimension in ['model', 'intent']:
        ranked: dict[tuple[str, str], tuple[float, set[str]]] = {}
        for row in generations(rows):
            value = number((row.get('costDetails') or {}).get('total'))
            if value is None:
                continue
            currency = str(row.get('currency') or row.get('costCurrency') or 'USD')
            label = str(row.get('model') or 'unknown')[:200] if dimension == 'model' else bucket(row)
            key = (currency, label)
            subtotal, ids = ranked.get(key, (0.0, set()))
            ranked[key] = (subtotal + value, ids | {row['id']})
        per_currency: dict[str, int] = defaultdict(int)
        for (currency, label), (value, ids) in sorted(ranked.items(), key=lambda item: (item[0][0], -item[1][0], item[0][1])):
            if per_currency[currency] < 10:
                notes.append(f'已知费用贡献 {dimension}={label} {currency} {value:.8g}；generation IDs={",".join(sorted(ids)[:5])}；仅已知小计，不是完整费用排名。')
                per_currency[currency] += 1
    for summary, _ in sorted(selected, key=lambda pair: (-(pair[0]['duration_ms'] or 0), pair[0]['id']))[:5]:
        if summary['duration_ms'] is not None:
            notes.append(f'根耗时排名 trace={summary["id"]} {summary["duration_ms"]:.3f}ms（图运行状态，不是业务成功率）。')
    for row in sorted((r for r in rows if duration(r) is not None), key=lambda r: (-duration(r), r['id']))[:5]:
        notes.append(f'观测耗时排名 observation={row["id"]} {duration(row):.3f}ms；并发节点耗时不得相加为根耗时。')
    notes.append('比较依据不足：' + ('当前批次截断；' if truncated else '') + '没有同工作负载控制实验，不据成本/延迟排名推断模型质量、异常阈值或优化因果。')
    return notes


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
    for tid, observations in grouped.items():
        summary = trace_summary(tid, observations)
        if source != 'all' and summary['source'] != source:
            continue
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
                'notes': ['真实 parent_id 树；缺失父节点可能在读取边界之外，不能确认其身份。',
                          '旧记录可能没有内部会话/消息关联；根 trace metadata 不一定由 v2 observation 返回。',
                          'usage_details 是 Langfuse 记录值，来源未核验不计入已确认 provider 汇总。',
                          'search_faq 仅整体耗时；未采集内部检索阶段。'],
            }
    else:
        observations = [r for _, obs in selected for r in obs]
        roots = [r for _, obs in selected for r in root_rows(obs)]
        by_cost_dimension: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in generations(observations):
            start_time = timestamp(row.get('startTime'))
            dimension = (start_time[:10] if start_time else 'unknown',
                         str(row.get('model') or 'unknown')[:200], bucket(row))
            by_cost_dimension[dimension].append(row)
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
            'cost_trend': [{'date': day, 'model': model_name, 'bucket': cost_bucket,
                            'generations': len(group), 'tokens': tokens(group), 'costs': costs(group)}
                           for (day, model_name, cost_bucket), group in sorted(by_cost_dimension.items())],
            'attribution': breakdown('bucket'), 'models': breakdown('model'),
            'analysis': analysis(selected, observations, truncated),
        }
    return envelope('ready' if selected else 'empty', **args, data=data, matched=len(selected),
                    matched_rows=[r for _, obs in selected for r in obs])
