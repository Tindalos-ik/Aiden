"""旁路分类的唯一标签定义；顺序同时约束训练头与持久化分数。"""
from __future__ import annotations
import json
from pathlib import Path



def load_taxonomy() -> dict:
    payload = json.loads(Path(__file__).with_name('taxonomy.json').read_text(encoding='utf-8'))
    ids = [x['id'] for x in payload['labels']]
    if len(ids) != 17 or len(set(ids)) != 17 or 'other' not in ids or not payload.get('version'):
        raise ValueError('invalid 17-label taxonomy')
    if any(not all(x.get(k) for k in ('id', 'display', 'include', 'exclude')) for x in payload['labels']):
        raise ValueError('taxonomy boundaries required')
    return payload


_TAXONOMY = load_taxonomy()
TAXONOMY_VERSION = _TAXONOMY['version']
LABEL_IDS = tuple(x['id'] for x in _TAXONOMY['labels'])


def validate_labels(labels) -> list[str]:
    values = list(labels)
    if len(values) != len(set(values)) or any(x not in LABEL_IDS for x in values):
        raise ValueError('unknown or duplicate topic label')
    if 'other' in values and len(values) != 1:
        raise ValueError('other is exclusive with concrete topics')
    return [x for x in LABEL_IDS if x in values]
