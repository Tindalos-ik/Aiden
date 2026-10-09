"""员工旁路控制台：受控本地文件、有限任务、真实编码器与冻结数据闸门。"""
from __future__ import annotations
import json
import os
import threading
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, uuid5, UUID, NAMESPACE_URL
from typing import Any
from app.config.settings import BACKEND_DIR
from app.persistence.mysql import topic_classification as repo
from app.services.rag.runtime_control import MiningRuntimeControl, AlreadyRunningError
from . import batch
from .data import validate_dataset, fingerprint, write_new_json, HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT, require_data_mode
from .evaluation import evaluate, validate_frozen_evaluation
from .model import EncoderPredictor, file_hash, validate_metadata
from .taxonomy import load_taxonomy, TAXONOMY_VERSION

_mutex = threading.RLock()
_model_cache = None
_model_cache_lock = threading.RLock()
_validated_dataset_identity = None
_dataset_validation_lock = threading.Lock()
_active_job: dict[str, Any] | None = None

def artifact_signature(directory: Path) -> tuple:
    """运行期间以完整文件清单阻断部署变动；加载时仍核验每份文件的hash。"""
    return tuple(sorted((p.relative_to(directory).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                        for p in directory.rglob('*') if p.is_file()))

def checked_model(directory: Path, device: str, *, data_mode: str = HUMAN_REVIEWED) -> EncoderPredictor:
    """单槽缓存包含模式和目录；先释放旧权重再加载，不累积多模型显存。"""
    global _model_cache
    signature = (str(directory), device, data_mode, artifact_signature(directory))
    with _model_cache_lock:
        if _model_cache is None or _model_cache[0] != signature:
            _model_cache = None
            predictor = EncoderPredictor(directory, device, data_mode=data_mode)
            _model_cache = (signature, predictor)
        return _model_cache[1]

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def config() -> dict[str, Any]:
    """只读取服务端设置；受控运行目录默认 ignored，绝不自动发现模型。"""
    def path(name, default=None):
        value = os.getenv(name, '').strip() or default
        if not value:
            return None
        result = Path(value)
        return result if result.is_absolute() else BACKEND_DIR / result
    runtime = path('TOPIC_CLASSIFIER_RUNTIME_DIR', '.tmp/topic-console')
    return {'registry': path('TOPIC_CLASSIFIER_REGISTRY', 'app/config/topic_models.json'),
            'runtime': runtime, 'reports': path('TOPIC_CLASSIFIER_REPORT_DIR', '.tmp/topic-console/reports'),
            'device': os.getenv('TOPIC_CLASSIFIER_DEVICE', 'cpu').strip()}

def _shared_read_fd(path: str, flags: int) -> int:
    """Windows默认读取句柄不允许替换；共享删除使读者保持旧快照、写者原子发布新状态。"""
    if os.name != 'nt':
        return os.open(path, flags)
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(path, 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise

def _json_reader(path: Path):
    return open(path, encoding='utf-8', opener=_shared_read_fd)

def load(path: Path) -> Any:
    with _json_reader(path) as stream:
        return json.load(stream)

class UnknownModel(LookupError):
    """注册标识不存在；绝不回退到默认模型。"""

def registered_models(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """唯一受控配置入口；相对路径统一相对backend，浏览器无路径入口。"""
    entries = load(cfg['registry'])
    if not isinstance(entries, list):
        raise ValueError('invalid model registry')
    keys = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'model_key', 'display_name', 'artifact_dir', 'dataset', 'data_mode'}:
            raise ValueError('invalid model registry')
        key = entry['model_key']
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key) or key in keys:
            raise ValueError('invalid model registry')
        keys.add(key)
        if entry['data_mode'] not in (HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT):
            raise ValueError('unknown topic data mode')
        if not isinstance(entry['display_name'], str) or not 1 <= len(entry['display_name']) <= 150:
            raise ValueError('invalid model registry')
        for field in ('artifact_dir', 'dataset'):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise ValueError('invalid model registry')
            value = Path(entry[field])
            entry[field] = (value if value.is_absolute() else BACKEND_DIR / value).resolve()
    return entries

def resolve_model(entries: list[dict[str, Any]], model_key: str) -> dict[str, Any]:
    entry = next((value for value in entries if value['model_key'] == model_key), None)
    if entry is None:
        raise UnknownModel('unknown_model_key')
    return entry

def candidate(entry: dict[str, Any]) -> dict[str, Any]:
    """候选只校验metadata，不加载权重；validated不是目录存在或metadata有效的同义词。"""
    public = {k: entry[k] for k in ('model_key', 'display_name', 'data_mode')}
    public.update(model_version=None, evaluation_source=None, training_summary={}, blocked_reason=None, validated=False)
    try:
        metadata = load(entry['artifact_dir'] / 'metadata.json')
        validate_metadata(metadata, data_mode=entry['data_mode'])
        public.update(model_version=metadata['model_version'],
                      evaluation_source=metadata.get('evaluation_source', 'human_confirmed'))
        public['training_summary'] = {k: v for k, v in metadata.get('train_args', {}).items()
                                      if k in ('epochs', 'max_length', 'batch_size', 'learning_rate')
                                      and isinstance(v, (int, float)) and math.isfinite(v)}
    except Exception as exc:
        public['blocked_reason'] = validation_error(exc, 'artifact')
    return public

def checked_dataset(dataset: dict[str, Any], *, data_mode: str = HUMAN_REVIEWED) -> None:
    """只复用同模式、全部JSON内容指纹相同的审核结果；单槽不缓存可变数据对象。"""
    global _validated_dataset_identity
    require_data_mode(dataset, data_mode)
    identity = (data_mode, fingerprint(dataset))
    with _dataset_validation_lock:
        if _validated_dataset_identity != identity:
            validate_dataset(dataset, data_mode=data_mode)
            _validated_dataset_identity = identity

@dataclass(frozen=True)
class AcceptedModel:
    """仅线程持有本地路径；公开任务只保存内容身份，执行不再读取注册配置。"""
    model_key: str
    artifact: Path
    dataset: Path
    device: str
    runtime: Path
    reports: Path
    data_mode: str
    evaluation_source: str
    model_version: str
    artifact_identity: str
    dataset_identity: str
    signature: tuple

    def check(self) -> None:
        if artifact_signature(self.artifact) != self.signature or file_hash(self.artifact / 'metadata.json') != self.artifact_identity:
            raise ValueError('artifact_changed_after_acceptance')
        if file_hash(self.dataset) != self.dataset_identity:
            raise ValueError('dataset_changed_after_acceptance')

def save_job(job: dict[str, Any], runtime: Path | None = None) -> None:
    """原子替换受控任务文件；不得保存原异常、原话、路径或凭据。"""
    directory = (runtime if runtime is not None else config()['runtime']) / 'jobs'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (job['id'] + '.json')
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(job, ensure_ascii=False, default=str), encoding='utf-8')
    if os.name == 'nt' and target.exists():
        # MoveFileEx/os.replace在目标仍有读句柄时会拒绝；ReplaceFile允许共享删除的旧快照继续读。
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        replace = kernel.ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        replace.restype = wintypes.BOOL
        if not replace(str(target), str(temp), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        temp.replace(target)

def safe_error(code: str = 'operation_failed') -> dict[str, str]:
    return {'code': code, 'message': '操作未完成；请由管理员检查本地依赖和配置（不返回原始异常或凭据）。'}

def validation_error(exc: Exception, domain: str) -> dict[str, str]:
    """仅匹配源码定义的固定错误片段；未知异常永不透传文本。"""
    if isinstance(exc, FileNotFoundError):
        return {'code': domain + '_missing', 'message': '配置的本地模型或数据文件缺失'}
    if isinstance(exc, ImportError):
        return {'code': 'model_dependency_missing', 'message': '缺少本地 torch/transformers 或其运行依赖'}
    text = str(exc) if isinstance(exc, ValueError) else ''
    cases = (
        ('artifact changed', 'artifact_changed', '接受任务后模型版本改变，未继续读取或预测'),
        ('artifact_changed_after_acceptance', 'artifact_changed', '接受任务后模型版本改变，未继续预测'),
        ('dataset_changed_after_acceptance', 'dataset_changed', '接受任务后冻结数据内容改变，未继续执行'),
        ('explicit matching authorization', 'data_mode_mismatch', '注册模式与模型或数据模式不匹配，需要明确匹配授权'),
        ('evaluation source', 'evaluation_source_mismatch', '参考标签来源与声明的数据模式不匹配'),
        ('taxonomy/order/input', 'artifact_schema_mismatch', '模型taxonomy、标签顺序或输入版本不匹配'),
        ('taxonomy-compatible', 'artifact_not_trained', '缺训练标记或taxonomy指纹不兼容'),
        ('threshold vector', 'artifact_threshold_invalid', '模型阈值向量无效'),
        ('validation calibration/data lineage', 'artifact_lineage_missing', '缺验证集阈值校准或训练切分来源'),
        ('checksum mismatch or missing weights', 'artifact_checksum_invalid', '模型文件hash不一致或缺权重'),
        ('model version content mismatch', 'artifact_version_invalid', '模型版本与内容指纹不一致'),
        ('weights incomplete/incompatible', 'artifact_weights_incompatible', '模型权重不完整或不兼容'),
        ('head/config mismatch', 'artifact_head_incompatible', '多标签分类头或标签配置不匹配'),
        ('original frozen training dataset', 'frozen_hash_mismatch', '模型与原冻结dataset/split hash不匹配'),
        ('privacy', 'dataset_privacy_unreviewed', '数据隐私审核缺失或文本未通过隐私检查'),
        ('human semantic review', 'dataset_semantic_unreviewed', '缺绑定精确数据hash的人工语义审核'),
        ('human-confirmed', 'dataset_human_unconfirmed', '验证或测试数据缺人工确认真值'),
        ('human annotation requires', 'dataset_human_unconfirmed', '人工标注缺审阅者'),
        ('hash mismatch', 'dataset_hash_mismatch', '数据或切分hash不匹配'),
        ('fingerprint mismatch', 'dataset_hash_mismatch', '数据输入指纹不匹配'),
        ('lineage mismatch', 'dataset_split_invalid', '原分组切分来源不一致'),
        ('held-out', 'dataset_split_leakage', '增强数据与冻结留出集重叠'),
        ('taxonomy/input version', 'dataset_schema_mismatch', '数据taxonomy或输入版本不匹配'),
    )
    for fragment, code, message in cases:
        if fragment in text:
            return {'code': code, 'message': message}
    return {'code': domain + '_invalid', 'message': '本地模型校验或设备运行失败' if domain == 'artifact' else '冻结数据结构、标注或切分校验失败'}

def jobs() -> list[dict[str, Any]]:
    """读取真实持久状态；仅拿到OS锁后可判定旧运行任务中断，不篡改活跃持有者。"""
    # 固定锁文件永不删除，避免释放后 unlink 到另一进程已持有的 inode。
    cfg = config()
    control = MiningRuntimeControl(cfg['runtime'] / 'task.lock')
    owned = False
    try:
        control.acquire(); owned = True
    except AlreadyRunningError:
        pass
    try:
        items = [load(p) for p in (cfg['runtime'] / 'jobs').glob('*.json')]
        if _active_job is not None and _active_job['state'] in ('queued', 'running'):
            items = [item for item in items if item['id'] != _active_job['id']] + [dict(_active_job)]
        if owned:
            for item in items:
                if item['state'] in ('queued', 'running') and (_active_job is None or item['id'] != _active_job['id']):
                    item.update(state='interrupted', finished_at=now(), error=safe_error('interrupted'))
                    save_job(item)
        return sorted(items, key=lambda x: x['created_at'], reverse=True)
    finally:
        if owned:
            release(control)

def release(control: MiningRuntimeControl) -> None:
    control.release(keep_file=True)

def version_source(model_version: str) -> dict[str, Any]:
    """只为可核验metadata版本补来源，未知历史预测不包装成人审。"""
    unknown = {'data_mode': None, 'evaluation_source': None}
    try:
        matches = [candidate(entry) for entry in registered_models(config())]
        match = next((entry for entry in matches if entry['model_version'] == model_version and entry['blocked_reason'] is None), None)
        if match is not None:
            return {k: match[k] for k in unknown}
    except Exception:
        pass
    return unknown

def availability(model_key: str | None = None) -> dict[str, Any]:
    """无选择仅列候选；员工显式选择后真实加载，活动任务期间不装载其他权重。"""
    with _mutex:
        cfg = config()
        return _availability(model_key, cfg, registered_models(cfg))

def _availability(model_key: str | None, cfg: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    entry = resolve_model(entries, model_key) if model_key is not None else None
    models = [candidate(value) for value in entries]
    selected_candidate = next((value for value in models if value['model_key'] == model_key), None)
    checks = []; predictor = None; dataset = None; versions = []
    active = next((j for j in jobs() if j['state'] in ('queued', 'running')), None)
    def check(name, ready, code, message, **extra):
        checks.append({'name': name, 'ready': ready, 'code': code, 'message': message, **extra})
    try:
        taxonomy = load_taxonomy(); check('taxonomy', True, 'ready', '主题定义有效')
    except Exception:
        taxonomy = {'version': None, 'labels': []}; check('taxonomy', False, 'invalid_taxonomy', '主题定义无效')
    try:
        state = repo.readiness()
        check('database', state['ready'], 'ready' if state['ready'] else 'schema_missing', '数据库结构就绪' if state['ready'] else '主题表或所需列缺失',
              missing_tables=state['missing_tables'], missing_columns=state['missing_columns'])
        try:
            versions = repo.model_versions()
        except Exception:
            pass
    except Exception:
        check('database', False, 'database_unavailable', '数据库未配置或不可连接')
    if entry is None:
        check('model', False, 'model_selection_required', '请明确选择服务端注册的执行模型')
    elif active is not None:
        check('model', False, 'model_task_active', '已有固定模型身份的活动任务，完成后再检查或切换')
    else:
        try:
            predictor = checked_model(entry['artifact_dir'], cfg['device'], data_mode=entry['data_mode'])
            check('model', True, 'ready', '真实本地模型通过完整权重和版本校验')
        except Exception as exc:
            diagnostic = validation_error(exc, 'artifact')
            check('model', False, diagnostic['code'], diagnostic['message'])
    if entry is None:
        check('dataset', False, 'model_selection_required', '冻结数据随明确选择的注册模型绑定')
    else:
        try:
            dataset = load(entry['dataset']); checked_dataset(dataset, data_mode=entry['data_mode'])
            check('dataset', True, 'ready', '冻结数据通过所选模式的来源、标签及切分校验')
        except Exception as exc:
            dataset = None
            diagnostic = validation_error(exc, 'dataset')
            check('dataset', False, diagnostic['code'], diagnostic['message'])
    frozen = False
    if predictor is not None and dataset is not None:
        try:
            validate_frozen_evaluation(predictor.metadata, dataset); frozen = True
        except Exception:
            pass
    check('frozen_evaluation', frozen, 'ready' if frozen else (
        'frozen_hash_mismatch' if predictor is not None and dataset is not None else 'frozen_binding_unavailable'),
        '模型与原冻结数据匹配' if frozen else ('模型与原冻结dataset/split hash不匹配' if predictor is not None and dataset is not None else '缺少有效模型或冻结数据'))
    selected = predictor.model_version if predictor else (selected_candidate['model_version'] if selected_candidate else None)
    if selected and selected not in versions:
        versions.append(selected)
    ready = {c['name']: c['ready'] for c in checks}
    if selected_candidate is not None:
        selected_candidate['validated'] = predictor is not None and frozen
        failure = next((c for c in checks if c['name'] in ('model', 'dataset', 'frozen_evaluation') and not c['ready']), None)
        if failure:
            selected_candidate['blocked_reason'] = {k: failure[k] for k in ('code', 'message')}
    return {'taxonomy': taxonomy, 'models': models, 'selected_model_key': model_key,
            'selected_model_version': selected, 'model_versions': sorted(versions), 'checks': checks,
            'data_mode': entry['data_mode'] if entry else None,
            'evaluation_source': selected_candidate['evaluation_source'] if selected_candidate else None,
            'can_classify': all(ready[n] for n in ('taxonomy', 'database', 'model', 'dataset', 'frozen_evaluation')),
            'can_evaluate': all(ready[n] for n in ('taxonomy', 'model', 'dataset', 'frozen_evaluation')),
            'active_job': active}

def start(kind: str, parameters: dict[str, Any], *, model_key: str) -> dict[str, Any]:
    """接受时固定配置及内容身份；有限线程只消费该快照，配置切换不改变任务。"""
    global _active_job
    if kind not in ('batch', 'evaluation'):
        raise ValueError('invalid_kind')
    with _mutex:
        cfg = config(); entries = registered_models(cfg); entry = resolve_model(entries, model_key)
        if _active_job is not None and _active_job['state'] in ('queued', 'running'):
            raise AlreadyRunningError('topic task already running')
        jobs()
        control = MiningRuntimeControl(cfg['runtime'] / 'task.lock')
        control.acquire()
        try:
            try:
                signature = artifact_signature(entry['artifact_dir'])
                artifact_identity = file_hash(entry['artifact_dir'] / 'metadata.json')
                dataset_identity = file_hash(entry['dataset'])
            except Exception as exc:
                diagnostic = validation_error(exc, 'artifact')
                raise PrerequisitesBlocked([{'name': 'model', 'ready': False, **diagnostic}]) from None
            state = _availability(model_key, cfg, entries)
            if not state['can_classify' if kind == 'batch' else 'can_evaluate']:
                raise PrerequisitesBlocked(state['checks'])
            accepted = AcceptedModel(model_key, entry['artifact_dir'], entry['dataset'], cfg['device'],
                                     cfg['runtime'], cfg['reports'], entry['data_mode'], state['evaluation_source'],
                                     state['selected_model_version'], artifact_identity, dataset_identity, signature)
            accepted.check()
            dataset = load(accepted.dataset)
            job = {'id': str(uuid4()), 'kind': kind, 'state': 'queued', 'created_at': now(), 'started_at': None,
                   'finished_at': None, 'model_key': model_key, 'model_version': accepted.model_version,
                   'data_mode': accepted.data_mode, 'evaluation_source': accepted.evaluation_source,
                   'artifact_identity': artifact_identity, 'dataset_identity': dataset_identity,
                   'dataset_hash': dataset['dataset_hash'], 'split_hashes': dataset['split_hashes'],
                   'taxonomy_version': TAXONOMY_VERSION, 'parameters': json.loads(json.dumps(parameters)),
                   'result': None, 'error': None, 'report_id': None}
            accepted.check()
            save_job(job, accepted.runtime)
            _active_job = job
            worker = threading.Thread(target=execute, args=(job, control, accepted), daemon=True)
            worker.start()
            return json.loads(json.dumps(job))
        except Exception:
            _active_job = None
            release(control); raise

class PrerequisitesBlocked(Exception):
    def __init__(self, checks):
        self.checks = checks

def execute(job: dict[str, Any], control: MiningRuntimeControl, accepted: AcceptedModel) -> None:
    """全程固定模型快照；每次推理前及报告落盘前阻断模型/数据内容变化。"""
    stage = 'runtime'
    def set_stage(value):
        nonlocal stage
        stage = value
    try:
        job.update(state='running', started_at=now()); save_job(job, accepted.runtime)
        params = job['parameters']
        set_stage('model_load')
        accepted.check()
        predictor = checked_model(accepted.artifact, accepted.device, data_mode=accepted.data_mode)
        if predictor.model_version != job['model_version']:
            raise ValueError('artifact_changed_after_acceptance')
        if job['kind'] == 'batch':
            job['result'] = {'processed': 0, 'persisted': 0, 'stale_source': 0, 'next_cursor': params.get('after')}
            def outcome(row, cursor):
                result = job['result']; result['processed'] += 1
                result[row['persistence_outcome']] += 1; result['next_cursor'] = cursor
                save_job(job, accepted.runtime)
            def stamp(value):
                return datetime.fromisoformat(value) if value else None
            after = params.get('after')
            batch.run_batch(artifact_dir=accepted.artifact, device=accepted.device, data_mode=accepted.data_mode,
                            start_at=stamp(params.get('start_at')), end_at=stamp(params.get('end_at')),
                            after=(stamp(after['created_at']), after['source_id']) if after else None,
                            limit=params['limit'], batch_size=params['batch_size'], on_outcome=outcome,
                            expected_model_version=job['model_version'], predictor=predictor, on_stage=set_stage,
                            before_inference=accepted.check)
        else:
            set_stage('evaluation_validation')
            accepted.check()
            dataset = load(accepted.dataset); checked_dataset(dataset, data_mode=accepted.data_mode)
            validate_frozen_evaluation(predictor.metadata, dataset)
            set_stage('evaluation_inference')
            report = evaluate(predictor, dataset['splits']['test'], split_hash=dataset['split_hashes']['test'],
                              batch_size=params['batch_size'], data_mode=accepted.data_mode,
                              before_inference=accepted.check)
            report.update(dataset_hash=dataset['dataset_hash'], split_hashes=dataset['split_hashes'],
                          model_key=accepted.model_key, artifact_identity=accepted.artifact_identity,
                          dataset_identity=accepted.dataset_identity, model_metadata=predictor.metadata)
            report_id = job['id']; directory = accepted.reports; directory.mkdir(parents=True, exist_ok=True)
            # 保存该模式的冻结证据，历史报告不依赖当前注册项或当前选中的模型。
            predictions = report['predictions']
            envelope = {'id': report_id, 'created_at': now(), 'report': report, 'dataset': dataset,
                        'predictions': predictions, 'evidence_hash': fingerprint({'report': report, 'dataset': dataset, 'predictions': predictions})}
            set_stage('report_persistence')
            accepted.check()
            write_new_json(directory / (report_id + '.json'), envelope)
            job.update(report_id=report_id, result={'report_id': report_id})
        job.update(state='succeeded', finished_at=now()); save_job(job, accepted.runtime)
    except Exception as exc:
        diagnostic = validation_error(exc, 'artifact' if 'artifact' in str(exc) else 'dataset') if (
            stage in ('model_load', 'evaluation_validation') or isinstance(exc, ValueError) and 'changed_after_acceptance' in str(exc)
        ) else safe_error(stage + '_failed')
        job.update(state='failed', finished_at=now(), error={**diagnostic, 'stage': stage})
        try:
            save_job(job, accepted.runtime)
        except OSError:
            # 无法写运行文件时不向线程stderr泄漏路径；下次可读取时running会被标为interrupted。
            pass
    finally:
        release(control)

def conclusions(report: dict[str, Any]) -> dict[str, str]:
    m = report['metrics']; coverage = report['coverage']; seconds = report.get('elapsed_seconds')
    per_topic = []
    for label, value in m['per_class'].items():
        description = '证据不足' if value['support'] == 0 else (
            '有遗漏' if value['fn'] else ('观测无漏分/误分' if not value['fp'] else '有误分'))
        per_topic.append(f"{label}: {description}; support={value['support']}, F1={value['f1']}, recall={value['recall']}, FN={value['fn']}, FP={value['fp']}")
    return {'quality': f"micro F1={m.get('micro_f1')}; supported macro F1={m.get('macro_f1_supported_classes')}；仅描述实测，无验收阈值。" + '；'.join(per_topic),
            'coverage': f"样本={coverage['samples']}，有支持类={m['supported_class_count']}；零支持类不可评价：{','.join(m['unevaluable_classes'])}",
            'performance': f"耗时={seconds} 秒，吞吐={report.get('samples_per_second')} 样本/秒；当前设备实测，不代表生产容量。",
            'threshold': 'unconfirmed'}

def report_index() -> dict[str, Path]:
    """稳定不透明 ID 对应受控报告文件；CLI 历史文件也保留独立版本。"""
    result = {}
    for path in config()['reports'].glob('*.json'):
        if path.name.endswith('.dataset.json'):
            continue
        try:
            identifier = str(UUID(path.stem))
        except ValueError:
            identifier = str(uuid5(NAMESPACE_URL, 'aiden-topic-report:' + path.name))
        result[identifier] = path
    return result

def report_detail(report_id: str) -> dict[str, Any]:
    """独立核验历史冻结审核数据和数值；仅重建白名单schema，绝不透传任意报告文本。"""
    UUID(report_id)
    path = report_index().get(report_id)
    if path is None:
        raise LookupError('not_found')
    payload = load(path)
    if 'reports' in payload:
        # CLI 比较报告的 encoder 仍须绑定原冻结审核数据，不能用当前不同题集代替。
        report = payload['reports']['encoder']
        dataset_path = path.with_name(path.stem + '.dataset.json')
        if 'dataset' in payload:
            dataset = payload['dataset']
        elif dataset_path.is_file():
            dataset = load(dataset_path)
        else:
            # 旧报告没有快照时只匹配原内容hash；不能拿当前另一数据集代替。
            matches = []
            for entry in registered_models(config()):
                try:
                    value = load(entry['dataset'])
                except (OSError, ValueError):
                    continue
                if value.get('dataset_hash') == payload.get('dataset_hash') and value.get('split_hashes', {}).get('test') == report.get('test_split_hash'):
                    matches.append(value)
            if not matches or any(value != matches[0] for value in matches):
                raise ValueError('historical frozen dataset unavailable')
            dataset = matches[0]
        report = {**report, 'dataset_hash': payload['dataset_hash'],
                  'split_hashes': report.get('split_hashes', dataset['split_hashes'])}
        predictions = report['predictions']
        payload = {'id': report_id, 'created_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                   'report': report, 'dataset': dataset, 'predictions': predictions}
        payload['evidence_hash'] = fingerprint({'report': report, 'dataset': dataset, 'predictions': predictions})
    else:
        dataset = payload['dataset']; report = payload['report']; predictions = payload['predictions']
    declared_mode = report.get('data_mode')
    mode = declared_mode if declared_mode is not None else HUMAN_REVIEWED
    require_data_mode(report, mode)
    require_data_mode(dataset, mode)
    checked_dataset(dataset, data_mode=mode)
    known_source = report.get('evaluation_source')
    if declared_mode is not None and known_source is None:
        raise ValueError('report evaluation source missing')
    metadata = report.get('model_metadata')
    if metadata is None and mode == SYNTHETIC_EXPERIMENT:
        # 旧实验报告没有训练metadata时，必须找到同版本已注册产物以核验全部冻结切分。
        for entry in registered_models(config()):
            info = candidate(entry)
            if entry['data_mode'] == mode and info['model_version'] == report['model_version'] and info['blocked_reason'] is None:
                metadata = load(entry['artifact_dir'] / 'metadata.json')
                break
        if metadata is None:
            raise ValueError('report original model binding unavailable')
    if metadata is not None:
        validate_metadata(metadata, data_mode=mode)
        validate_frozen_evaluation(metadata, dataset)
        if metadata['model_version'] != report['model_version']:
            raise ValueError('report model version mismatch')
        if report.get('model_metadata') is not None:
            for field in ('artifact_identity', 'dataset_identity'):
                if not re.fullmatch(r'[0-9a-f]{64}', report.get(field, '')):
                    raise ValueError('report accepted identity invalid')
    rows = dataset['splits']['test']
    if payload.get('evidence_hash') != fingerprint({'report': report, 'dataset': dataset, 'predictions': predictions}):
        raise ValueError('invalid_report_evidence')
    if report['dataset_hash'] != dataset['dataset_hash'] or report['split_hashes'] != dataset['split_hashes'] or report['test_split_hash'] != dataset['split_hashes']['test'] or len(predictions) != len(rows):
        raise ValueError('invalid_report_lineage')
    from .taxonomy import LABEL_IDS, validate_labels
    import math
    if report['taxonomy_version'] != TAXONOMY_VERSION:
        raise ValueError('report_taxonomy_mismatch')
    clean_predictions = []
    for row, prediction in zip(rows, predictions):
        labels = validate_labels(prediction['predicted_labels'])
        scores = prediction['scores']
        if prediction.get('id') != row['id'] or set(scores) != set(LABEL_IDS) or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()):
            raise ValueError('invalid_report_prediction')
        if prediction['status'] not in ('predicted', 'uncertain') or (prediction['status'] == 'predicted') != bool(labels):
            raise ValueError('invalid_report_prediction_status')
        clean_predictions.append({'id': row['id'], 'scores': scores, 'predicted_labels': labels, 'status': prediction['status']})
    if predictions != clean_predictions:
        raise ValueError('unexpected_report_prediction_fields')
    class RecordedPredictor:
        model_version = report['model_version']
        taxonomy_version = report['taxonomy_version']
        def __init__(self):
            self.offset = 0
        def predict_batch(self, texts):
            values = predictions[self.offset:self.offset + len(texts)]
            self.offset += len(texts)
            return [{k: v for k, v in value.items() if k != 'id'} for value in values]
    reconstructed = evaluate(RecordedPredictor(), rows, split_hash=dataset['split_hashes']['test'], data_mode=mode)
    for key in ('metrics', 'status_metrics', 'coverage', 'errors', 'predictions'):
        if reconstructed[key] != report[key]:
            raise ValueError('invalid_report_evaluation_payload')
    import math
    elapsed = report.get('elapsed_seconds')
    throughput = report.get('samples_per_second')
    if not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed) or elapsed <= 0 or not isinstance(throughput, (float, int)) or not math.isclose(throughput, len(rows) / elapsed):
        raise ValueError('invalid_report_performance')
    # 输出采用重建的固定schema，绝不透传 baseline endpoint、human notes 或任意 JSON 字符串。
    public = {k: reconstructed[k] for k in ('metrics', 'status_metrics', 'coverage', 'errors', 'predictions')}
    if not re.fullmatch(r'[0-9a-f]{64}', report['model_version']):
        raise ValueError('invalid_report_model_version')
    resources = report.get('resource_usage') or {}
    device = resources.get('device')
    device = device if isinstance(device, str) and re.fullmatch(r'cpu|cuda(?::[0-9]+)?|mps', device) else None
    peak = resources.get('gpu_peak_bytes')
    peak = peak if isinstance(peak, int) and peak >= 0 else None
    model_key = report.get('model_key')
    model_key = model_key if isinstance(model_key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', model_key) else None
    public.update(model_key=model_key, data_mode=declared_mode, evaluation_source=known_source)
    public.update(model_version=report['model_version'], taxonomy_version=report['taxonomy_version'],
                  dataset_hash=report['dataset_hash'], split_hashes=report['split_hashes'],
                  test_split_hash=report['test_split_hash'], elapsed_seconds=elapsed,
                  samples_per_second=throughput, usage=None, cost=None, resource_usage={'device': device, 'gpu_peak_bytes': peak})
    return {'id': report_id, 'created_at': payload['created_at'], 'model_key': model_key,
            'data_mode': declared_mode, 'evaluation_source': known_source,
            'report': public, 'conclusions': conclusions(public)}

def reports(model_version: str | None = None) -> list[dict[str, Any]]:
    """列出真实文件历史；不可核验报告保留安全版本标识并明确不可用。"""
    items = []
    for identifier, path in report_index().items():
        try:
            detail = report_detail(identifier); r = detail['report']
            items.append({'id': detail['id'], 'created_at': detail['created_at'], 'model_version': r['model_version'],
                          'taxonomy_version': r['taxonomy_version'], 'dataset_hash': r['dataset_hash'],
                          'test_split_hash': r['test_split_hash'], 'available': True, 'reason': None, 'conclusions': detail['conclusions']})
            items[-1].update(model_key=r['model_key'], data_mode=r['data_mode'], evaluation_source=r['evaluation_source'])
        except Exception:
            # 仅保留格式受限的历史版本/hash，不泄漏任意文件文本；这些不是已验证的指标。
            import re
            identity = {}
            try:
                raw = load(path); old = raw.get('report') or raw.get('reports', {}).get('encoder') or {}
                for key in ('model_version', 'dataset_hash', 'test_split_hash'):
                    value = old.get(key) or raw.get(key)
                    identity[key] = value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) else None
                version = old.get('taxonomy_version')
                identity['taxonomy_version'] = version if isinstance(version, str) and re.fullmatch(r'aiden-topic-[a-z0-9-]{1,50}', version) else None
            except Exception:
                pass
            items.append({'id': identifier, 'created_at': None, 'model_version': identity.get('model_version'),
                          'taxonomy_version': identity.get('taxonomy_version'), 'dataset_hash': identity.get('dataset_hash'),
                          'test_split_hash': identity.get('test_split_hash'), 'available': False,
                          'reason': 'taxonomy_version_mismatch' if identity.get('taxonomy_version') and identity['taxonomy_version'] != TAXONOMY_VERSION else 'report_evidence_unverified',
                          'conclusions': None, 'model_key': None, 'data_mode': None, 'evaluation_source': None})
    return sorted((item for item in items if model_version is None or item['model_version'] == model_version),
                  key=lambda x: x['created_at'] or '', reverse=True)
