"""只加载完整已训练本地产物；错误或版本不一致硬失败，无基线回退。"""
from __future__ import annotations
import hashlib
import math
from pathlib import Path
import json
from .data import INPUT_VERSION, HUMAN_REVIEWED, fingerprint, prepare_input, require_data_mode
from .taxonomy import LABEL_IDS, TAXONOMY_VERSION, load_taxonomy


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def select_labels(scores: dict, thresholds: dict) -> tuple[list[str], str]:
    if set(scores) != set(LABEL_IDS) or set(thresholds) != set(LABEL_IDS) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values()) or any(not math.isfinite(v) or not 0 < v < 1 for v in thresholds.values()):
        raise ValueError('invalid score/threshold label vector')
    labels = [k for k in LABEL_IDS if scores[k] >= thresholds[k]]
    if 'other' in labels and len(labels) > 1:
        # 两种互斥解释都过线是待人工裁决，不静默掩盖冲突。
        return [], 'uncertain'
    return labels, 'predicted' if labels else 'uncertain'


def validate_metadata(metadata: dict, *, data_mode: str = HUMAN_REVIEWED) -> None:
    """候选静态契约校验；不校验磁盘权重或授予模型执行就绪状态。"""
    m = metadata
    require_data_mode(m, data_mode)
    if m.get('taxonomy') != load_taxonomy() or m.get('label_ids') != list(LABEL_IDS) or m.get('taxonomy_version') != TAXONOMY_VERSION or m.get('input_version') != INPUT_VERSION:
        raise ValueError('artifact taxonomy/order/input version mismatch')
    if m.get('taxonomy_hash') != fingerprint(load_taxonomy()) or not m.get('trained'):
        raise ValueError('artifact is not a trained taxonomy-compatible classifier')
    thresholds = m.get('thresholds')
    if not isinstance(thresholds, dict) or set(thresholds) != set(LABEL_IDS) or any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v) or not 0 < v < 1 for v in thresholds.values()):
        raise ValueError('invalid threshold vector')
    split_hashes = m.get('split_hashes')
    if m.get('threshold_source') != 'validation' or not isinstance(split_hashes, dict) or any(not split_hashes.get(name) for name in ('train', 'validation', 'test')):
        raise ValueError('missing validation calibration/data lineage')
    expected = fingerprint({k: v for k, v in m.items() if k != 'model_version'})
    if m.get('model_version') != expected:
        raise ValueError('model version content mismatch')


class EncoderPredictor:
    def __init__(self, artifact_dir, device='cpu', *, data_mode: str = HUMAN_REVIEWED):
        directory = Path(artifact_dir)
        self.metadata = json.loads((directory / 'metadata.json').read_text(encoding='utf-8'))
        m = self.metadata
        validate_metadata(m, data_mode=data_mode)
        thresholds = m['thresholds']
        files = m['files']
        actual = {p.relative_to(directory).as_posix(): file_hash(p) for p in directory.rglob('*') if p.is_file() and p.name != 'metadata.json'}
        if files != actual or not any(k.endswith(('.safetensors','.bin')) for k in files):
            raise ValueError('artifact content checksum mismatch or missing weights')
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        self.device = device
        self.model, loading = AutoModelForSequenceClassification.from_pretrained(directory, local_files_only=True, output_loading_info=True)
        if any(loading.get(k) for k in ('missing_keys','mismatched_keys','unexpected_keys','error_msgs')):
            raise ValueError('artifact weights incomplete/incompatible; random inference forbidden')
        self.model.to(device)
        self.tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
        if self.model.config.num_labels != len(LABEL_IDS) or self.model.config.problem_type != 'multi_label_classification' or [self.model.config.id2label[i] for i in range(len(LABEL_IDS))] != list(LABEL_IDS):
            raise ValueError('trained classification head/config mismatch')
        self.model.eval()
        self.model_version = m['model_version']
        self.taxonomy_version = TAXONOMY_VERSION
        self.thresholds = thresholds
    def predict_batch(self, texts):
        if not texts:
            return []
        encoded = self.tokenizer([prepare_input(x) for x in texts], padding=True, truncation=True, max_length=self.metadata['train_args']['max_length'], return_tensors='pt').to(self.device)
        with self.torch.inference_mode():
            probabilities = self.model(**encoded).logits.sigmoid().cpu().tolist()
        results = []
        for vector in probabilities:
            scores = dict(zip(LABEL_IDS, vector))
            labels, status = select_labels(scores, self.thresholds)
            results.append({'scores': scores, 'predicted_labels': labels, 'status': status})
        return results
