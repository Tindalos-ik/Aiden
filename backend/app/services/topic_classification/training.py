"""真实本地中文RoBERTa全参BCE训练；验证选checkpoint与全局阈值，测试不参与。"""
from __future__ import annotations
import json
import random
from pathlib import Path
from .data import INPUT_VERSION, HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT, fingerprint, validate_dataset
from .taxonomy import LABEL_IDS, TAXONOMY_VERSION, load_taxonomy
from .model import file_hash, select_labels
from .evaluation import metrics


def train(dataset: dict, *, base_dir: str, provenance_path: str, artifact_dir: str, epochs=3, batch_size=16, learning_rate=2e-5, max_length=256, device='cpu', seed=42, data_mode: str = HUMAN_REVIEWED) -> dict:
    """provenance必须人工核查本地来源/许可/revision，绝不下载替代基座。"""
    import math
    if epochs < 1 or batch_size < 1 or max_length < 1 or not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError('positive epochs/batch_size/max_length/learning_rate required')
    validate_dataset(dataset, data_mode=data_mode)
    base = Path(base_dir)
    if not base.is_dir():
        raise ValueError('local pretrained model directory unavailable; download is not authorized')
    provenance = json.loads(Path(provenance_path).read_text(encoding='utf-8'))
    if not all(provenance.get(k) for k in ('model_id','revision','license','source','reviewer','license_verified','chinese_encoder_verified')):
        raise ValueError('verified local Chinese encoder provenance/license required')
    base_files = {p.relative_to(base).as_posix():file_hash(p) for p in base.rglob('*') if p.is_file()}
    if provenance.get('files') != base_files:
        raise ValueError('base provenance content checksum mismatch')
    if any(fingerprint(v) != dataset['split_hashes'][k] for k,v in dataset['splits'].items()):
        raise ValueError('dataset split content hash mismatch')
    if dataset.get('taxonomy_version') != TAXONOMY_VERSION or dataset.get('input_version') != INPUT_VERSION:
        raise ValueError('dataset version mismatch')
    splits = dataset['splits']
    train_rows = [x for x in splits['train'] if x.get('classification_status','predicted') == 'predicted' and x['labels']]
    val_rows = splits['validation']
    val_labeled = [x for x in val_rows if x['classification_status'] == 'predicted']
    if not train_rows or not val_labeled or (data_mode != SYNTHETIC_EXPERIMENT and any(x['annotation_status'] != 'human_confirmed' for x in splits['validation'])):
        raise ValueError('nonempty labeled training and mode-appropriate validation required')
    output = Path(artifact_dir)
    if output.exists():
        raise ValueError('artifact output already exists; refusing overwrite')
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    random.seed(seed); torch.manual_seed(seed)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    # 分类头随机初始化是训练起点，绝不作为已训练predictor交付。
    model, loading = AutoModelForSequenceClassification.from_pretrained(base, local_files_only=True, num_labels=len(LABEL_IDS), problem_type='multi_label_classification', id2label=dict(enumerate(LABEL_IDS)), label2id={v:i for i,v in enumerate(LABEL_IDS)}, output_loading_info=True)
    if any(not any(s in key for s in ('classifier','score')) for key in loading['missing_keys']) or loading.get('mismatched_keys'):
        raise ValueError('base encoder weights incomplete or incompatible')
    model.to(device)
    for parameter in model.parameters():
        parameter.requires_grad_(True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    def targets(rows):
        return torch.tensor([[float(k in r['labels']) for k in LABEL_IDS] for r in rows], device=device)
    y = targets(train_rows)
    positives = y.sum(0)
    pos_weight = ((len(train_rows)-positives)/positives.clamp_min(1)).clamp(min=1,max=20)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    history = []; best_loss = float('inf'); best_epoch = None
    output.mkdir(parents=True)
    for epoch in range(epochs):
        model.train(); order = list(range(len(train_rows))); random.Random(seed+epoch).shuffle(order)
        losses = []
        for offset in range(0,len(order),batch_size):
            rows = [train_rows[i] for i in order[offset:offset+batch_size]]
            inputs = tokenizer([r['original_question'] for r in rows],padding=True,truncation=True,max_length=max_length,return_tensors='pt').to(device)
            optimizer.zero_grad(); loss = loss_fn(model(**inputs).logits, targets(rows)); loss.backward(); optimizer.step(); losses.append(loss.item())
        model.eval(); val_losses = []
        with torch.inference_mode():
            for offset in range(0,len(val_labeled),batch_size):
                rows = val_labeled[offset:offset+batch_size]
                inputs = tokenizer([r['original_question'] for r in rows],padding=True,truncation=True,max_length=max_length,return_tensors='pt').to(device)
                logits = model(**inputs).logits
                val_losses.append((torch.nn.functional.binary_cross_entropy_with_logits(logits,targets(rows),reduction='sum').item(),len(rows)))
        val_loss = sum(x[0] for x in val_losses)/(sum(x[1] for x in val_losses)*len(LABEL_IDS))
        history.append({'epoch':epoch+1,'train_loss':sum(losses)/len(losses),'validation_loss':val_loss})
        if val_loss < best_loss:
            best_loss = val_loss; best_epoch = epoch+1
            model.save_pretrained(output); tokenizer.save_pretrained(output)
    model = AutoModelForSequenceClassification.from_pretrained(output,local_files_only=True).to(device).eval()
    vectors = []
    with torch.inference_mode():
        for offset in range(0,len(val_rows),batch_size):
            inputs = tokenizer([r['original_question'] for r in val_rows[offset:offset+batch_size]],padding=True,truncation=True,max_length=max_length,return_tensors='pt').to(device)
            vectors.extend(model(**inputs).logits.sigmoid().cpu().tolist())
    calibration = []
    for threshold in (.1,.2,.3,.4,.5,.6,.7,.8,.9):
        selected = [select_labels(dict(zip(LABEL_IDS,v)),dict.fromkeys(LABEL_IDS,threshold)) for v in vectors]
        predictions = [x[0] for x in selected]
        score = metrics([r['labels'] for r in val_rows],predictions)['micro_f1']
        status_accuracy = sum(predicted[1] == ('predicted' if r['classification_status']=='predicted' else 'uncertain') for r,predicted in zip(val_rows,selected))/len(val_rows)
        calibration.append({'threshold':threshold,'validation_micro_f1':score,'validation_status_accuracy':status_accuracy})
    chosen = max(calibration,key=lambda r: (r['validation_micro_f1'] if r['validation_micro_f1'] is not None else -1,r['validation_status_accuracy'],-abs(r['threshold']-.5)))['threshold']
    metadata = {'trained':True,'taxonomy':load_taxonomy(),'taxonomy_hash':fingerprint(load_taxonomy()),'taxonomy_version':TAXONOMY_VERSION,'label_ids':list(LABEL_IDS),'input_version':INPUT_VERSION,'thresholds':dict.fromkeys(LABEL_IDS,chosen),'threshold_source':'validation','threshold_mode':'global','calibration':calibration,'base_provenance':provenance,'split_hashes':dataset['split_hashes'],'dataset_hash':dataset['dataset_hash'],'seed':seed,'train_args':{'epochs':epochs,'batch_size':batch_size,'learning_rate':learning_rate,'max_length':max_length,'device':device,'full_parameter':True,'loss':'BCEWithLogitsLoss','pos_weight_train_only':pos_weight.cpu().tolist()},'history':history,'best_epoch':best_epoch,'files':{p.relative_to(output).as_posix():file_hash(p) for p in output.rglob('*') if p.is_file()}}
    import transformers
    metadata['software_versions'] = {'torch':torch.__version__,'transformers':transformers.__version__}
    metadata['data_mode'] = data_mode
    metadata['evaluation_source'] = 'synthetic_model_prelabels' if data_mode == SYNTHETIC_EXPERIMENT else 'human_confirmed'
    metadata['split_manifest'] = {name:[{'id':r['id'],'input_hash':r['input_hash'],'group_id':r['group_id']} for r in rows] for name,rows in splits.items()}
    metadata['dataset_coverage'] = dataset['coverage']
    metadata['checkpoint_selection'] = {'metric':'validation_BCE_loss','direction':'min','best_loss':best_loss,'epoch':best_epoch}
    metadata['model_version'] = fingerprint(metadata)
    (output/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return metadata
