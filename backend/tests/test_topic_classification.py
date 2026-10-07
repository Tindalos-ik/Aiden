"""受控原话仅验证边界，不代表分类质量或真实训练验收。"""
import unittest
import json
import tempfile
from pathlib import Path
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION, validate_labels, load_taxonomy
from app.services.topic_classification.data import prepare_input, input_hash, INPUT_VERSION, fingerprint
from app.services.topic_classification.baselines import RulePredictor, LLMPredictor
from app.services.topic_classification.model import select_labels, EncoderPredictor
from app.services.topic_classification.evaluation import metrics


class TopicBehaviorTests(unittest.TestCase):
    def test_other_exclusive_with_concrete_topic(self):
        with self.assertRaises(ValueError): validate_labels(['other','quality'])
    def test_raw_question_privacy_preserves_requests(self):
        text = prepare_input('姓名：张三，账号：abc123，请改地址，鞋坏了要退款')
        self.assertNotIn('张三',text); self.assertNotIn('abc123',text)
        self.assertIn('改地址',text); self.assertIn('坏了要退款',text)
        self.assertEqual(input_hash(text),input_hash('姓名：张三，账号：abc123，请改地址，鞋坏了要退款'))
        self.assertNotEqual(input_hash('我要退款'),input_hash('我要开发票'))
    def test_no_inferred_return(self):
        rows = RulePredictor().predict_batch(['鞋坏了','买大了想换小一号','嗯？'])
        self.assertEqual(rows[0]['predicted_labels'],['quality'])
        self.assertEqual(rows[1]['predicted_labels'],['returns_exchange','size'])
        self.assertEqual(rows[2]['status'],'uncertain'); self.assertEqual(rows[2]['predicted_labels'],[])
    def test_other_conflict_uncertain(self):
        scores = dict.fromkeys(LABEL_IDS,0.0); scores.update(other=.9,quality=.9)
        self.assertEqual(select_labels(scores,dict.fromkeys(LABEL_IDS,.5)),([], 'uncertain'))
    def test_unavailable_and_paid_hard_fail(self):
        with self.assertRaises(FileNotFoundError): EncoderPredictor('.tmp/nonexistent-topic-artifact')
        with self.assertRaises(ValueError): LLMPredictor(model='x',base_url='x',api_key='x')
    def test_zero_support_unevaluable(self):
        report = metrics([['quality']],[['quality']])
        self.assertIsNone(report['per_class']['invoice']['f1'])
        self.assertEqual(report['supported_class_count'],1)
    def test_artifact_label_order_and_threshold_hard_fail(self):
        metadata = {'taxonomy':load_taxonomy(),'taxonomy_hash':fingerprint(load_taxonomy()),'taxonomy_version':TAXONOMY_VERSION,'label_ids':list(LABEL_IDS),'input_version':INPUT_VERSION,'trained':True,'thresholds':dict.fromkeys(LABEL_IDS,.5)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'metadata.json'
            metadata['label_ids'] = list(reversed(LABEL_IDS))
            path.write_text(json.dumps(metadata),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'order'): EncoderPredictor(tmp)
            metadata['label_ids'] = list(LABEL_IDS)
            metadata['thresholds']['quality'] = float('nan')
            path.write_text(json.dumps(metadata),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'threshold'): EncoderPredictor(tmp)
    def test_exact_dedup_group_and_manifest_tampering(self):
        from app.services.topic_classification.data import prepare_dataset, validate_dataset, attach_augmentations
        texts = ['鞋坏了','鞋坏了','想开电子发票','请问还剩几双库存','我忘记登录密码了']
        tags = ['quality','quality','invoice','stock','account']
        rows = [{'id':str(i),'original_question':text,'source_refs':[{'conversation_id':str(i)}],'privacy_confirmed':True,'privacy_reviewer':'fixture','taxonomy_version':TAXONOMY_VERSION,'input_version':INPUT_VERSION,'labels':[tags[i]],'classification_status':'predicted','annotation_status':'human_confirmed','annotation_revision':'fixture-v1','annotator':'fixture','annotation_method':'fixture','annotation_audit':[],'data_version':'fixture-only'} for i,text in enumerate(texts)]
        with self.assertRaisesRegex(ValueError,'semantic review') as error:
            prepare_dataset(rows,semantic_review={})
        digest = str(error.exception).split('dataset_hash=')[1]
        dataset = prepare_dataset(rows,semantic_review={'dataset_hash':digest,'confirmed':True,'reviewer':'fixture-only','revision':'fixture'})
        validate_dataset(dataset)
        self.assertEqual(dataset['dedup'],{'source_samples':5,'unique_inputs':4})
        parent = dataset['splits']['train'][0]
        heldout = dataset['splits']['test'][0]
        augmented = {'id':'augmentation','original_question':'商品已经损坏了','labels':parent['labels'],'augmentation_parent':heldout['id'],'parent_input_hash':heldout['input_hash'],'parent_split_hash':dataset['split_hashes']['train'],'privacy_confirmed':True,'human_confirmed':True,'reviewer':'fixture'}
        with self.assertRaisesRegex(ValueError,'train parent'): attach_augmentations(dataset,[augmented])
        augmented.update(augmentation_parent=parent['id'],parent_input_hash=parent['input_hash'],original_question=heldout['original_question'])
        with self.assertRaisesRegex(ValueError,'held-out'): attach_augmentations(dataset,[augmented])
        sample = dataset['splits']['test'][0]
        sample['group_id'] = 'tampered'
        dataset['split_hashes']['test'] = fingerprint(dataset['splits']['test'])
        with self.assertRaisesRegex(ValueError,'lineage'): validate_dataset(dataset)
    def test_encoder_rejects_reseeded_heldout(self):
        from app.services.topic_classification.evaluation import validate_frozen_evaluation
        metadata = {'dataset_hash':'same-original-questions','split_hashes':{'train':'train-seed42','validation':'val-seed42','test':'test-seed42'}}
        dataset = {'dataset_hash':'same-original-questions','split_hashes':dict(metadata['split_hashes'])}
        validate_frozen_evaluation(metadata,dataset)
        # 原问题仍相同，但重切分可把训练样本放进test，必须拒绝评价。
        dataset['split_hashes'].update(train='train-seed43',test='test-seed43')
        with self.assertRaisesRegex(ValueError,'original frozen'): validate_frozen_evaluation(metadata,dataset)


if __name__ == '__main__': unittest.main()
