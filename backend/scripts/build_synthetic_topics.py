"""离线重放 Vibe worker 实际创作批次；无外部模型客户端，不伪造人工审核。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.services.topic_classification.data import INPUT_VERSION, dataset_coverage, fingerprint, input_hash, prepare_input
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION, validate_labels

CORE_NAMES={'dataset.json','train.jsonl','validation.jsonl','test.jsonl','provenance.jsonl','ambiguity_challenge.jsonl','rejections.jsonl','disputes.jsonl','scenes.jsonl','scene_split_manifest.json','final_split_manifest.json','semantic_candidates.jsonl','semantic_candidates_manifest.json','prompt_snapshot.json','config_snapshot.json','scenario_exclusions.json','variant_identity_audit.json','blind_review_index.json','style_audit_candidates.json','language_coverage_additions.json','language_coverage_audit.json'}


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def jsonl(path: Path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')


def batches(root: Path, stage: str):
    values=[]
    for path in sorted((root/'raw').glob('*.json')):
        item=json.loads(path.read_text(encoding='utf-8'))
        if item.get('stage')!=stage: continue
        author=item.get('author',{})
        if item.get('exclude_from_final') is True: continue
        if author.get('flavor') not in ('good','fast') or author.get('model') not in ('openai-codex/gpt-6.1-sol:medium','openai-codex/gpt-6-luna:medium'):
            raise ValueError(f'unverified Vibe author metadata: {path.name}')
        if not item.get('batch_id'): raise ValueError('batch_id required')
        values.append((path,item))
    return values


def expression_variants(batch,item):
    """显式追加索引不改变既有原文或身份；单个来源最多五个槽位。"""
    start=item.get('variant_start',batch.get('variant_start',0))
    if not isinstance(start,int) or isinstance(start,bool) or start<0 or start+len(item['variants'])>5:
        raise ValueError('variant start and count must fit the five-sample origin cap')
    return enumerate(item['variants'],start=start)


def captured_reviews(batch):
    rows=batch.get('input_rows')
    if not isinstance(rows,list) or len(rows)!=batch.get('input_rows_count'): raise ValueError('review requires complete captured input rows')
    if batch['author']['flavor']=='fast':
        payload='\n'.join(q['id']+'\t'+q['original_question'] for q in rows)
    else:
        payload=''.join(json.dumps(q,ensure_ascii=False)+'\n' for q in rows)
        if batch.get('input_capture_method')!='captured_original_rows_hash_added_after_review_no_id_text_or_decision_changes':
            payload=payload.replace('\n','\r\n')
    if hashlib.sha256(payload.encode('utf-8')).hexdigest()!=batch.get('input_snapshot_sha256'): raise ValueError('review batch captured input content hash changed')
    captured={q['id']:q for q in rows}
    if len(captured)!=len(rows): raise ValueError('duplicate captured opaque input id')
    return captured


class Groups:
    def __init__(self, ids): self.parent={k:k for k in ids}
    def find(self,k):
        if self.parent[k]!=k: self.parent[k]=self.find(self.parent[k])
        return self.parent[k]
    def union(self,a,b):
        a,b=self.find(a),self.find(b)
        self.parent[max(a,b)]=min(a,b)
    def mapping(self): return {k:self.find(k) for k in self.parent}


def forms(text):
    patterns={'spoken':r'咋|啥|啊|呀|呢|呗|嘛|咱|帮我|怎么','emotional':r'气死|生气|无语|离谱|(?<!麻)烦|服了|崩溃|！|!!','polite':r'请|麻烦|谢谢|您好|劳驾','urgent':r'急|赶|立刻|马上|尽快|来不及','complaint':r'投诉|一直|还没|怎么又|太差|离谱|失望','negation':r'不|没|无|别|并非','turn':r'但是|但|不过|可是|却|结果','conditional':r'如果|要是|假如|若|的话','quotation':r'[“”「」"]'}
    result=[k for k,p in patterns.items() if re.search(p,text)]
    if len(text)<=24: result.append('short')
    if len(text)>=65: result.append('long')
    if not re.search(r'咋|啥|呗|嘛|咱|[“”「」]|(.)\1{2,}',text): result.append('standard')
    if re.search(r'(.)\1{2,}',text): result.append('word_repeat')
    return {'method':'observable_text_heuristics_approximate_overlapping_not_human_truth','forms':result,'length':len(text)}


REQUIRED_LANGUAGE_STYLES={'standard','short','colloquial_short','colloquial_long','long_narrative','emotional','polite','urgent','complaint','natural_typo','word_repetition','word_order_variation','keyword_free_clear','keyword_hard_negative','negation','contrast','conditional','quotation','natural_multi_request'}


def language_examples(root):
    """Only literal model-audited candidates; no automatic inference of typo/order."""
    aliases={'emotion':'emotional','repetition':'word_repetition','word_order_context_fronted':'word_order_variation','word_order_request_led':'word_order_variation','condition':'conditional'}
    result=[]
    for name in ('style_audit_candidates.json','language_coverage_additions.json'):
        artifact=json.loads((root/name).read_text(encoding='utf-8'))
        for example in artifact['style_examples']:
            style=aliases.get(example['style'],example['style'])
            if style not in REQUIRED_LANGUAGE_STYLES: continue
            review=example.get('opposite_review',{})
            if review and (review.get('score',0)<4 or review.get('flags')): continue
            result.append({**example,'style':style,'excerpt':example.get('excerpt',example.get('text')),'audit_source':name})
    return result


def language_coverage(chosen,examples):
    by_id={row['id']:row for row in chosen}; supported=defaultdict(list)
    for example in examples:
        row=by_id.get(example['source_sample_id'])
        if row is None: continue
        excerpt=example['excerpt']
        if not isinstance(excerpt,str) or not excerpt or excerpt not in row['original_question']:
            raise ValueError('language-audit excerpt differs from captured original expression')
        if example.get('input_hash',row['input_hash'])!=row['input_hash']:
            raise ValueError('language-audit input hash differs from captured expression')
        review=row['prelabel_history'][0]
        supported[example['style']].append({'sample_id':row['id'],'input_hash':row['input_hash'],'literal_excerpt':excerpt,'question_length':len(row['original_question']),'reasoning':example['reasoning'],'audit_source':example['audit_source'],'crossworker_review_id':review['review']['review_id'],'crossworker_author':review['author'],'naturalness':review['review']['naturalness'],'flags':review['review']['flags'],'human_reviewed':False})
    approximations=Counter(form for row in chosen for form in forms(row['original_question'])['forms'])
    return {'status':'actual_final_retained_model_examples_not_human_audit','sample_count':len(chosen),'human_reviewed':False,'privacy_confirmed':False,'required_styles':sorted(REQUIRED_LANGUAGE_STYLES),'model_audited_example_counts':{style:len(supported[style]) for style in sorted(REQUIRED_LANGUAGE_STYLES)},'style_examples':dict(supported),'approximate_heuristic_counts':dict(approximations),'heuristic_method':'Regex and character lengths only; approximate and overlapping. Emotional 烦 excludes 麻烦. No typo/order/semantic classifier and no extrapolation from the selected model examples.'}


def scenes(root):
    result=[]; ids=set()
    for path,batch in batches(root,'scenarios'):
        for item in batch['scenarios']:
            if isinstance(item,list):
                item=dict(zip(batch['columns'],item,strict=True))
            if 'labels' not in item:
                item['labels']=batch['labels']
            item.setdefault('problem',item.get('canonical_semantics'))
            item.setdefault('user_request',item.get('canonical_semantics'))
            if not all(item.get(k) for k in ('scenario_id','product','stage','problem','user_request','canonical_semantics')): raise ValueError('incomplete scenario')
            if item['scenario_id'] in ids: raise ValueError('duplicate scenario_id')
            ids.add(item['scenario_id'])
            result.append({**item,'labels':validate_labels(item['labels']),'generation_batch':batch['batch_id'],'author':batch['author']})
    if not result: raise ValueError('no real worker-authored scenario batches')
    return result


def semantic_groups(root,items):
    groups=Groups(s['scenario_id'] for s in items)
    canonical={}
    for s in items:
        text=re.sub(r'\s+','',s['canonical_semantics'])
        if text in canonical: groups.union(s['scenario_id'],canonical[text])
        canonical[text]=s['scenario_id']
    decisions=[]; snapshot=fingerprint(items)
    expected={tuple(sorted((r['a_id'],r['b_id']))) for r in (json.loads(line) for line in (root/'semantic_candidates.jsonl').read_text(encoding='utf-8').splitlines() if line.strip())}
    seen=set()
    for _,batch in batches(root,'semantic_review'):
        if batch.get('candidate_snapshot',{}).get('scenes_hash')!=snapshot:
            continue
        for pair in batch.get('pairs',[]):
            key=tuple(sorted((pair['a_id'],pair['b_id'])))
            if key not in expected or key in seen: raise ValueError('stale or conflicting semantic review tuple')
            seen.add(key)
            if pair.get('duplicate') is True: groups.union(pair['a_id'],pair['b_id'])
            decisions.append(pair)
    if seen!=expected: raise ValueError('all current semantic candidates require literal worker decisions before split')
    return groups,decisions


def allocate(groups,seed):
    mapping=groups.mapping(); keys=sorted(set(mapping.values()))
    random.Random(seed).shuffle(keys); n=len(keys)//10
    assignment={g:'test' if i<n else 'validation' if i<2*n else 'train' for i,g in enumerate(keys)}
    return {k:{'group_id':g,'split':assignment[g]} for k,g in mapping.items()}


def plan(root,seed):
    items=scenes(root); groups,decisions=semantic_groups(root,items)
    allocation=allocate(groups,seed)
    value={'seed':seed,'stage':'before_expression_generation','scenes_hash':fingerprint(items),'origins':allocation,'semantic_review_status':'automated_worker_review_not_human','semantic_decisions':decisions}
    value['manifest_hash']=fingerprint(value)
    path=root/'scene_split_manifest.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=value: raise ValueError('frozen scene plan differs: new version required')
    jsonl(root/'scenes.jsonl',items); save(path,value)
    return value


def semantic_candidates(root):
    """先生成候选供Vibe盲审，再冻结分组；检索不是语义全对保证。"""
    items=scenes(root); inverted=defaultdict(set); index={s['scenario_id']:s for s in items}; result=[]
    for a in items:
        text=a['canonical_semantics']; grams={text[i:i+2] for i in range(len(text)-1)}
        counts=Counter(k for gram in grams for k in inverted[gram])
        for bid in sorted(counts,key=lambda k:counts[k],reverse=True)[:12]:
            b=index[bid]; score=SequenceMatcher(None,text,b['canonical_semantics'],autojunk=False).ratio()
            if score>=.65:
                result.append({'pair_id':f'cp_{len(result):05}','a_id':a['scenario_id'],'b_id':bid,'a':text,'b':b['canonical_semantics'],'lexical_score':score})
        for gram in grams: inverted[gram].add(a['scenario_id'])
    jsonl(root/'semantic_candidates.jsonl',result)
    save(root/'semantic_candidates_manifest.json',{'scenes_hash':fingerprint(items),'candidate_count':len(result),'retrieval':'global canonical bigram inverted retrieval top12; SequenceMatcher>=0.65; not exhaustive semantic all-pairs'})
    return {'candidate_count':len(result),'scene_count':len(items)}


def assemble(root,seed):
    if (root/'dataset.json').exists(): raise ValueError('assembled dataset is immutable; replay into a new output directory')
    config=json.loads((root/'config_snapshot.json').read_text(encoding='utf-8'))
    dataset_version=config['dataset_version']
    items=scenes(root); index={s['scenario_id']:s for s in items}
    frozen=json.loads((root/'scene_split_manifest.json').read_text(encoding='utf-8'))
    if frozen['seed']!=seed or frozen['scenes_hash']!=fingerprint(items): raise ValueError('scene plan changed after expression generation')
    groups,decisions=semantic_groups(root,items)
    exclusions=json.loads((root/'scenario_exclusions.json').read_text(encoding='utf-8')) if (root/'scenario_exclusions.json').exists() else []
    excluded_groups={groups.find(r['scenario_id']) for r in exclusions}
    reviews={}; review_authors={}
    blind_index=json.loads((root/'blind_review_index.json').read_text(encoding='utf-8'))
    for _,batch in batches(root,'reviews'):
        captured=captured_reviews(batch)
        for review in batch['reviews']:
            if isinstance(review,list):
                review=dict(zip(batch['columns'],review,strict=True))
                review.setdefault('status','predicted' if review.get('labels') else 'uncertain')
            opaque=review['id']
            if opaque in batch.get('excluded_review_ids',[]): continue
            if opaque not in blind_index: raise ValueError('review must use opaque handoff identity')
            if opaque not in captured or input_hash(captured[opaque]['original_question'])!=blind_index[opaque]['input_hash']:
                raise ValueError('captured blind review input missing or differs from source hash')
            if batch['author']['flavor']==blind_index[opaque]['author_flavor']:
                raise ValueError('review must be authored by the opposite flavor')
            source_id=blind_index[opaque]['source_sample_id']
            if source_id in reviews: raise ValueError('duplicate review id')
            reviews[source_id]={**review,'review_id':opaque,'id':source_id}
            review_authors[source_id]=batch['author']
    candidates=[]; rejected=[]; disputed=[]; expression_ids=set(); generated=Counter()
    style_examples=language_examples(root)
    style_required_ids={example['source_sample_id'] for example in style_examples}
    for _,batch in batches(root,'expressions'):
        generated[batch['author']['flavor']]+=sum(len(item['variants']) if isinstance(item,dict) else len(dict(zip(batch['columns'],item,strict=True))['variants']) for item in batch['expressions'])
        if batch.get('split_manifest_hash')!=frozen['manifest_hash']: raise ValueError('expression batch must bind frozen pre-expression scene split')
        for item in batch['expressions']:
            if isinstance(item,list):
                item=dict(zip(batch['columns'],item,strict=True))
            scenario=index[item['scenario_id']]
            for j,text in expression_variants(batch,item):
                sid=f'{scenario["scenario_id"]}_v{j}'
                if sid in expression_ids: raise ValueError('duplicate authored sample id')
                expression_ids.add(sid)
                if groups.find(scenario['scenario_id']) in excluded_groups:
                    rejected.append({'id':sid,'scenario_id':scenario['scenario_id'],'reason':'audited_frozen_scene_exclusion'})
                    continue
                row={'id':sid,'original_question':text,'labels':scenario['labels'],'scenario_id':scenario['scenario_id'],'generation_batch':batch['batch_id'],'generation_author':batch['author']}
                opaque=hashlib.sha256(('blind-v2'+sid).encode()).hexdigest()
                if opaque not in blind_index or blind_index[opaque]['input_hash']!=input_hash(text): raise ValueError('expression input hash differs from reviewed handoff identity')
                if not isinstance(text,str) or not text.strip() or prepare_input(text)!=text: rejected.append({'id':sid,'reason':'privacy_or_empty'}); continue
                row['input_hash']=input_hash(text); candidates.append(row)
    # 精确和高阈值词面近重复全对检查，先合并来源再重新切整组。
    match_texts={r['id']:re.sub(r'\s+','',r['original_question']).lower() for r in candidates}
    for i,a in enumerate(candidates):
        ta=match_texts[a['id']]
        for k in range(i):
            b=candidates[k]
            if a['scenario_id']==b['scenario_id']: continue
            tb=match_texts[b['id']]; la,lb=len(ta),len(tb)
            if ta==tb:
                groups.union(a['scenario_id'],b['scenario_id'])
            elif 2*min(la,lb)/(la+lb)>=.9:
                matcher=SequenceMatcher(None,ta,tb)
                if matcher.quick_ratio()>=.9 and matcher.ratio()>=.9:
                    groups.union(a['scenario_id'],b['scenario_id'])
    allocation=allocate(groups,seed)
    kept=[]; counts=Counter(); hashes=set(); normalized_kept=set()
    # Required language examples still pass the same blind gate; prioritize only
    # before the whole-group five-variant cap, never bypass quality or labels.
    candidates.sort(key=lambda row:(row['id'] not in style_required_ids,row['id']))
    for row in candidates:
        d=reviews.get(row['id']); text=row['original_question']
        if not d or review_authors[row['id']]['flavor']==row['generation_author']['flavor']:
            disputed.append({'id':row['id'],'reason':'missing_crossworker_blind_review'}); continue
        try: labels=validate_labels(d.get('labels',[]))
        except (ValueError,TypeError): labels=[]
        evidence=d.get('evidence',{})
        literal=lambda v:isinstance(v,str) and bool(v) and v in text
        if labels!=row['labels'] or d.get('status')!='predicted' or not isinstance(evidence,dict) or any(not literal(evidence.get(label)) for label in row['labels']):
            disputed.append({'id':row['id'],'reason':'labels_status_or_literal_evidence_dispute','target_labels':row['labels'],'review':d}); continue
        if d.get('naturalness',0)<4 or d.get('flags'): rejected.append({'id':row['id'],'reason':'blind_quality_flags','review':d}); continue
        group=allocation[row['scenario_id']]['group_id']
        if counts[group]>=5 or match_texts[row['id']] in normalized_kept: rejected.append({'id':row['id'],'reason':'merged_group_cap_or_exact_duplicate'}); continue
        counts[group]+=1; hashes.add(row['input_hash']); normalized_kept.add(match_texts[row['id']])
        row.update(origin_group_id=group,group_id=group,semantic_cluster=group,taxonomy_version=TAXONOMY_VERSION,input_version=INPUT_VERSION,annotation_status='prelabel',annotation_method='vibe_literal_generation_crossworker_blind_review',annotation_revision='vibe-topics-fixed-v2',annotation_audit=[],human_reviewed=False,privacy_confirmed=False,privacy_reviewer=None,annotator=None,classification_status='predicted',data_version=dataset_version,source='synthetic',source_refs=[{'source':'synthetic','scenario_id':row['scenario_id'],'origin_group_id':group,'batch_id':row['generation_batch']}],prelabel_history=[{'method':'crossworker_blind_review','author':review_authors[row['id']],'review':d,'human_reviewed':False}])
        kept.append(row)
    target={1:3250,2:1500,3:250}; chosen=[]
    for n in (1,2,3):
        bucket=[r for r in kept if len(r['labels'])==n]; random.Random(seed+n).shuffle(bucket)
        mandatory=[row for row in bucket if row['id'] in style_required_ids]; used={row['id'] for row in mandatory}
        if n==1:
            for label in LABEL_IDS:
                seen={row['group_id'] for row in mandatory if row['labels']==[label]}
                for r in bucket:
                    if r['id'] not in used and r['labels']==[label] and r['group_id'] not in seen and len(seen)<100:
                        seen.add(r['group_id']); mandatory.append(r); used.add(r['id'])
        selected=mandatory+[r for r in bucket if r['id'] not in used][:max(0,target[n]-len(mandatory))]
        chosen.extend(selected); selected_ids={r['id'] for r in selected}
        rejected.extend({'id':r['id'],'reason':'oversample_quota'} for r in bucket if r['id'] not in selected_ids)
    chosen.sort(key=lambda r:r['id'])
    splits={k:[r for r in chosen if allocation[r['scenario_id']]['split']==k] for k in ('train','validation','test')}
    originals=[{k:v for k,v in r.items() if k!='group_id'} for r in chosen]; digest=fingerprint(originals)
    data={'taxonomy_version':TAXONOMY_VERSION,'input_version':INPUT_VERSION,'seed':seed,'dataset_hash':digest,'original_ids':[r['id'] for r in chosen],'dedup':{'source_samples':len(candidates),'unique_inputs':len(chosen)},'semantic_review':{'dataset_hash':digest,'confirmed':False,'reviewer':None,'revision':'vibe-topics-fixed-v2'},'splits':splits,'coverage':dataset_coverage(splits),'split_hashes':{k:fingerprint(v) for k,v in splits.items()},'delivery_status':'pending_human_review','human_reviewed':False,'privacy_confirmed':False}
    amb=[]; ambiguity_raw=0
    for _,batch in batches(root,'ambiguity'):
        ambiguity_raw+=len(batch['questions'])
        for item in batch['questions']:
            text=item['original_question']; d=reviews.get(item['id'])
            opaque=hashlib.sha256(('blind-v2'+item['id']).encode()).hexdigest()
            if opaque not in blind_index or blind_index[opaque]['input_hash']!=input_hash(text): raise ValueError('ambiguity input hash differs from reviewed handoff identity')
            if not d:
                disputed.append({'id':item['id'],'reason':'missing_crossworker_blind_review'}); continue
            normalized=re.sub(r'\s+','',text).lower()
            if d.get('labels') or d.get('status') not in ('uncertain','insufficient_context') or normalized in normalized_kept or any(SequenceMatcher(None,normalized,match_texts[r['id']]).ratio()>=.9 for r in chosen): rejected.append({'id':item['id'],'reason':'ambiguity_concrete_or_duplicate'}); continue
            hashes.add(input_hash(text)); normalized_kept.add(normalized); amb.append({**item,'labels':[],'classification_status':d['status'],'input_hash':input_hash(text),'source':'synthetic','human_reviewed':False,'privacy_confirmed':False,'annotation_status':'prelabel','blind_review':d})
    support={label:len({r['group_id'] for r in chosen if label in r['labels']}) for label in LABEL_IDS}
    style_audit=language_coverage(chosen,style_examples)
    missing_styles=sorted(style for style in REQUIRED_LANGUAGE_STYLES if not style_audit['style_examples'].get(style))
    missing_style_samples=sorted(style_required_ids-{row['id'] for row in chosen})
    review_coverage_sha256=hashlib.sha256('\n'.join(sorted(f'{review["review_id"]}\t{blind_index[review["review_id"]]["input_hash"]}\t{review_authors[source_id]["flavor"]}' for source_id,review in reviews.items())).encode('utf-8')).hexdigest()
    complete=len(chosen)==5000 and all(n>=100 for n in support.values()) and not any(r['reason']=='missing_crossworker_blind_review' for r in disputed) and not missing_styles and not missing_style_samples
    if not complete:
        diagnostic={'snapshot_utc':datetime.now(timezone.utc).isoformat(),'publication_status':'provisional_only_no_final_core_written','final_published_count':None,'review_coverage_sha256':review_coverage_sha256,'active_reviewed_unique_ids':len(reviews),'raw_supervised_attempts':sum(generated.values()),'eligible_candidates':len(candidates),'qa_kept':len(kept),'provisional_selected':len(chosen),'independent_groups_per_label':support,'missing_crossworker_reviews':sum(r['reason']=='missing_crossworker_blind_review' for r in disputed),'missing_language_styles':missing_styles,'missing_required_style_samples':missing_style_samples,'disputes_by_reason':dict(Counter(r['reason'] for r in disputed)),'rejections_by_reason':dict(Counter(r['reason'] for r in rejected))}
        save(root/'provisional_diagnostic.json',diagnostic)
        print(json.dumps(diagnostic,ensure_ascii=True))
        return 2
    save(root/'final_split_manifest.json',{'seed':seed,'origins':allocation,'reason':'entire merged semantic origins reassigned after lexical duplicate detection','semantic_decisions':decisions})
    save(root/'dataset.json',data)
    save(root/'language_coverage_audit.json',style_audit)
    for k,v in splits.items(): jsonl(root/f'{k}.jsonl',v)
    jsonl(root/'provenance.jsonl',[{'id':r['id'],'sample_id':r['id'],'source':'synthetic','human_reviewed':False,'privacy_confirmed':False,'split':allocation[r['scenario_id']]['split'],'scenario_id':r['scenario_id'],'origin_group_id':r['group_id'],'group_id':r['group_id'],'generation_batch':r['generation_batch'],'scenario_batch':index[r['scenario_id']]['generation_batch'],'author':r['generation_author'],'product':index[r['scenario_id']]['product'],'language_forms':forms(r['original_question']),'input_hash':r['input_hash']} for r in chosen])
    jsonl(root/'ambiguity_challenge.jsonl',amb); jsonl(root/'rejections.jsonl',rejected); jsonl(root/'disputes.jsonl',disputed)
    core_files=[p for p in root.iterdir() if p.is_file() and p.name in CORE_NAMES]
    core_files.extend(p for p in (root/'raw').glob('*.json') if p.is_file())
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in core_files}
    manifest={'dataset_version':dataset_version,'delivery_status':'pending_human_review','taxonomy_version':TAXONOMY_VERSION,'label_ids':list(LABEL_IDS),'dataset_hash':digest,'split_hashes':data['split_hashes'],'file_sha256':hashes,'sample_count':len(chosen),'label_cardinality':dict(Counter(len(r['labels']) for r in chosen)),'independent_groups_per_label':support,'generated_counts':{'scenes':len(items),'eligible_scenes':sum(groups.find(s['scenario_id']) not in excluded_groups for s in items),'raw_supervised_attempts':sum(generated.values()),'supervised_by_author':dict(generated),'unique_sample_ids':len(expression_ids),'eligible_expressions':len(candidates),'active_crossworker_reviews':len(reviews),'unreviewed_supervised_candidates':sum(r['reason']=='missing_crossworker_blind_review' for r in disputed if r['id'] in expression_ids),'blind_review_retained':len(kept),'final':len(chosen),'ambiguity_raw':ambiguity_raw,'ambiguity_retained':len(amb)},'excluded_by_reason':dict(Counter(r['reason'] for r in rejected)),'unresolved_disputes':len(disputed),'disputes_by_reason':dict(Counter(r['reason'] for r in disputed)),'language_form_counts':dict(Counter(f for r in chosen for f in forms(r['original_question'])['forms'])),'sampled_human_audit_ids':random.Random(seed).sample([r['id'] for r in chosen],min(100,len(chosen))),'human_reviewed':False,'privacy_confirmed':False,'semantic_review_confirmed':False,'seed':seed,'seed_scope':'offline group assignment/quota only; fresh Vibe model outputs not bitwise reproducible','review_limit':'Cross-worker blind automated review, not human truth. Semantic candidate review not exhaustive all-pairs guarantee.','generation_method':'supervised Vibe literal worker tool writes, no provider API','config':config}
    manifest.update(review_coverage_sha256=review_coverage_sha256,language_coverage_audit='language_coverage_audit.json',language_form_count_method='observable regex/length approximations, overlapping; semantic examples audited separately, not human truth')
    save(root/'manifest.json',manifest)
    save(root/'status.json',{'phase':'complete' if complete else 'needs_more_actual_worker_batches','retained':len(chosen),'independent_groups_per_label':support,'split_counts':{k:len(v) for k,v in splits.items()},'external_provider_calls':0,'human_reviewed':False})
    print(json.dumps({'retained':len(chosen),'complete':complete,'groups':support},ensure_ascii=True))
    return 0 if complete else 2


def handoff(root):
    """仅输出原文和稳定ID给另一worker，绝不暴露目标标签、场景或split。"""
    counts={}; private={}
    prior_index=json.loads((root/'blind_review_index.json').read_text(encoding='utf-8')) if (root/'blind_review_index.json').exists() else {}
    covered=set()
    for _,batch in batches(root,'reviews'):
        captured=captured_reviews(batch)
        for review in batch['reviews']:
            if isinstance(review,list): review=dict(zip(batch['columns'],review,strict=True))
            if review['id'] in batch.get('excluded_review_ids',[]): continue
            if review['id'] not in captured: raise ValueError('review decision lacks captured input')
            if review['id'] in prior_index and input_hash(captured[review['id']]['original_question'])!=prior_index[review['id']]['input_hash']: raise ValueError('active reviewed input hash differs from prior handoff')
            if review['id'] in covered: raise ValueError('duplicate active opaque review identity')
            covered.add(review['id'])
    for flavor in ('good','fast'):
        rows=[]
        for _,batch in batches(root,'expressions'):
            if batch['author']['flavor']!=flavor: continue
            for item in batch['expressions']:
                if isinstance(item,list): item=dict(zip(batch['columns'],item,strict=True))
                for j,text in expression_variants(batch,item):
                    source=f'{item["scenario_id"]}_v{j}'
                    opaque=hashlib.sha256(('blind-v2'+source).encode()).hexdigest()
                    if opaque in private: raise ValueError('duplicate authored source identity in handoff')
                    if opaque in covered and opaque in prior_index and prior_index[opaque]['input_hash']!=input_hash(text): raise ValueError('previously reviewed source text changed')
                    private[opaque]={'source_sample_id':source,'author_flavor':flavor,'input_hash':input_hash(text)}
                    rows.append({'id':opaque,'original_question':text})
        for _,batch in batches(root,'ambiguity'):
            if batch['author']['flavor']!=flavor: continue
            for item in batch['questions']:
                opaque=hashlib.sha256(('blind-v2'+item['id']).encode()).hexdigest()
                if opaque in private: raise ValueError('duplicate authored ambiguity identity in handoff')
                if opaque in prior_index and prior_index[opaque]['input_hash']!=input_hash(item['original_question']): raise ValueError('previously handed-off ambiguity text changed')
                private[opaque]={'source_sample_id':item['id'],'author_flavor':flavor,'input_hash':input_hash(item['original_question'])}
                rows.append({'id':opaque,'original_question':item['original_question']})
        rows.sort(key=lambda r:r['id'])
        jsonl(root/'blind_handoff'/f'{flavor}_texts.jsonl',rows)
        pending=[r for r in rows if r['id'] not in covered]
        jsonl(root/'blind_handoff'/f'{flavor}_unreviewed.jsonl',pending)
        save(root/'blind_handoff'/f'{flavor}_unreviewed_manifest.json',{'input_sha256':hashlib.sha256((root/'blind_handoff'/f'{flavor}_unreviewed.jsonl').read_bytes()).hexdigest(),'rows':len(pending),'review_ids_sha256':fingerprint([r['id'] for r in pending]),'review_payload_contract':'opaque identity and original question only; set difference by reviewed identities'})
        counts[flavor]=len(rows)
    save(root/'blind_review_index.json',private)
    save(root/'prompt_snapshot.json',json.loads((Path(__file__).with_name('topic_synthetic_v2')/'prompts.json').read_text(encoding='utf-8')))
    print(json.dumps(counts))


def replay(source,output,seed):
    if source is None or output is None: raise ValueError('replay requires --source and a new --output directory')
    source=source.resolve(); output=output.resolve()
    if output.exists(): raise ValueError('replay output must not exist; user annotations and published data are never overwritten')
    if output==source or source in output.parents: raise ValueError('replay output must be outside source dataset directory')
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    config=json.loads((source/'config_snapshot.json').read_text(encoding='utf-8'))
    if manifest['dataset_version']!=config['dataset_version']: raise ValueError('source dataset version differs from captured config')
    for name,expected in manifest['file_sha256'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected: raise ValueError(f'captured core hash changed: {name}')
    output.mkdir(parents=True)
    inputs=CORE_NAMES-{'dataset.json','train.jsonl','validation.jsonl','test.jsonl','provenance.jsonl','ambiguity_challenge.jsonl','rejections.jsonl','disputes.jsonl','final_split_manifest.json','language_coverage_audit.json'}
    for name in sorted(inputs):
        if (source/name).is_file(): shutil.copy2(source/name,output/name)
    (output/'raw').mkdir()
    for name in sorted(manifest['file_sha256']):
        if name.startswith('raw/') and Path(name).name==name.removeprefix('raw/'):
            shutil.copy2(source/name,output/name)
    return assemble(output,seed)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['candidates','plan','handoff','assemble','replay'])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--seed',type=int,default=20261007)
    args=parser.parse_args()
    if args.phase=='replay': return replay(args.source,args.output,args.seed)
    if args.source is not None: raise ValueError('--source is exclusive to replay')
    if args.output is None: args.output=BACKEND/'datasets'/'topic_synthetic_v2_vibe_20261007'
    if args.phase=='handoff': handoff(args.output); return 0
    if args.phase=='candidates':
        print(json.dumps(semantic_candidates(args.output))); return 0
    if args.phase=='plan': plan(args.output,args.seed); return 0
    return assemble(args.output,args.seed)

if __name__=='__main__':
    try: raise SystemExit(main())
    except (ValueError,KeyError,FileNotFoundError) as exc:
        print(f'offline contract refused: {exc}',file=sys.stderr); raise SystemExit(2)
