"""员工主题分类 API；HTTP 层不接受本地路径、设备或命令。"""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from app.api.deps import current_staff
from app.persistence.mysql import topic_classification as repo
from app.services.topic_classification import console
from app.services.topic_classification.taxonomy import LABEL_IDS
from app.services.rag.runtime_control import AlreadyRunningError
router = APIRouter(prefix='/api/topics', tags=['topics'], dependencies=[Depends(current_staff)])

class Cursor(BaseModel):
    model_config = ConfigDict(extra='forbid')
    created_at: datetime
    source_id: UUID
class BatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_key: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    start_at: datetime | None = None
    end_at: datetime | None = None
    limit: int = Field(default=100, ge=1, le=5000)
    batch_size: int = Field(default=32, ge=1, le=500)
    after: Cursor | None = None
class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_key: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    batch_size: int = Field(default=32, ge=1, le=500)

def utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

def scope(start_at, end_at):
    start_at, end_at = utc(start_at), utc(end_at)
    if start_at and end_at and start_at >= end_at:
        raise HTTPException(422, detail={'code': 'invalid_time_range', 'message': '开始时间须早于结束时间'})
    return start_at, end_at

def db_call(fn, **kwargs):
    try:
        state = repo.readiness()
        if not state['ready']:
            raise HTTPException(503, detail={'code': 'schema_missing', 'message': '主题表或所需列缺失', **state})
        if fn == repo.result_rows and kwargs.get('model_version') is None:
            versions = repo.model_versions()
            if len(versions) > 1:
                raise HTTPException(422, detail={'code': 'model_version_required', 'message': '多模型历史须显式选择版本'})
            if versions:
                kwargs['model_version'] = versions[0]
        return fn(**kwargs)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail={'code': 'database_unavailable', 'message': '数据库未配置或不可用'}) from None

def runtime_call(fn):
    try:
        return fn()
    except console.UnknownModel:
        raise HTTPException(404, detail={'code': 'unknown_model_key', 'message': '执行模型未注册，未回退到其他模型'}) from None
    except Exception:
        raise HTTPException(503, detail=console.safe_error('runtime_unavailable')) from None

@router.get('/availability')
def availability(model_key: str | None = Query(None, min_length=1, max_length=100)):
    return runtime_call(lambda: console.availability(model_key))
@router.get('/jobs')
def jobs():
    return {'items': runtime_call(console.jobs)}
@router.get('/jobs/{job_id}')
def job_detail(job_id: UUID):
    job = next((j for j in runtime_call(console.jobs) if j['id'] == str(job_id)), None)
    if job is None:
        raise HTTPException(404, detail={'code': 'job_not_found'})
    return job

def launch(kind, params, model_key):
    try:
        return console.start(kind, params, model_key=model_key)
    except console.UnknownModel:
        raise HTTPException(404, detail={'code': 'unknown_model_key', 'message': '执行模型未注册，未回退到其他模型'}) from None
    except AlreadyRunningError:
        raise HTTPException(409, detail={'code': 'job_already_running', 'message': '已有主题任务运行'}) from None
    except console.PrerequisitesBlocked as exc:
        raise HTTPException(503, detail={'code': 'prerequisites_blocked', 'checks': exc.checks}) from None
    except Exception:
        raise HTTPException(503, detail=console.safe_error('runtime_unavailable')) from None
@router.post('/jobs/batch', status_code=202)
def start_batch(body: BatchRequest):
    start, end = scope(body.start_at, body.end_at)
    return launch('batch', {'start_at': start.isoformat() if start else None, 'end_at': end.isoformat() if end else None,
                           'limit': body.limit, 'batch_size': body.batch_size,
                           'after': {'created_at': utc(body.after.created_at).isoformat(), 'source_id': str(body.after.source_id)} if body.after else None}, body.model_key)
@router.post('/jobs/evaluation', status_code=202)
def start_evaluation(body: EvaluationRequest):
    return launch('evaluation', {'batch_size': body.batch_size}, body.model_key)

def label_prediction_sources(rows):
    versions = {prediction['model_version'] for row in rows for field in ('current_prediction', 'latest_prediction')
                if (prediction := row.get(field)) is not None}
    origins = {version: console.version_source(version) for version in versions}
    for row in rows:
        for field in ('current_prediction', 'latest_prediction'):
            if row.get(field) is not None:
                row[field].update(origins[row[field]['model_version']])
    return rows

@router.get('/results')
def results(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
            model_version: str | None = Query(None, max_length=255), start_at: datetime | None = None,
            end_at: datetime | None = None, topic: str | None = None,
            status: Literal['predicted', 'uncertain', 'unclassified'] | None = None,
            current_prediction: bool | None = None, has_human: bool | None = None):
    start, end = scope(start_at, end_at)
    if topic is not None and topic not in LABEL_IDS:
        raise HTTPException(422, detail={'code': 'invalid_topic'})
    rows = db_call(repo.result_rows, model_version=model_version, start_at=start, end_at=end,
                   topic=topic, status=status, current_prediction=current_prediction, has_human=has_human)
    return {'items': label_prediction_sources(rows[offset:offset+limit]), 'total': len(rows), 'limit': limit, 'offset': offset}
@router.get('/results/{source_id}')
def result_detail(source_id: UUID, model_version: str | None = Query(None, max_length=255)):
    rows = db_call(repo.result_rows, source_id=str(source_id), model_version=model_version)
    if not rows:
        raise HTTPException(404, detail={'code': 'source_not_found'})
    return label_prediction_sources(rows)[0]
@router.get('/stats')
def stats(model_version: str = Query(min_length=1, max_length=255), start_at: datetime | None = None, end_at: datetime | None = None):
    start, end = scope(start_at, end_at)
    result = db_call(repo.statistics, model_version=model_version, start_at=start, end_at=end)
    return {**result, **console.version_source(model_version)}
@router.get('/reports')
def reports(model_version: str | None = Query(None, min_length=1, max_length=255)):
    return {'items': runtime_call(lambda: console.reports(model_version))}
@router.get('/reports/{report_id}')
def report_detail(report_id: UUID):
    try:
        return console.report_detail(str(report_id))
    except LookupError:
        raise HTTPException(404, detail={'code': 'report_not_found'}) from None
    except Exception:
        raise HTTPException(503, detail={'code': 'report_evidence_unverified', 'message': '历史报告数值或隐私证据无法核验'}) from None
