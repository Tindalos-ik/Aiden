import { useMemo, useState } from 'react';
import { Alert, Button, Card, Collapse, Empty, Select, Space, Spin, Table, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, apiMode } from '../api';
import { ragApi } from '../api/rag';
import { PageHeader } from '../components/Common';
import type { RagEvalCase, RagEvalMetrics } from '../types';

const { Text, Title, Paragraph } = Typography;
const strategyNames: Record<string, string> = {
  dense: '纯向量', bm25: '纯 BM25', hybrid: '混合 RRF', hybrid_rerank: '混合 + Rerank',
};
const typeNames: Record<string, string> = {
  exact_model: '具体型号', colloquial: '口语问法', policy_boundary: '政策边界',
  multi_evidence: '多证据', unanswerable: '库外问题',
};
const difficultyNames: Record<string, string> = { easy: '简单', medium: '中等', hard: '困难' };

function rate(value: number | null): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`;
}

function metricColumns() {
  return [
    { title: '策略', dataIndex: 'name', key: 'name', fixed: 'left' as const, width: 150 },
    ...(['recall@1', 'recall@5', 'recall@10', 'mrr', 'faithfulness', 'unanswerable_refusal_rate'] as const).map((key) => ({
      title: key === 'unanswerable_refusal_rate' ? '库外拒答率' : key === 'faithfulness' ? 'Faithfulness' : key.toUpperCase(),
      key, dataIndex: 'metrics', align: 'right' as const, width: 120,
      render: (metrics: RagEvalMetrics) => rate(metrics[key]),
    })),
  ];
}

function StrategyResult({ item }: { item: RagEvalCase }) {
  return <Card size="small" className="eval-strategy-card" title={strategyNames[item.strategy] ?? item.strategy}>
    <Space wrap className="eval-result-tags">
      {item.ground_truth.length > 0 && <Tag color={item['recall@5'] === 1 ? 'green' : 'orange'}>Recall@5 {rate(item['recall@5'])}</Tag>}
      {item.ground_truth.length === 0 && <Tag color={item.refused === null ? 'default' : item.refused === 1 ? 'green' : 'red'}>{item.refused === null ? '拒答未评审' : item.refused === 1 ? '已拒答' : '未拒答'}</Tag>}
      <Tag color={item.faithfulness === null ? 'default' : item.faithfulness === 1 ? 'green' : 'orange'}>Faithfulness {rate(item.faithfulness)}</Tag>
    </Space>
    <Text strong>召回排名</Text>
    <ol className="eval-ranking">{item.retrieved.map((hit) => {
      // 评估脚本用 Path(source_path).name 比较标注文件名，并在完整章节路径中找目标章节。
      const sourceFile = hit.source_path?.split(/[\\/]/).pop();
      const matched = item.ground_truth.some((truth) => truth.source_path === sourceFile && hit.section_path.includes(truth.section));
      return <li key={hit.chunk_id} className={matched ? 'eval-hit' : undefined}>
        <span>{hit.source_path ?? '未知来源'} · {hit.section_path}</span>
        <Space size={4}><Text type="secondary">{hit.score.toFixed(3)}</Text>{matched && <Tag color="green">目标章节</Tag>}</Space>
      </li>;
    })}</ol>
    <Text strong>生成答案</Text>
    <pre className="eval-answer">{item.answer ?? '未生成答案（仅检索评估）'}</pre>
    <Text strong>Faithfulness 评审理由</Text>
    <Paragraph className="eval-judge">{item.judge_reason ?? '未进行 Faithfulness 评审'}</Paragraph>
  </Card>;
}

export function RagEvals() {
  const [questionType, setQuestionType] = useState('all');
  const [difficulty, setDifficulty] = useState('all');
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const report = useQuery({ queryKey: ['rag-eval-report-v1'], queryFn: ragApi.evalReport, enabled: apiMode === 'remote', retry: 0 });

  const questions = useMemo(() => {
    const grouped = new Map<string, RagEvalCase[]>();
    for (const item of report.data?.cases ?? []) {
      const rows = grouped.get(item.id) ?? [];
      rows.push(item);
      grouped.set(item.id, rows);
    }
    return [...grouped.entries()].filter(([, rows]) =>
      (questionType === 'all' || rows[0].type === questionType) &&
      (difficulty === 'all' || rows[0].difficulty === difficulty));
  }, [report.data, questionType, difficulty]);

  if (actor.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;

  const firstStrategy = Object.values(report.data?.strategies ?? {})[0];
  const typeOptions = Object.keys(firstStrategy?.by_type ?? {}).map((value) => ({ value, label: typeNames[value] ?? value }));
  const difficultyOptions = Object.keys(firstStrategy?.by_difficulty ?? {}).map((value) => ({ value, label: difficultyNames[value] ?? value }));

  return <div className="workspace-shell rag-shell">
    <PageHeader actor={actor.data} />
    <main className="rag-main eval-main">
      <div className="rag-heading"><div><Text className="section-kicker">RAG EVALUATION</Text><Title level={2}>客服 RAG 评估</Title><Paragraph type="secondary">逐题核对人工标注、四策略召回与生成答案。显示的是已保存的 v1 评估快照。</Paragraph></div><Space wrap><Link to="/staff/rag">返回建库控制台</Link><Button icon={<ReloadOutlined />} disabled={apiMode !== 'remote'} onClick={() => void report.refetch()}>刷新报告</Button></Space></div>
      {apiMode === 'mock' ? <Alert showIcon type="warning" message="演示模式不提供真实评估报告" description="请切换 VITE_API_MODE=remote 并使用员工账号登录。" /> : <>
        {report.isLoading && <Card><Spin /></Card>}
        {report.isError && <Alert showIcon type="error" message="评估报告加载失败" description={report.error instanceof Error ? report.error.message : '请检查后端服务'} action={<Button size="small" onClick={() => void report.refetch()}>重试</Button>} />}
        {report.data && <>
          <Card className="rag-card" title="总体指标">
            <Paragraph type="secondary">{firstStrategy?.overall.count ?? 0} 道题，其中 {firstStrategy?.overall.answerable ?? 0} 道可回答；Recall 与 MRR 只统计可回答题，Faithfulness 统计全部题目，库外拒答率只统计库外题。评审模型：{report.data.faithfulness_judge ?? '未评审（仅检索）'}；集合：{report.data.collection}。</Paragraph>
            <Table size="small" rowKey="key" pagination={false} scroll={{ x: 900 }} columns={metricColumns()} dataSource={Object.entries(report.data.strategies).map(([key, value]) => ({ key, name: strategyNames[key] ?? key, metrics: value.overall }))} />
          </Card>
          <Card className="rag-card" title="按问题类型对比">
            <Paragraph type="secondary">可回答题显示 Recall@1 / MRR / Faithfulness；库外题显示拒答率 / Faithfulness。分数均来自报告的 by_type 分桶，未按当前筛选重新计算。</Paragraph>
            <Table size="small" rowKey="key" pagination={false} scroll={{ x: 750 }} columns={[
              { title: '题型', dataIndex: 'name', key: 'name', width: 155, fixed: 'left' as const },
              ...Object.keys(report.data.strategies).map((strategy) => ({
                title: strategyNames[strategy] ?? strategy, key: strategy, dataIndex: 'key', width: 145,
                render: (type: string) => {
                  const metrics = report.data.strategies[strategy].by_type[type];
                  return <span className="eval-bucket-value">{metrics.answerable ? <>R@1 {rate(metrics['recall@1'])}<br />MRR {rate(metrics.mrr)}</> : <>拒答 {rate(metrics.unanswerable_refusal_rate)}</>}<br />Faithfulness {rate(metrics.faithfulness)}</span>;
                },
              })),
            ]} dataSource={Object.entries(firstStrategy?.by_type ?? {}).map(([key, metrics]) => ({ key, name: `${typeNames[key] ?? key}（${metrics.count}）` }))} />
          </Card>
          <Card className="rag-card" title={`逐题核对（${questions.length} / ${new Set(report.data.cases.map((item) => item.id)).size}）`} extra={<Space wrap><Select aria-label="按题型筛选" value={questionType} onChange={setQuestionType} style={{ minWidth: 150 }} options={[{ value: 'all', label: '全部类型' }, ...typeOptions]} /><Select aria-label="按难度筛选" value={difficulty} onChange={setDifficulty} style={{ minWidth: 140 }} options={[{ value: 'all', label: '全部难度' }, ...difficultyOptions]} /></Space>}>
            {questions.length === 0 ? <Empty description="没有符合条件的题目" /> : <Collapse items={questions.map(([id, rows]) => ({
              key: id,
              label: <Space wrap><Text strong>{rows[0].query}</Text><Tag>{typeNames[rows[0].type] ?? rows[0].type}</Tag><Tag>{difficultyNames[rows[0].difficulty] ?? rows[0].difficulty}</Tag><Text type="secondary">{id}</Text></Space>,
              children: <>
                <div className="eval-truth"><div><Text strong>Ground truth</Text><div>{rows[0].ground_truth.length ? rows[0].ground_truth.map((truth) => <Tag key={`${truth.source_path}:${truth.section}`}>{truth.source_path} · {truth.section}</Tag>) : <Tag color="orange">库外题，无目标章节</Tag>}</div></div><div><Text strong>答案要点</Text>{rows[0].answer_facts.length ? <ul>{rows[0].answer_facts.map((fact, index) => <li key={index}>{fact}</li>)}</ul> : <Paragraph type="secondary">无预设答案要点</Paragraph>}</div></div>
                <div className="eval-strategies">{rows.map((item) => <StrategyResult key={item.strategy} item={item} />)}</div>
              </>,
            }))} />}
          </Card>
        </>}
      </>}
    </main>
  </div>;
}
