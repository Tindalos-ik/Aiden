"""员工旁路控制台：受控本地文件、有限任务、真实编码器与冻结数据闸门。"""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, uuid5, UUID, NAMESPACE_URL
from typing import Any
from app.config.settings import BACKEND_DIR
from app.persistence.mysql import topic_classification as repo
from app.services.rag.runtime_control import MiningRuntimeControl, AlreadyRunningError
from . import batch
from .data import validate_dataset, fingerprint, write_new_json
from .evaluation import evaluate, validate_frozen_evaluation
from .model import EncoderPredictor
from .taxonomy import load_taxonomy, TAXONOMY_VERSION

_mutex = threading.Lock()
_model_cache = None
_model_cache_lock = threading.Lock()

def checked_model(directory: Path, device: str) -> EncoderPredictor:
    """文件 stat 清单变化即失效；首次/变更必走真实 checksum 和权重加载。"""
    global _model_cache
    signature = (str(directory), device, tuple(sorted(
        (p.relative_to(directory).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
        for p in directory.rglob('*') if p.is_file())))
    with _model_cache_lock:
        if _model_cache is None or _model_cache[0] != signature:
            predictor = EncoderPredictor(directory, device)
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
    return {'artifact': path('TOPIC_CLASSIFIER_ARTIFACT_DIR'), 'dataset': path('TOPIC_CLASSIFIER_DATASET'),
            'runtime': runtime, 'reports': path('TOPIC_CLASSIFIER_REPORT_DIR', '.tmp/topic-console/reports'),
            'device': os.getenv('TOPIC_CLASSIFIER_DEVICE', 'cpu').strip()}

def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))

def save_job(job: dict[str, Any]) -> None:
    """原子替换受控任务文件；不得保存原异常、原话、路径或凭据。"""
    directory = config()['runtime'] / 'jobs'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (job['id'] + '.json')
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(job, ensure_ascii=False, default=str), encoding='utf-8')
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
        if owned:
            for item in items:
                if item['state'] in ('queued', 'running'):
                    item.update(state='interrupted', finished_at=now(), error=safe_error('interrupted'))
                    save_job(item)
        return sorted(items, key=lambda x: x['created_at'], reverse=True)
    finally:
        if owned:
            release(control)

def release(control: MiningRuntimeControl) -> None:
    control.release(keep_file=True)

def availability() -> dict[str, Any]:
    """检查真实消费者依赖，模型必须完整加载成功；变化失效缓存，不回退规则或LLM。"""
    cfg = config(); checks = []; predictor = None; dataset = None; versions = []
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
    if cfg['artifact'] is None:
        check('model', False, 'artifact_unconfigured', '未配置本地已训练模型')
    else:
        try:
            predictor = checked_model(cfg['artifact'], cfg['device'])
            check('model', True, 'ready', '真实本地模型通过校验')
        except FileNotFoundError:
            check('model', False, 'artifact_missing', '本地模型文件缺失')
        except Exception as exc:
            diagnostic = validation_error(exc, 'artifact')
            check('model', False, diagnostic['code'], diagnostic['message'])
    if cfg['dataset'] is None:
        check('dataset', False, 'dataset_unconfigured', '未配置人工审核冻结数据集')
    else:
        try:
            dataset = load(cfg['dataset']); validate_dataset(dataset)
            check('dataset', True, 'ready', '冻结数据及人工隐私/语义审核有效')
        except FileNotFoundError:
            check('dataset', False, 'dataset_missing', '冻结数据文件缺失')
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
    selected = predictor.model_version if predictor else None
    if selected and selected not in versions:
        versions.append(selected)
    active = next((j for j in jobs() if j['state'] in ('queued', 'running')), None)
    ready = {c['name']: c['ready'] for c in checks}
    return {'taxonomy': taxonomy, 'selected_model_version': selected, 'model_versions': sorted(versions), 'checks': checks,
            'can_classify': all(ready[n] for n in ('taxonomy', 'database', 'model')),
            'can_evaluate': all(ready[n] for n in ('taxonomy', 'model', 'dataset', 'frozen_evaluation')),
            'active_job': active}

def start(kind: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """非阻塞跨进程锁先取得后启动有限线程；锁由执行线程持有至终态，状态独立保留。"""
    if kind not in ('batch', 'evaluation'):
        raise ValueError('invalid_kind')
    with _mutex:
        jobs()  # 获取新锁前先把已无持有者的旧 running 状态标记为中断。
        control = MiningRuntimeControl(config()['runtime'] / 'task.lock')
        control.acquire()
        try:
            # availability 内部不能重新获取正在持有的锁来判中断。
            state = availability()
            if not state['can_classify' if kind == 'batch' else 'can_evaluate']:
                raise PrerequisitesBlocked(state['checks'])
            job = {'id': str(uuid4()), 'kind': kind, 'state': 'queued', 'created_at': now(), 'started_at': None,
                   'finished_at': None, 'model_version': state['selected_model_version'], 'taxonomy_version': TAXONOMY_VERSION,
                   'parameters': parameters, 'result': None, 'error': None, 'report_id': None}
            save_job(job)
            worker = threading.Thread(target=execute, args=(job, control), daemon=True)
            worker.start()
            return job
        except Exception:
            release(control); raise

class PrerequisitesBlocked(Exception):
    def __init__(self, checks):
        self.checks = checks

def execute(job: dict[str, Any], control: MiningRuntimeControl) -> None:
    """执行真实批次/冻结评价；逐条完成结果持久化，失败保留实际进度与安全阶段诊断。"""
    cfg = config()
    stage = 'runtime'
    def set_stage(value):
        nonlocal stage
        stage = value
    try:
        job.update(state='running', started_at=now()); save_job(job)
        params = job['parameters']
        set_stage('model_load')
        predictor = checked_model(cfg['artifact'], cfg['device'])
        if predictor.model_version != job['model_version']:
            raise ValueError('artifact_changed_after_acceptance')
        if job['kind'] == 'batch':
            job['result'] = {'processed': 0, 'persisted': 0, 'stale_source': 0, 'next_cursor': params.get('after')}
            def outcome(row, cursor):
                result = job['result']; result['processed'] += 1
                result[row['persistence_outcome']] += 1; result['next_cursor'] = cursor
                save_job(job)
            def stamp(value):
                return datetime.fromisoformat(value) if value else None
            after = params.get('after')
            batch.run_batch(artifact_dir=cfg['artifact'], device=cfg['device'],
                            start_at=stamp(params.get('start_at')), end_at=stamp(params.get('end_at')),
                            after=(stamp(after['created_at']), after['source_id']) if after else None,
                            limit=params['limit'], batch_size=params['batch_size'], on_outcome=outcome,
                            expected_model_version=job['model_version'], predictor=predictor, on_stage=set_stage)
        else:
            set_stage('evaluation_validation')
            dataset = load(cfg['dataset']); validate_dataset(dataset); validate_frozen_evaluation(predictor.metadata, dataset)
            set_stage('evaluation_inference')
            report = evaluate(predictor, dataset['splits']['test'], split_hash=dataset['split_hashes']['test'], batch_size=params['batch_size'])
            report.update(dataset_hash=dataset['dataset_hash'], split_hashes=dataset['split_hashes'])
            report_id = job['id']; directory = cfg['reports']; directory.mkdir(parents=True, exist_ok=True)
            # 保留每报告的审核数据和原始预测：以后换当前配置仍可验证历史数值和文本。
            predictions = report['predictions']
            envelope = {'id': report_id, 'created_at': now(), 'report': report, 'dataset': dataset,
                        'predictions': predictions, 'evidence_hash': fingerprint({'report': report, 'dataset': dataset, 'predictions': predictions})}
            set_stage('report_persistence')
            write_new_json(directory / (report_id + '.json'), envelope)
            job.update(report_id=report_id, result={'report_id': report_id})
        job.update(state='succeeded', finished_at=now()); save_job(job)
    except Exception as exc:
        diagnostic = validation_error(exc, 'artifact' if stage == 'model_load' else 'dataset') if stage in ('model_load', 'evaluation_validation') else safe_error(stage + '_failed')
        job.update(state='failed', finished_at=now(), error={**diagnostic, 'stage': stage})
        try:
            save_job(job)
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
        dataset = load(dataset_path if dataset_path.is_file() else config()['dataset'])
        report = {**report, 'dataset_hash': payload['dataset_hash'], 'split_hashes': dataset['split_hashes']}
        predictions = report['predictions']
        payload = {'id': report_id, 'created_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                   'report': report, 'dataset': dataset, 'predictions': predictions}
        payload['evidence_hash'] = fingerprint({'report': report, 'dataset': dataset, 'predictions': predictions})
    else:
        dataset = payload['dataset']; report = payload['report']; predictions = payload['predictions']
    validate_dataset(dataset)
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
    reconstructed = evaluate(RecordedPredictor(), rows, split_hash=dataset['split_hashes']['test'])
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
    import re
    if not re.fullmatch(r'[0-9a-f]{64}', report['model_version']):
        raise ValueError('invalid_report_model_version')
    resources = report.get('resource_usage') or {}
    device = resources.get('device')
    device = device if isinstance(device, str) and re.fullmatch(r'cpu|cuda(?::[0-9]+)?|mps', device) else None
    peak = resources.get('gpu_peak_bytes')
    peak = peak if isinstance(peak, int) and peak >= 0 else None
    public.update(model_version=report['model_version'], taxonomy_version=report['taxonomy_version'],
                  dataset_hash=report['dataset_hash'], split_hashes=report['split_hashes'],
                  test_split_hash=report['test_split_hash'], elapsed_seconds=elapsed,
                  samples_per_second=throughput, usage=None, cost=None, resource_usage={'device': device, 'gpu_peak_bytes': peak})
    return {'id': report_id, 'created_at': payload['created_at'], 'report': public, 'conclusions': conclusions(public)}

def reports() -> list[dict[str, Any]]:
    """列出真实文件历史；不可核验报告保留安全版本标识并明确不可用。"""
    items = []
    for identifier, path in report_index().items():
        try:
            detail = report_detail(identifier); r = detail['report']
            items.append({'id': detail['id'], 'created_at': detail['created_at'], 'model_version': r['model_version'],
                          'taxonomy_version': r['taxonomy_version'], 'dataset_hash': r['dataset_hash'],
                          'test_split_hash': r['test_split_hash'], 'available': True, 'reason': None, 'conclusions': detail['conclusions']})
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
                          'conclusions': None})
    return sorted(items, key=lambda x: x['created_at'] or '', reverse=True)
