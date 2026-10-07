"""冻结测试集比较原始预测；人工纠错需求单列，绝不用人工最终标签替代预测。"""
from __future__ import annotations
from .data import prepare_input
import time
from .taxonomy import LABEL_IDS, validate_labels


def validate_frozen_evaluation(metadata: dict, dataset: dict) -> None:
    """只接受训练产物绑定的原冻结数据；换seed/heldout后不能宣称独立测试。"""
    if not metadata.get('dataset_hash') or metadata['dataset_hash'] != dataset.get('dataset_hash') or metadata.get('split_hashes') != dataset.get('split_hashes'):
        raise ValueError('encoder evaluation requires original frozen training dataset and split hashes')


def metrics(truth: list[list[str]], predictions: list[list[str]]) -> dict:
    per_class = {}
    for label in LABEL_IDS:
        tp = sum(label in y and label in p for y,p in zip(truth,predictions))
        fp = sum(label not in y and label in p for y,p in zip(truth,predictions))
        fn = sum(label in y and label not in p for y,p in zip(truth,predictions))
        support = tp + fn
        per_class[label] = {'tp':tp,'fp':fp,'fn':fn,'support':support,'evaluable': bool(support), 'precision': tp/(tp+fp) if tp+fp else None, 'recall':tp/support if support else None, 'f1':2*tp/(2*tp+fp+fn) if support else None}
    supported = [x['f1'] for x in per_class.values() if x['evaluable']]
    tp,fp,fn = (sum(x[k] for x in per_class.values()) for k in ('tp','fp','fn'))
    return {'per_class':per_class,'macro_f1_supported_classes':sum(supported)/len(supported) if supported else None,'supported_class_count':len(supported),'unevaluable_classes':[k for k,v in per_class.items() if not v['evaluable']],'micro_f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}


def evaluate(predictor, rows: list[dict], *, split_hash: str, batch_size=32) -> dict:
    if not rows or any(
        x['annotation_status'] != 'human_confirmed'
        or not x.get('privacy_confirmed')
        or not x.get('privacy_reviewer')
        for x in rows
    ):
        raise ValueError('evaluation requires human-confirmed, privacy-reviewed frozen test rows')
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    for row in rows:
        if prepare_input(row['original_question']) != row['original_question']:
            raise ValueError('evaluation text must pass human privacy review before scoring')
    truth = [validate_labels(x['labels']) for x in rows]
    start = time.perf_counter()
    predictions = []
    for offset in range(0,len(rows),batch_size):
        predictions.extend(predictor.predict_batch([x['original_question'] for x in rows[offset:offset+batch_size]]))
    seconds = time.perf_counter()-start
    if len(predictions) != len(rows):
        raise ValueError('predictor result count mismatch')
    labels = [validate_labels(x['predicted_labels']) for x in predictions]
    errors = []
    status_confusion = {}
    confusion_pairs = {}
    for row, y, p, raw in zip(rows,truth,labels,predictions):
        missing = sorted(set(y)-set(p))
        extra = sorted(set(p)-set(y))
        expected = row['classification_status']
        # 编码器只输出uncertain，不能在没有上下文的输入上伪称知道不足原因。
        comparable = 'uncertain' if expected == 'insufficient_context' else expected
        pair = f"{expected}->{raw['status']}"
        status_confusion[pair] = status_confusion.get(pair,0) + 1
        for missed in missing:
            for added in extra:
                key = f'{missed}->{added}'
                confusion_pairs[key] = confusion_pairs.get(key,0)+1
        if missing or extra or raw['status'] != comparable:
            errors.append({'id':row['id'],'input_hash':row['input_hash'],'redacted_text':row['original_question'],'human_truth_labels':y,'expected_status':expected,'raw_prediction':raw,'missing':missing,'extra':extra,'other_error':'other' in missing or 'other' in extra,'uncertain':raw['status']=='uncertain','human_revision_needed':True,'human_revision':None})
    status_report = {'confusion':status_confusion,'label_confusion_pairs':confusion_pairs,'revision_needed_count':len(errors),'revision_needed_rate':len(errors)/len(rows),'completed_human_revisions':0,'insufficient_context_is_uncertain_for_comparison':True}
    return {'model_version':predictor.model_version,'taxonomy_version':predictor.taxonomy_version,'test_split_hash':split_hash,'baseline_configuration':getattr(predictor,'audit_metadata',None),'metrics':metrics(truth,labels),'status_metrics':status_report,'coverage':{'samples':len(rows),'groups':len({x['group_id'] for x in rows}),'multilabel_samples':sum(len(x)>1 for x in truth),'label_support':{k:sum(k in x for x in truth) for k in LABEL_IDS}},'errors':errors,'elapsed_seconds':seconds,'samples_per_second':len(rows)/seconds,'usage':getattr(predictor,'audit',None),'cost':None,'resource_usage':{'device':getattr(predictor,'device',None),'gpu_peak_bytes':predictor.torch.cuda.max_memory_allocated() if hasattr(predictor,'torch') and str(predictor.device).startswith('cuda') else None},'predictions':[{'id':r['id'],**p} for r,p in zip(rows,predictions)]}
