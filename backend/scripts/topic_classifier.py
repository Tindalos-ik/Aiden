"""旁路分类CLI。文件均独占新建；导出默认最多20条待人工确认，不日志输出原话。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from app.config import settings as _settings  # noqa: F401 沿用脚本惯例读取 backend/.env，不启动在线服务
from app.services.topic_classification.data import read_jsonl, write_new_json, prepare_dataset, prepare_synthetic_dataset, attach_augmentations, fingerprint, INPUT_VERSION, validate_dataset, HUMAN_REVIEWED, SYNTHETIC_EXPERIMENT
from app.services.topic_classification.taxonomy import TAXONOMY_VERSION
from datetime import datetime, timezone


def utc_datetime(value):
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError('ISO8601 UTC offset required')
    return result.astimezone(timezone.utc).replace(tzinfo=None)


def source_cursor(value):
    try:
        timestamp, source_id = value.rsplit(',',1)
        from uuid import UUID
        UUID(source_id)
        return utc_datetime(timestamp), source_id
    except ValueError as exc:
        raise argparse.ArgumentTypeError('cursor must be ISO8601_DATETIME,UUID') from exc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command',required=True)
    export = sub.add_parser('export'); export.add_argument('--output',required=True); export.add_argument('--limit',type=int,default=20)
    prepare = sub.add_parser('prepare'); prepare.add_argument('--input',required=True); prepare.add_argument('--semantic-review',required=True); prepare.add_argument('--output',required=True); prepare.add_argument('--seed',type=int,default=42)
    synthetic = sub.add_parser('prepare-synthetic', help='显式重建合成预标签实验数据；不授予人工审核或生产准入')
    synthetic.add_argument('--input', nargs='+', required=True)
    synthetic.add_argument('--output-dir', required=True)
    synthetic.add_argument('--seed', type=int, default=42)
    augment = sub.add_parser('augment'); augment.add_argument('--dataset',required=True); augment.add_argument('--input',required=True); augment.add_argument('--output',required=True)
    tr = sub.add_parser('train'); tr.add_argument('--dataset',required=True); tr.add_argument('--base-dir',required=True); tr.add_argument('--provenance',required=True); tr.add_argument('--artifact-dir',required=True); tr.add_argument('--device',default='cpu'); tr.add_argument('--epochs',type=int,default=3); tr.add_argument('--batch-size',type=int,default=16)
    tr.add_argument('--learning-rate',type=float,default=2e-5); tr.add_argument('--max-length',type=int,default=256); tr.add_argument('--seed',type=int,default=42)
    predict = sub.add_parser('predict'); predict.add_argument('--artifact-dir',required=True); predict.add_argument('--input',required=True); predict.add_argument('--output',required=True); predict.add_argument('--device',default='cpu')
    ev = sub.add_parser('evaluate'); ev.add_argument('--dataset',required=True); ev.add_argument('--output',required=True); ev.add_argument('--artifact-dir'); ev.add_argument('--device',default='cpu'); ev.add_argument('--llm-model'); ev.add_argument('--llm-base-url'); ev.add_argument('--allow-paid',action='store_true')
    for command in (tr, predict, ev):
        command.add_argument('--synthetic-experiment', action='store_true', help='显式允许合成实验数据/模型，非人工真值或生产验收')
    batch = sub.add_parser('batch'); batch.add_argument('--artifact-dir',required=True); batch.add_argument('--device',default='cpu'); batch.add_argument('--limit',type=int,default=20); batch.add_argument('--batch-size',type=int,default=16)
    stats = sub.add_parser('stats'); stats.add_argument('--model-version',required=True)
    review = sub.add_parser('review'); review.add_argument('--source-id',required=True); review.add_argument('--input-hash',required=True); review.add_argument('--staff-id',required=True); review.add_argument('--labels',nargs='*',default=[]); review.add_argument('--status',choices=['confirmed','uncertain','insufficient_context'],required=True); review.add_argument('--note')
    for command in (export,batch,stats):
        command.add_argument('--start-at',type=utc_datetime); command.add_argument('--end-at',type=utc_datetime)
    for command in (export,batch):
        command.add_argument('--after',type=source_cursor,help='ISO8601_DATETIME,UUID')
    args = p.parse_args(argv)
    def load(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))
    data_mode = SYNTHETIC_EXPERIMENT if getattr(args, 'synthetic_experiment', False) else HUMAN_REVIEWED
    try:
        if args.command == 'export':
            from app.services.topic_classification.batch import export_source_batch
            if not 1 <= args.limit <= 100:
                raise ValueError('export limit must be 1..100; use explicit bounded batches')
            rows = export_source_batch(start_at=args.start_at,end_at=args.end_at,after=args.after,limit=args.limit)
            for row in rows:
                stamp = row['created_at']
                stamp = stamp if isinstance(stamp,datetime) else datetime.fromisoformat(stamp.replace('Z','+00:00'))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                row['created_at'] = stamp.isoformat(timespec='microseconds').replace('+00:00','Z')
            data_version = fingerprint([{k:v for k,v in row.items() if k != 'text'} for row in rows])
            records = [{'id':r['source_id'],'original_question':r['text'],'input_hash':r['input_hash'],'source_refs':[{'source':'low_confidence_questions','source_id':r['source_id'],'conversation_id':r['conversation_id'],'source_user_message_id':r['user_message_id'],'matched_review_id':r['matched_review_id'],'created_at':str(r['created_at'])}],'data_version':data_version,'taxonomy_version':TAXONOMY_VERSION,'input_version':INPUT_VERSION,'labels':[],'classification_status':'uncertain','annotation_status':'prelabel','annotation_method':'unlabeled_export','annotation_revision':'export-v1','annotation_audit':[],'privacy_confirmed':False,'privacy_reviewer':None,'semantic_cluster':None} for r in rows]
            with Path(args.output).open('x',encoding='utf-8') as stream:
                for record in records:
                    stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            result = {'exported':len(records),'output':args.output,'data_version':data_version,'human_privacy_and_annotation_required':True}
        elif args.command == 'prepare':
            result = prepare_dataset(read_jsonl(args.input),seed=args.seed,semantic_review=load(args.semantic_review)); write_new_json(args.output,result); result = {'output':args.output,'split_hashes':result['split_hashes'],'coverage':result['coverage'],'dedup':result['dedup']}
        elif args.command == 'prepare-synthetic':
            from app.services.topic_classification.model import file_hash
            directory = Path(args.output_dir)
            if directory.exists():
                raise ValueError('synthetic output directory already exists; refusing overwrite')
            rows = [row for path in args.input for row in read_jsonl(path)]
            dataset = prepare_synthetic_dataset(rows, seed=args.seed)
            validate_dataset(dataset, data_mode=SYNTHETIC_EXPERIMENT)
            directory.mkdir(parents=True)
            write_new_json(directory / 'dataset.json', dataset)
            for split, records in dataset['splits'].items():
                with (directory / f'{split}.jsonl').open('x', encoding='utf-8') as stream:
                    for row in records:
                        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            groups = {name:[{'id':r['id'], 'input_hash':r['input_hash'], 'group_id':r['group_id']} for r in records] for name,records in dataset['splits'].items()}
            write_new_json(directory / 'group_manifest.json', groups)
            write_new_json(directory / 'statistics.json', {'data_mode':SYNTHETIC_EXPERIMENT, 'evaluation_source':dataset['evaluation_source'], 'coverage':dataset['coverage'], 'dedup':dataset['dedup']})
            manifest = {'data_mode':SYNTHETIC_EXPERIMENT, 'evaluation_source':dataset['evaluation_source'], 'dataset_hash':dataset['dataset_hash'], 'split_hashes':dataset['split_hashes'], 'label_ids':dataset['label_ids'], 'source_files':[{'path':str(path), 'sha256':file_hash(Path(path))} for path in args.input], 'files':{p.name:file_hash(p) for p in directory.iterdir() if p.is_file()}}
            write_new_json(directory / 'manifest.json', manifest)
            result = {'output_dir':str(directory), 'data_mode':SYNTHETIC_EXPERIMENT, 'dataset_hash':dataset['dataset_hash'], 'coverage':dataset['coverage']}
        elif args.command == 'augment':
            result = attach_augmentations(load(args.dataset),read_jsonl(args.input)); write_new_json(args.output,result); result = {'output':args.output,'split_hashes':result['split_hashes']}
        elif args.command == 'train':
            from app.services.topic_classification.training import train
            m = train(load(args.dataset),base_dir=args.base_dir,provenance_path=args.provenance,artifact_dir=args.artifact_dir,device=args.device,epochs=args.epochs,batch_size=args.batch_size,learning_rate=args.learning_rate,max_length=args.max_length,seed=args.seed,data_mode=data_mode); result = {'model_version':m['model_version'],'artifact_dir':args.artifact_dir,'data_mode':data_mode}
        elif args.command == 'predict':
            from app.services.topic_classification.model import EncoderPredictor
            model = EncoderPredictor(args.artifact_dir,args.device,data_mode=data_mode); rows = read_jsonl(args.input)
            predictions = model.predict_batch([r['original_question'] for r in rows]); write_new_json(args.output,{'model_version':model.model_version,'taxonomy_version':model.taxonomy_version,'data_mode':data_mode,'evaluation_source':model.metadata.get('evaluation_source','human_confirmed'),'predictions':[{'id':r['id'],**v} for r,v in zip(rows,predictions)]}); result = {'predicted':len(rows),'output':args.output}
        elif args.command == 'evaluate':
            from app.services.topic_classification.baselines import RulePredictor, LLMPredictor
            from app.services.topic_classification.model import EncoderPredictor, file_hash
            from app.services.topic_classification.evaluation import evaluate, validate_frozen_evaluation
            dataset = load(args.dataset)
            validate_dataset(dataset, data_mode=data_mode)
            predictors = {'rules':RulePredictor()}
            if args.artifact_dir:
                predictors['encoder'] = EncoderPredictor(args.artifact_dir,args.device,data_mode=data_mode)
                validate_frozen_evaluation(predictors['encoder'].metadata,dataset)
            if args.llm_model:
                predictors['llm'] = LLMPredictor(model=args.llm_model,base_url=args.llm_base_url,api_key=os.environ.get('TOPIC_LLM_API_KEY'),allow_paid=args.allow_paid)
            reports = {k:evaluate(v,dataset['splits']['test'],split_hash=dataset['split_hashes']['test'],data_mode=data_mode) for k,v in predictors.items()}
            # 比较报告也自带原冻结证据；控制台刷新不依赖当前注册数据集。
            for report in reports.values():
                report.update(dataset_hash=dataset['dataset_hash'], split_hashes=dataset['split_hashes'])
            if 'encoder' in reports:
                reports['encoder'].update(model_metadata=predictors['encoder'].metadata,
                    artifact_identity=file_hash(Path(args.artifact_dir) / 'metadata.json'),
                    dataset_identity=file_hash(Path(args.dataset)))
            result = {'comparison_complete':set(reports)=={'rules','encoder','llm'},'missing_baselines':sorted({'rules','encoder','llm'}-set(reports)),'dataset_hash':dataset['dataset_hash'],'test_split_hash':dataset['split_hashes']['test'],'selection':{'checkpoint':predictors['encoder'].metadata['checkpoint_selection'] if 'encoder' in predictors else None,'threshold_source':'validation' if 'encoder' in predictors else None,'test_used_for_tuning':False},'reports':reports}
            result.update(data_mode=data_mode, evaluation_source=dataset.get('evaluation_source','human_confirmed'),
                          dataset=dataset, split_hashes=dataset['split_hashes'])
            write_new_json(args.output,result); result = {'output':args.output,'baselines':list(reports),'comparison_complete':result['comparison_complete']}
        elif args.command == 'batch':
            from app.services.topic_classification.batch import run_batch
            # 此非注册授权入口始终只允许人工审核产物。
            result = run_batch(artifact_dir=args.artifact_dir,device=args.device,start_at=args.start_at,end_at=args.end_at,after=args.after,limit=args.limit,batch_size=args.batch_size,data_mode=HUMAN_REVIEWED)
        elif args.command == 'stats':
            from app.services.topic_classification.batch import statistics
            result = statistics(model_version=args.model_version,start_at=args.start_at,end_at=args.end_at)
        else:
            from app.services.topic_classification.batch import record_human_review
            result = record_human_review(source_id=args.source_id,input_hash=args.input_hash,staff_id=args.staff_id,labels=args.labels,status=args.status,note=args.note)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str)); return 0
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False)); return 2


if __name__ == '__main__':
    raise SystemExit(main())
