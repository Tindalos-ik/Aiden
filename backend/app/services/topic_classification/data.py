"""默认人工隐私/语义门控；显式合成实验仅消费未人审预标签，并先归组后切分。"""
from __future__ import annotations
import hashlib
import json
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
from app.services.rag.sanitization import redact_sensitive_text
from .taxonomy import TAXONOMY_VERSION, LABEL_IDS, validate_labels

INPUT_VERSION = 'topic-raw-redacted-v1'

HUMAN_REVIEWED = 'human_reviewed'
SYNTHETIC_EXPERIMENT = 'synthetic_experiment'


def require_data_mode(value: dict, data_mode: str = HUMAN_REVIEWED) -> None:
    """默认消费者拒绝实验数据/模型；旧版未写模式的人工产物仍按原契约消费。"""
    if data_mode not in (HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT):
        raise ValueError('unknown topic data mode')
    if value.get('data_mode', HUMAN_REVIEWED) != data_mode:
        raise ValueError('topic data mode requires explicit matching authorization')
    if data_mode == SYNTHETIC_EXPERIMENT and value.get('evaluation_source') != 'synthetic_model_prelabels':
        raise ValueError('synthetic experiment evaluation source required')
    if data_mode == HUMAN_REVIEWED and value.get('evaluation_source', 'human_confirmed') != 'human_confirmed':
        raise ValueError('human-reviewed evaluation source required')


def validate_synthetic_row(row: dict) -> None:
    """仅接受明确合成预标签，不制造人工隐私或标签确认身份。"""
    refs = row.get('source_refs')
    if row.get('source') != 'synthetic' or not isinstance(refs, list) or not refs or any(ref.get('source') != 'synthetic' for ref in refs):
        raise ValueError('synthetic experiment requires exclusively synthetic sources')
    if row.get('annotation_status') != 'prelabel' or row.get('human_reviewed') is not False or row.get('privacy_confirmed') is not False:
        raise ValueError('synthetic experiment requires unconfirmed model prelabels')
    if any(row.get(k) for k in ('privacy_reviewer', 'annotator', 'human_confirmed', 'reviewer')):
        raise ValueError('synthetic experiment must not claim human review')
    if row.get('classification_status') != 'predicted' or not row.get('labels'):
        raise ValueError('synthetic supervised rows require nonempty predicted labels')
    expected = [int(label in row['labels']) for label in LABEL_IDS]
    if 'multi_hot' in row and row['multi_hot'] != expected:
        raise ValueError('synthetic label encoding mismatch')


def prepare_synthetic_dataset(rows: list[dict], *, seed: int = 42) -> dict:
    """从原生监督记录重建实验切分；原生组/hash不是新产物的准入证明。"""
    originals = [{**{k:v for k,v in row.items() if k != 'group_id'}, 'multi_hot': [int(label in row['labels']) for label in LABEL_IDS]} for row in rows]
    result = prepare_dataset(originals, seed=seed, semantic_review=None, data_mode=SYNTHETIC_EXPERIMENT)
    return result


def prepare_input(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError('nonempty raw user question required')
    text = redact_sensitive_text(text)
    # 标签保留以避免脱敏把“修改收件人”等诉求吞掉；裸姓名/地址仍须人工审核。
    for pattern, token in [
        (r'((?:姓名|收件人|联系人|我叫)\s*[:：]?\s*)[\u4e00-\u9fff]{2,4}', '[姓名]'),
        (r'((?:账号|账户|银行卡号|支付宝|微信号)\s*[:：]?\s*)[A-Za-z0-9_@.\-]{3,}', '[账号]'),
        (r'(?<![A-Za-z0-9])\d{10,}(?![A-Za-z0-9])', '[长编号]'),
        (r'((?:地址|住址|收货地址)\s*[:：]\s*)[^，。；\n]+', '[地址]'),
    ]:
        text = re.sub(pattern, lambda m: (m.group(1) if m.lastindex else '') + token, text)
    return text.strip()


def input_hash(raw: str) -> str:
    return hashlib.sha256((INPUT_VERSION + '\n' + prepare_input(raw)).encode()).hexdigest()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def write_new_json(path: str | Path, value) -> None:
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def dataset_coverage(splits: dict) -> dict:
    from .taxonomy import LABEL_IDS
    result = {}
    for name, rows in splits.items():
        support = {k:sum(k in r['labels'] for r in rows) for k in LABEL_IDS}
        result[name] = {'samples':len(rows),'groups':len({r['group_id'] for r in rows}),'multilabel_samples':sum(len(r['labels'])>1 for r in rows),'multilabel_ratio':sum(len(r['labels'])>1 for r in rows)/len(rows) if rows else None,'label_support':support,'missing_classes':[k for k,v in support.items() if not v],'human_confirmed':sum(r['annotation_status']=='human_confirmed' for r in rows)}
    return result


def prepare_dataset(rows: list[dict], *, seed: int = 42, semantic_review: dict | None, data_mode: str = HUMAN_REVIEWED) -> dict:
    """默认人工契约要求指纹绑定的人工语义复核；实验模式只声明自动分组。

    精确原话合并来源审计，近重复保留自然问法但共组。实验预标签不成为人工真值。
    默认semantic_review={dataset_hash,confirmed,reviewer,revision}必须由人工签署。
    """
    if data_mode not in (HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT):
        raise ValueError('unknown topic data mode')
    synthetic = data_mode == SYNTHETIC_EXPERIMENT
    if not rows:
        raise ValueError('no annotated raw user questions')
    records = []
    for row in rows:
        if synthetic:
            validate_synthetic_row(row)
        elif not row.get('privacy_confirmed') or not row.get('privacy_reviewer'):
            raise ValueError('human privacy confirmation required')
        if row.get('taxonomy_version') != TAXONOMY_VERSION:
            raise ValueError('annotation taxonomy mismatch')
        text = prepare_input(row['original_question'])
        if text != row['original_question']:
            raise ValueError('source export is not fully redacted; review before preparation')
        labels = validate_labels(row.get('labels', []))
        if row.get('annotation_status') not in ('human_confirmed', 'prelabel'):
            raise ValueError('annotation_status required')
        if not all(row.get(k) for k in ('annotation_revision','source_refs','data_version','annotation_method')) or 'annotation_audit' not in row:
            raise ValueError('data/source/annotation version and audit required')
        status = row.get('classification_status')
        if status not in ('predicted','uncertain','insufficient_context') or bool(labels) != (status == 'predicted'):
            raise ValueError('labels must correspond to explicit human classification status')
        if row['annotation_status'] == 'human_confirmed' and not row.get('annotator'):
            raise ValueError('human annotation requires annotator')
        if row.get('augmentation_parent'):
            raise ValueError('augmentations must be attached after split')
        records.append({**row, 'labels': labels, 'input_hash': input_hash(text)})
    ids = [x['id'] for x in records]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate sample id')
    unique = {}
    for row in records:
        row.setdefault('dedup_source_ids',[row['id']])
        existing = unique.get(row['input_hash'])
        if existing is None:
            unique[row['input_hash']] = row
            continue
        if any(existing.get(k) != row.get(k) for k in ('labels','annotation_status','classification_status','semantic_cluster')):
            raise ValueError('exact duplicate annotation/semantic conflict requires human resolution')
        existing['source_refs'] = list({fingerprint(ref):ref for ref in existing['source_refs']+row['source_refs']}.values())
        existing['dedup_source_ids'] = list(dict.fromkeys(existing['dedup_source_ids']+row['dedup_source_ids']))
        existing.setdefault('dedup_annotation_audit',[]).append({k:row.get(k) for k in ('id','annotator','annotation_revision','annotation_method','annotation_audit','data_version')})
    records = list(unique.values())
    ids = [x['id'] for x in records]
    dataset_hash = fingerprint(records)
    if synthetic:
        experiment_review = {'dataset_hash': dataset_hash, 'confirmed': False, 'method': 'synthetic_cluster_and_near_duplicate_grouping', 'human_reviewed': False}
        if semantic_review is not None and semantic_review != experiment_review:
            raise ValueError('synthetic grouping manifest mismatch')
        semantic_review = experiment_review
    elif not semantic_review or semantic_review.get('dataset_hash') != dataset_hash or semantic_review.get('confirmed') is not True or not semantic_review.get('reviewer') or not semantic_review.get('revision'):
        raise ValueError(f'human semantic review required for dataset_hash={dataset_hash}')
    parent = list(range(len(records)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    conversations = [{s.get('conversation_id') for s in row['source_refs']} - {None, ''} for row in records]
    for i, a in enumerate(records):
        for j in range(i):
            b = records[j]
            conversations_a = conversations[i]
            conversations_b = conversations[j]
            same_semantics = bool(a.get('semantic_cluster') and a.get('semantic_cluster') == b.get('semantic_cluster'))
            # 长度与quick_ratio仅作保证上界筛选；通过quick_ratio仍须同一Matcher精确ratio >= .9，不能据其直接判重复。
            lengths = (len(a['original_question']), len(b['original_question']))
            duplicate = bool(conversations_a & conversations_b) or same_semantics or a['input_hash'] == b['input_hash']
            if not duplicate and 2 * min(lengths) >= .9 * sum(lengths):
                matcher = SequenceMatcher(None, a['original_question'], b['original_question'])
                duplicate = matcher.quick_ratio() >= .9 and matcher.ratio() >= .9
            if duplicate:
                parent[find(i)] = find(j)
    groups = {}
    for i, row in enumerate(records):
        groups.setdefault(find(i), []).append(row)
    keys = sorted(groups, key=lambda k: sorted(x['id'] for x in groups[k]))
    random.Random(seed).shuffle(keys)
    # 组数不足不能把空验证/测试当有效训练材料。
    if len(keys) < 3:
        raise ValueError('at least three independent semantic/conversation groups required')
    n_eval = max(1, len(keys) // 10)
    splits = {'train': [], 'validation': [], 'test': []}
    group_labels = {k:{label for r in groups[k] for label in r['labels']} for k in keys}
    remaining = list(keys)
    allocation = {}
    remaining_counts = {label: sum(label in group_labels[k] for k in remaining) for label in LABEL_IDS}
    # 固定种子破平局；优先让验证/测试覆盖未覆盖的稀有类，保留至少一个训练来源组。
    for split in ('test','validation'):
        covered = set()
        for _ in range(n_eval):
            candidates = [k for k in remaining if synthetic or all(r['annotation_status'] == 'human_confirmed' for r in groups[k])]
            if not candidates:
                raise ValueError('insufficient human-confirmed held-out groups')
            def score(k):
                counts = {label:remaining_counts[label] for label in group_labels[k]}
                return (all(n > 1 for n in counts.values()), sum(1/counts[label] for label in group_labels[k]-covered))
            chosen = max(candidates,key=score)
            allocation[chosen] = split
            covered.update(group_labels[chosen])
            remaining.remove(chosen)
            for label in group_labels[chosen]:
                remaining_counts[label] -= 1
    for key in keys:
        split = allocation.get(key,'train')
        group_id = fingerprint(sorted(x['id'] for x in groups[key]))
        for row in groups[key]:
            if not synthetic and split != 'train' and row['annotation_status'] != 'human_confirmed':
                raise ValueError('validation/test require exclusively human-confirmed annotations')
            splits[split].append({**row, 'group_id': group_id})
    result = {'taxonomy_version': TAXONOMY_VERSION, 'input_version': INPUT_VERSION, 'seed': seed, 'dataset_hash': dataset_hash, 'original_ids':ids, 'dedup':{'source_samples':sum(len(r['dedup_source_ids']) for r in records),'unique_inputs':len(records)}, 'semantic_review': semantic_review, 'splits': splits, 'coverage':dataset_coverage(splits), 'split_hashes': {k: fingerprint(v) for k, v in splits.items()}}
    if synthetic:
        result.update(data_mode=SYNTHETIC_EXPERIMENT, evaluation_source='synthetic_model_prelabels', label_ids=list(LABEL_IDS))
    return result


def attach_augmentations(dataset: dict, rows: list[dict]) -> dict:
    require_data_mode(dataset)
    parents = {r['id']: r for r in dataset['splits']['train']}
    for row in rows:
        p = parents.get(row.get('augmentation_parent'))
        if not p or row.get('parent_input_hash') != p['input_hash'] or row.get('parent_split_hash') != dataset['split_hashes']['train']:
            raise ValueError('augmentation must bind original train parent and split fingerprint')
        if not row.get('privacy_confirmed') or not row.get('human_confirmed') or not row.get('reviewer'):
            raise ValueError('augmentation requires human privacy and semantic confirmation')
        if validate_labels(row['labels']) != p['labels']:
            raise ValueError('augmentation changed parent labels')
        text = prepare_input(row['original_question'])
        if text != row['original_question']:
            raise ValueError('augmentation privacy review incomplete')
    result = json.loads(json.dumps(dataset))
    result['augmentation_base_train_hash'] = dataset['split_hashes']['train']
    for row in rows:
        p = parents[row['augmentation_parent']]
        result['splits']['train'].append({**p, **row, 'input_hash': input_hash(row['original_question']), 'group_id': p['group_id'], 'annotation_status': 'human_confirmed', 'annotator':row['reviewer'], 'classification_status':'predicted', 'privacy_reviewer':row['reviewer']})
    result['split_hashes']['train'] = fingerprint(result['splits']['train'])
    result['coverage'] = dataset_coverage(result['splits'])
    validate_dataset(result)
    return result


def validate_dataset(dataset: dict, *, data_mode: str = HUMAN_REVIEWED) -> None:
    """消费端重新构建原始分组切分，防止自改hash绕过泄漏与人工gate。"""
    require_data_mode(dataset, data_mode)
    if data_mode == SYNTHETIC_EXPERIMENT and dataset.get('label_ids') != list(LABEL_IDS):
        raise ValueError('synthetic label order mismatch')
    if dataset.get('taxonomy_version') != TAXONOMY_VERSION or dataset.get('input_version') != INPUT_VERSION:
        raise ValueError('dataset taxonomy/input version mismatch')
    if set(dataset['splits']) != {'train','validation','test'}:
        raise ValueError('train/validation/test required')
    originals = []
    augmentations = []
    identities = set()
    for split, rows in dataset['splits'].items():
        if fingerprint(rows) != dataset['split_hashes'][split]:
            raise ValueError('split hash mismatch')
        for row in rows:
            if row['id'] in identities:
                raise ValueError('duplicate dataset/augmentation id')
            identities.add(row['id'])
            if data_mode == SYNTHETIC_EXPERIMENT and 'multi_hot' not in row:
                raise ValueError('synthetic label encoding missing')
            if row['input_hash'] != input_hash(row['original_question']):
                raise ValueError('input fingerprint mismatch')
            if row.get('augmentation_parent'):
                if data_mode == SYNTHETIC_EXPERIMENT:
                    raise ValueError('synthetic experiment augmentations are not supported')
                if split != 'train':
                    raise ValueError('augmentation outside train')
                augmentations.append(row)
            else:
                originals.append({k:v for k,v in row.items() if k != 'group_id'})
    # dataset_hash在导出顺序上绑定；原始顺序单独存储以便确定性重建。
    order = dataset.get('original_ids')
    if not order or set(order) != {r['id'] for r in originals}:
        raise ValueError('original dataset order manifest missing')
    by_id = {r['id']:r for r in originals}
    rebuilt = prepare_dataset([by_id[i] for i in order], seed=dataset['seed'], semantic_review=dataset['semantic_review'], data_mode=data_mode)
    if rebuilt['dataset_hash'] != dataset['dataset_hash']:
        raise ValueError('dataset hash mismatch')
    for split in ('train','validation','test'):
        actual = [r for r in dataset['splits'][split] if not r.get('augmentation_parent')]
        if actual != rebuilt['splits'][split]:
            raise ValueError('group split lineage mismatch')
    parents = {r['id']:r for r in rebuilt['splits']['train']}
    for row in augmentations:
        p = parents.get(row['augmentation_parent'])
        if not p or row.get('parent_input_hash') != p['input_hash'] or row.get('parent_split_hash') != rebuilt['split_hashes']['train'] or row['group_id'] != p['group_id']:
            raise ValueError('augmentation train parent fingerprint mismatch')
        if not row.get('human_confirmed') or not row.get('privacy_confirmed') or not row.get('reviewer') or row['labels'] != p['labels']:
            raise ValueError('augmentation human semantic/privacy review required')
        if prepare_input(row['original_question']) != row['original_question'] or row.get('annotation_status') != 'human_confirmed' or row.get('classification_status') != 'predicted':
            raise ValueError('augmentation schema/privacy mismatch')
        if any(row.get(k) != p.get(k) for k in ('source_refs','taxonomy_version','input_version','data_version','semantic_cluster')):
            raise ValueError('augmentation source/schema lineage mismatch')
        for other in rebuilt['splits']['validation'] + rebuilt['splits']['test']:
            if SequenceMatcher(None,row['original_question'],other['original_question']).ratio() >= .9 or row['input_hash'] == other['input_hash'] or (row.get('semantic_cluster') and row.get('semantic_cluster') == other.get('semantic_cluster')):
                raise ValueError('augmentation overlaps held-out data')
