"""员工只读接口；上游失败转换为不含敏感内容的可用性响应。"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import current_staff
from app.config.settings import settings
from app.services.observability import console

router = APIRouter(prefix='/api/observability', tags=['observability'], dependencies=[Depends(current_staff)])


def filters(start_at: datetime | None = None, end_at: datetime | None = None,
            source: Literal['aiden_support', 'other'] = 'aiden_support',
            model: str | None = Query(default=None, max_length=200),
            intent: Literal['logistics', 'order', 'product', 'refund_return', 'after_sales', 'complaint', 'smalltalk', 'human', 'other'] | None = None,
            status: Literal['success', 'error', 'unknown'] | None = None,
            conversation_id: str | None = Query(default=None, max_length=100),
            message_id: str | None = Query(default=None, max_length=100),
            max_observations: int = Query(default=5000, ge=1, le=10000)) -> dict:
    if any(value is not None and value.tzinfo is None for value in [start_at, end_at]):
        raise HTTPException(status_code=422, detail='时间必须包含时区')
    end = (end_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = (start_at or end - timedelta(hours=24)).astimezone(timezone.utc)
    if start >= end or end - start > timedelta(days=31):
        raise HTTPException(status_code=422, detail='时间范围必须为正且不超过31天')
    return dict(start=start, end=end, source=source, model=model, intent=intent,
                root_status=status, conversation_id=conversation_id, message_id=message_id, limit=max_observations)


@router.get('/status')
def status() -> dict:
    return console.envelope('configured_unverified' if settings.langfuse_enabled else 'unconfigured', data={'limits': {'default_hours': 24, 'max_days': 31, 'default_observations': 5000, 'max_observations': 10000}, 'read_api': 'observations_v2', 'privacy': 'no inputs/outputs/prompts/raw metadata', 'async_incomplete': True})


@router.get('/overview')
def overview(params: dict = Depends(filters)) -> dict:
    return console.query(kind='overview', **params)


@router.get('/traces')
def traces(params: dict = Depends(filters), page: int = Query(default=1, ge=1),
           page_size: int = Query(default=20, ge=1, le=100)) -> dict:
    return console.query(kind='traces', page=page, page_size=page_size, **params)


@router.get('/traces/{trace_id}')
def detail(trace_id: str, params: dict = Depends(filters)) -> dict:
    if len(trace_id) > 100 or not all(c.isalnum() or c in '-_' for c in trace_id):
        raise HTTPException(status_code=422, detail='无效 trace ID')
    return console.query(kind='detail', trace_id=trace_id, source='all',
                         limit=params['limit'], start=params['start'], end=params['end'])
