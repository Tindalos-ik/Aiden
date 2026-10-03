import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Collapse, Empty, Select, Space, Spin, Table, Tabs, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, apiMode } from '../api';
import { ragApi } from '../api/rag';
import { PageHeader } from '../components/Common';
import type { RagEvalCase, RagEvalMetrics, RagEvalReport } from '../types';

const { Text, Title, Paragraph } = Typography;
type EvalMode = 'offline' | 'online';
type MetricKey = 'recall@1' | 'recall@5' | 'recall@10' | 'mrr' | 'faithfulness' | 'completeness' | 'false_refusal' | 'unsafe_answer' | 'unanswerable_refusal_rate' | 'latency_ms' | 'intent_correct' | 'tool_correct' | 'refund_correct' | 'handoff_correct' | 'blocked_tool_correct';
const strategyNames: Record<string, string> = {
  dense: '纯向量', bm25: '纯 BM25', hybrid: '混合 RRF', hybrid_rerank: '混合 + Rerank', online: '在线客服图',
};
const typeNames: Record<string, string> = {
  exact_model: '具体型号', colloquial: '口语问法', policy_boundary: '政策边界', negative_condition: '否定条件',
  version_conflict: '来源/版本冲突', multi_evidence: '多证据', unanswerable: '库外问题',
};
const difficultyNames: Record<string, string> = { easy: '简单', medium: '中等', hard: '困难' };
const tokenText = (value?: { input: number | null; output: number | null; total: number | null }) =>
  value ? `输入 ${value.input ?? '未知'} / 输出 ${value.output ?? '未知'} / 合计 ${value.total ?? '未知'}` : '未记录';
function rate(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`;
}
function metricColumns(online: boolean) {
  const fields: MetricKey[] = [
    'recall@1', 'recall@5', 'recall@10', 'mrr', 'faithfulness', 'completeness', 'false_refusal',
    'unsafe_answer', 'unanswerable_refusal_rate', 'latency_ms',
  ];
  if (online) fields.push('intent_correct', 'tool_correct', 'refund_correct', 'handoff_correct', 'blocked_tool_correct');
  const titles: Record<string, string> = {
    'recall@1': 'Recall@1', 'recall@5': 'Recall@5', 'recall@10': 'Recall@10', mrr: 'MRR',
    faithfulness: 'Faithfulness', completeness: '完整性', false_refusal: '误拒答',
    unsafe_answer: '应拒未拒', unanswerable_refusal_rate: '应拒答率', latency_ms: '延迟 ms',
    intent_correct: '意图正确', tool_correct: '工具正确', refund_correct: '售后阶段正确',
    handoff_correct: '人工转接正确', blocked_tool_correct: '写工具拦截正确',
  };
  return [
    { title: '策略', dataIndex: 'name', key: 'name', fixed: 'left' as const, width: 140 },
    ...fields.map((key) => ({
      title: titles[key], key, dataIndex: 'metrics', align: 'right' as const, width: 115,
      render: (metrics: RagEvalMetrics) => key === 'latency_ms'
        ? (metrics[key] == null ? '—' : `${metrics[key]!.toFixed(0)} ms`) : rate(metrics[key]),
    })),
  ];
}
function StrategyResult({ item }: { item: RagEvalCase }) {
  const refusalMatches = item.expected_refusal == null ? null : Number(Boolean(item.refused)) === Number(item.expected_refusal);
  return <Card size="small" className="eval-strategy-card" title={strategyNames[item.strategy] ?? item.strategy}>
    <Space wrap className="eval-result-tags">
      {item.split && <Tag>{item.split === 'validation' ? '验证集' : '留出集'}</Tag>}
      {item.expected_refusal == null
        ? <Tag>历史题：未记录应拒答标注</Tag>
        : <Tag color={item.expected_refusal ? 'orange' : 'blue'}>{item.expected_refusal ? '预期拒答' : '预期可答/可说明'}</Tag>}
      {item.refused != null && <Tag color={refusalMatches == null ? 'default' : refusalMatches ? 'green' : 'red'}>{item.refused === 1 ? '实际拒答' : '实际作答'}</Tag>}
      {item.faithfulness != null && <Tag color={item.faithfulness === 1 ? 'green' : 'orange'}>Faithfulness {rate(item.faithfulness)}</Tag>}
      {item.completeness != null && <Tag>完整性 {rate(item.completeness)}</Tag>}
      {item.false_refusal != null && <Tag color={item.false_refusal ? 'red' : 'green'}>误拒答 {rate(item.false_refusal)}</Tag>}
      {item.unsafe_answer != null && <Tag color={item.unsafe_answer ? 'red' : 'green'}>应拒未拒 {rate(item.unsafe_answer)}</Tag>}
      {item.latency_ms != null && <Tag>{item.latency_ms.toFixed(0)} ms</Tag>}
    </Space>
    {item.strategy !== 'online' && <>
      <Text strong>召回排名</Text>
      <ol className="eval-ranking">{item.retrieved.map((hit, index) => {
        const sourceFile = hit.source_path?.split(/[\\/]/).pop();
        const matched = item.ground_truth.some((truth) => truth.source_path === sourceFile && hit.section_path.includes(truth.section));
        return <li key={hit.chunk_id ?? `${hit.source_path}:${hit.section_path}:${index}`} className={matched ? 'eval-hit' : undefined}>
          <span>{hit.source_path ?? '未知来源'} · {hit.section_path}</span>
          <Space size={4}><Text type="secondary">{hit.score == null ? '—' : hit.score.toFixed(3)}</Text>{matched && <Tag color="green">目标章节</Tag>}</Space>
        </li>;
      })}</ol>
    </>}
    <Text strong>生成/工作流结果</Text>
    <pre className="eval-answer">{item.answer ?? '未生成答案'}</pre>
    {item.strategy === 'online' && <>
      <Paragraph>预期意图：{item.workflow?.expected_intents?.join('、') || '—'} / 实际：{item.actual_intents?.join('、') || '—'}；预期工具：{item.workflow?.expected_tools?.join('、') || '—'} / 实际：{item.actual_tools?.join('、') || '无'}；预期退款阶段：{item.workflow?.expected_refund_stage ?? '—'} / 实际：{item.refund_stage ?? '—'}；预期转人工：{item.workflow?.expected_handoff == null ? '—' : item.workflow.expected_handoff ? '是' : '否'} / 实际：{item.handoff == null ? '—' : item.handoff ? '是' : '否'}（实际提交：{item.handoff_committed == null ? '—' : item.handoff_committed ? '是' : '否'}）。</Paragraph>
      {item.preconditions && <Paragraph type="secondary">前置条件：{item.preconditions.status}{item.precondition_source ? ` · ${item.precondition_source}` : ''}{item.persistence_disabled == null ? '' : ` · 未持久化：${item.persistence_disabled ? '是' : '否'}`}</Paragraph>}
      {item.node_trace && <Paragraph type="secondary">节点轨迹：{item.node_trace.join(' → ')}</Paragraph>}
      {item.blocked_tools && item.blocked_tools.length > 0 && <Paragraph type="warning">被安全闸门拦截的写工具：{item.blocked_tools.join('、')}</Paragraph>}
    </>}
    <Text strong>Faithfulness 评审理由</Text>
    <Paragraph className="eval-judge">{item.judge_reason ?? '未进行 Faithfulness 评审'}</Paragraph>
    <Paragraph type="secondary">生成 tokens：{tokenText(item.tokens)}；judge tokens：{tokenText(item.judge_tokens)}；judge 延迟：{item.judge_latency_ms == null ? '—' : `${item.judge_latency_ms.toFixed(0)} ms`}；人工抽查：{item.manual_review?.status ?? 'pending'}{item.manual_review?.reviewer ? ` · ${item.manual_review.reviewer}` : ''}{item.manual_review?.notes ? ` · ${item.manual_review.notes}` : ''}</Paragraph>
    {item.judge_evidence?.length ? <ul>{item.judge_evidence.map((evidence, index) => <li key={index}>{JSON.stringify(evidence)}</li>)}</ul> : null}
  </Card>;
}
function Metadata({ report }: { report: RagEvalReport }) {
  const meta = report.metadata;
  return <Paragraph type="secondary">
    模式：{report.evaluation_mode ?? '历史/未标记'}；环境：{report.execution_environment ?? '未记录'}；
    题集：{meta?.dataset_version ?? report.dataset}；题集 SHA-256：{meta?.dataset_sha256 ?? '未记录'}；
    语料版本标签：{meta?.corpus_version_label ?? '未记录'}；语料清单 SHA-256：{meta?.corpus_manifest?.sha256 ?? meta?.corpus_version ?? '未记录'}（{meta?.corpus_manifest?.count ?? '未知'} 块；来源：{meta?.effective_collection_sources?.join('、') ?? meta?.corpus_manifest?.sources?.join('、') ?? meta?.retrieved_sources?.join('、') ?? '未记录'}）；
    当前有效集合：{meta?.effective_collection_chunks ?? meta?.collection_manifest?.count ?? '未知'} 向量；集合：{meta?.collection ?? report.collection}；集合版本 SHA-256：{meta?.collection_version ?? '未记录'}；
    embedding：{meta?.embedding_model ?? '未记录'}{meta?.embedding_dimension ? ` / ${meta.embedding_dimension}维` : ''}；reranker：{meta?.reranker_model ?? '未记录'}；生成模型：{meta?.generation_model ?? '未记录'}；judge：{meta?.judge_model ?? report.faithfulness_judge ?? '未记录'}（独立：{meta?.judge_independent == null ? '未知' : meta.judge_independent ? '是' : '否'}）；
    在线阈值：{meta?.online_threshold ?? report.threshold_selection?.selected_threshold ?? '未记录'}；持久化关闭：{meta?.persistence_disabled == null ? '未记录' : meta.persistence_disabled ? '是' : '否'}；生成时间：{report.generated_at ? new Date(report.generated_at).toLocaleString('zh-CN') : '历史报告未记录'}。
  </Paragraph>;
}

export function RagEvals() {
  const [mode, setMode] = useState<EvalMode>('offline');
  const [questionType, setQuestionType] = useState('all');
  const [difficulty, setDifficulty] = useState('all');
  const [split, setSplit] = useState('all');
  const [startError, setStartError] = useState('');
  const lastLoadedJobId = useRef<string | null>(null);
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const report = useQuery({
    queryKey: ['rag-eval-report', mode],
    queryFn: mode === 'online' ? ragApi.workflowEvalReport : ragApi.evalReport,
    enabled: apiMode === 'remote', retry: 0,
  });
  const jobs = useQuery({ queryKey: ['rag-jobs'], queryFn: ragApi.jobs, enabled: apiMode === 'remote', refetchInterval: 2000 });
  const jobKind = mode === 'online' ? 'evaluate-online' : 'evaluate';
  const latestEvalJob = jobs.data?.items.find((item) => item.kind === jobKind);
  const workingJob = jobs.data?.items.find((item) => item.status === 'queued' || item.status === 'running');
  const startEvaluation = useMutation({
    mutationFn: () => ragApi.startJob(jobKind),
    onSuccess: () => { setStartError(''); void jobs.refetch(); },
    onError: (error) => setStartError(error instanceof Error ? error.message : '评估启动失败'),
  });
  useEffect(() => {
    if (latestEvalJob?.status !== 'completed' || lastLoadedJobId.current === latestEvalJob.id) return;
    lastLoadedJobId.current = latestEvalJob.id;
    void report.refetch();
  }, [latestEvalJob?.id, latestEvalJob?.status, report.refetch]);

  const questions = useMemo(() => {
    const grouped = new Map<string, RagEvalCase[]>();
    for (const item of report.data?.cases ?? []) {
      const rows = grouped.get(item.id) ?? [];
      rows.push(item);
      grouped.set(item.id, rows);
    }
    return [...grouped.entries()].filter(([, rows]) =>
      (questionType === 'all' || rows[0].type === questionType) &&
      (difficulty === 'all' || rows[0].difficulty === difficulty) &&
      (split === 'all' || rows[0].split === split));
  }, [report.data, questionType, difficulty, split]);
  if (actor.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;

  const data = report.data;
  const firstStrategy = Object.values(data?.strategies ?? {})[0];
  const typeOptions = Object.keys(firstStrategy?.by_type ?? {}).map((value) => ({ value, label: typeNames[value] ?? value }));
  const difficultyOptions = Object.keys(firstStrategy?.by_difficulty ?? {}).map((value) => ({ value, label: difficultyNames[value] ?? value }));
  const overall = firstStrategy?.overall ?? data?.workflows ?? undefined;
  const legacy = data?.legacy === true || data?.schema_version === 1;
  const isOnline = mode === 'online';
  const strategies = data?.strategies ?? {};

  return <div className="workspace-shell rag-shell">
    <PageHeader actor={actor.data} />
    <main className="rag-main eval-main">
      <div className="rag-heading"><div><Text className="section-kicker">RAG EVALUATION</Text><Title level={2}>客服 RAG 评估</Title><Paragraph type="secondary">离线策略检索与在线客服工作流分别评估；历史报告只读，不作为新版基线。</Paragraph></div><Space wrap><Link to="/staff/rag">返回建库控制台</Link><Button type="primary" disabled={apiMode !== 'remote' || !!workingJob || jobs.isLoading || jobs.isError} loading={startEvaluation.isPending} onClick={() => startEvaluation.mutate()}>{isOnline ? '运行在线工作流评估' : '运行离线四策略评估'}</Button><Button icon={<ReloadOutlined />} disabled={apiMode !== 'remote'} onClick={() => void report.refetch()}>刷新报告</Button></Space></div>
      <Tabs activeKey={mode} onChange={(key) => { setMode(key as EvalMode); setQuestionType('all'); setDifficulty('all'); setSplit('all'); }} items={[
        { key: 'offline', label: '离线检索策略' }, { key: 'online', label: '在线工作流' },
      ]} />
      {apiMode === 'mock' ? <Alert showIcon type="warning" message="演示模式不提供真实评估报告" description="请切换 VITE_API_MODE=remote 并使用员工账号登录；mock 不生成评估结果。" /> : <>
        {startError && <Alert showIcon type="error" message={startError} closable onClose={() => setStartError('')} />}
        {jobs.isError && <Alert showIcon type="error" message="评估任务状态读取失败" description={jobs.error instanceof Error ? jobs.error.message : '请检查后端服务'} action={<Button size="small" onClick={() => void jobs.refetch()}>重试</Button>} />}
        {latestEvalJob && <Alert showIcon type={latestEvalJob.status === 'failed' ? 'error' : latestEvalJob.status === 'completed' ? 'success' : 'info'} message={latestEvalJob.status === 'completed' ? '评估任务已完成' : latestEvalJob.status === 'failed' ? '评估失败，旧报告仍可查看' : '评估正在后台运行'} description={latestEvalJob.error || (latestEvalJob.status === 'completed' ? '新报告已写入，页面会自动刷新。' : latestEvalJob.progress) || `启动时间：${new Date(latestEvalJob.createdAt).toLocaleString('zh-CN')}`} />}
        {report.isLoading && <Card><Spin /></Card>}
        {report.isError && <Alert showIcon type="error" message="评估报告加载失败" description={report.error instanceof Error ? report.error.message : '请检查后端服务'} action={<Button size="small" onClick={() => void report.refetch()}>重试</Button>} />}
        {data && <>
          {legacy && <Alert showIcon type="warning" message="历史报告（schema v1）" description="此报告沿用旧版 15 题结果，不含 v2 完整性、误拒答、应拒未拒、模型/token 与留出集指标；不能作为新题集基线。" />}
          {data.execution_environment === 'fixture' && <Alert showIcon type="info" message="Fixture 环境结果" description="此结果用于确定性工作流验证，不代表真实模型、知识库或依赖环境验收。" />}
          {data.metadata?.unobserved_sources?.length ? <Alert showIcon type="warning" message="题集来源覆盖不完整" description={`当前报告未观察到这些gold来源：${data.metadata.unobserved_sources.join('、')}；不可将该语料覆盖表述为完整在线验收。`} /> : null}
          <Card className="rag-card" title="报告与运行配置"><Metadata report={data} /></Card>
          {overall && <Card className="rag-card" title={isOnline ? '在线工作流总体指标' : '离线总体指标'}>
            <Paragraph type="secondary">{overall.count} 道题，按标注可答 {overall.answerable ?? '未知'}；有效评分 {overall.scored_count ?? '未记录'}，前置条件未满足 {overall.preconditions_unmet_count ?? '未记录'}。Recall/MRR 仅适用于有来源标签的离线检索题。</Paragraph>
            <Table size="small" rowKey="key" pagination={false} scroll={{ x: isOnline ? 2000 : 1450 }} columns={metricColumns(isOnline)} dataSource={Object.entries(strategies).map(([key, value]) => ({ key, name: strategyNames[key] ?? key, metrics: value.overall }))} />
          {firstStrategy?.by_type && <Card className="rag-card" title="按问题类型对比">
            <Table size="small" rowKey="key" pagination={false} scroll={{ x: 800 }} columns={[
              { title: '题型', dataIndex: 'name', key: 'name', fixed: 'left' as const, width: 170 },
              ...Object.entries(strategies).map(([strategy, result]) => ({
                title: strategyNames[strategy] ?? strategy, key: strategy, dataIndex: 'key', width: 190,
                render: (type: string) => {
                  const metrics = result.by_type[type];
                  return metrics ? <span className="eval-bucket-value">题数 {metrics.count}<br />R@1 {rate(metrics['recall@1'])}<br />完整性 {rate(metrics.completeness)}<br />误拒答 {rate(metrics.false_refusal)}<br />应拒未拒 {rate(metrics.unsafe_answer)}</span> : '—';
                },
              })),
            ]} dataSource={Object.entries(firstStrategy.by_type).map(([key, metrics]) => ({ key, name: `${typeNames[key] ?? key}（${metrics.count}）` }))} />
          </Card>}
          {firstStrategy?.by_split && <Card className="rag-card" title="Validation / Holdout 分割对比">
            <Table size="small" rowKey="key" pagination={false} scroll={{ x: 800 }} columns={[
              { title: '分割', dataIndex: 'name', key: 'name', fixed: 'left' as const, width: 150 },
              ...Object.entries(strategies).map(([strategy, result]) => ({
                title: strategyNames[strategy] ?? strategy, key: strategy, dataIndex: 'key', width: 190,
                render: (part: string) => {
                  const metrics = result.by_split?.[part];
                  return metrics ? <span className="eval-bucket-value">题数 {metrics.count}<br />完整性 {rate(metrics.completeness)}<br />误拒答 {rate(metrics.false_refusal)}<br />应拒未拒 {rate(metrics.unsafe_answer)}<br />延迟 {metrics.latency_ms == null ? '—' : `${metrics.latency_ms.toFixed(0)} ms`}</span> : '—';
                },
              })),
            ]} dataSource={Object.entries(firstStrategy.by_split).map(([key, metrics]) => ({ key, name: `${key}（${metrics.count}）` }))} />
          </Card>}
            <Table size="small" pagination={false} rowKey="name" columns={[
              { title: '策略', dataIndex: 'name', key: 'name' },
              { title: '生成 tokens', dataIndex: 'generation', key: 'generation' },
              { title: 'Judge tokens', dataIndex: 'judge', key: 'judge' },
              { title: 'Judge 延迟', dataIndex: 'judgeLatency', key: 'judgeLatency' },
            ]} dataSource={Object.entries(strategies).map(([key, value]) => ({
              key, name: strategyNames[key] ?? key, generation: tokenText(value.overall.tokens),
              judge: tokenText(value.overall.judge_tokens),
              judgeLatency: value.overall.judge_latency_ms == null ? '—' : `${value.overall.judge_latency_ms.toFixed(0)} ms`,
            }))} />
          </Card>}
          {data.threshold_selection && <Card className="rag-card" title="阈值校准与独立留出">
            <Paragraph>候选阈值：{data.threshold_selection.candidates.map((item) => item.threshold).join('、')}；所选阈值：<Text strong>{data.threshold_selection.selected_threshold}</Text>；验证集 {data.threshold_selection.validation_count} 题；holdout {data.threshold_selection.holdout_count} 题。</Paragraph>
            <Table size="small" pagination={false} rowKey="threshold" columns={[
              { title: '阈值', dataIndex: 'threshold', key: 'threshold' }, { title: '验证损失', dataIndex: 'loss', key: 'loss' },
              { title: '验证集完整性', key: 'validation', render: (_: unknown, row: { summary: RagEvalMetrics }) => rate(row.summary.completeness) },
              { title: '误拒答', key: 'false_refusal', render: (_: unknown, row: { summary: RagEvalMetrics }) => rate(row.summary.false_refusal) },
              { title: '应拒未拒', key: 'unsafe_answer', render: (_: unknown, row: { summary: RagEvalMetrics }) => rate(row.summary.unsafe_answer) },
            ]} dataSource={data.threshold_selection.candidates} />
            <Paragraph type="secondary">最终验证集完整性 {rate(data.threshold_selection.validation.completeness)}；holdout 完整性 {rate(data.threshold_selection.holdout.completeness)}，误拒答 {rate(data.threshold_selection.holdout.false_refusal)}，应拒未拒 {rate(data.threshold_selection.holdout.unsafe_answer)}；总 judge tokens {tokenText(data.workflows?.judge_tokens)}，judge 延迟 {data.workflows?.judge_latency_ms == null ? '—' : `${data.workflows.judge_latency_ms.toFixed(0)} ms`}。留出集不可用于继续调阈值。</Paragraph>
          </Card>}
          <Card className="rag-card" title={`逐题核对（${questions.length} / ${new Set(data.cases.map((item) => item.id)).size}）`} extra={<Space wrap>
            <Select aria-label="按题型筛选" value={questionType} onChange={setQuestionType} style={{ minWidth: 150 }} options={[{ value: 'all', label: '全部类型' }, ...typeOptions]} />
            <Select aria-label="按难度筛选" value={difficulty} onChange={setDifficulty} style={{ minWidth: 130 }} options={[{ value: 'all', label: '全部难度' }, ...difficultyOptions]} />
            <Select aria-label="按数据集筛选" value={split} onChange={setSplit} style={{ minWidth: 130 }} options={[{ value: 'all', label: 'validation 与 holdout' }, { value: 'validation', label: 'validation' }, { value: 'holdout', label: 'holdout' }]} />
          </Space>}>
            {questions.length === 0 ? <Empty description="没有符合条件的题目" /> : <Collapse items={questions.map(([id, rows]) => ({
              key: id,
              label: <Space wrap><Text strong>{rows[0].query}</Text><Tag>{typeNames[rows[0].type] ?? rows[0].type}</Tag>{rows[0].split && <Tag>{rows[0].split}</Tag>}{rows[0].expected_refusal == null ? <Tag>历史题：未记录应拒答标注</Tag> : <Tag color={rows[0].expected_refusal ? 'orange' : 'blue'}>{rows[0].expected_refusal ? '应拒答' : '可答/可说明'}</Tag>}<Text type="secondary">{id}</Text></Space>,
              children: <>
                <div className="eval-truth"><div><Text strong>Ground truth / authority</Text><div>{rows[0].ground_truth.length ? rows[0].ground_truth.map((truth) => <Tag key={`${truth.source_path}:${truth.section}`} title={truth.authority_note}>{truth.source_path} · {truth.section}</Tag>) : <Tag color="orange">库外题，无目标章节</Tag>}</div></div>
                  <div><Text strong>答案要点</Text>{rows[0].answer_facts.length ? <ul>{rows[0].answer_facts.map((fact, index) => <li key={index}>{fact}</li>)}</ul> : <Paragraph type="secondary">无可接受事实要点</Paragraph>}
                    {rows[0].acceptable_answers?.length ? <><Text strong>可接受回答示例</Text><ul>{rows[0].acceptable_answers.map((answer) => <li key={answer}>{answer}</li>)}</ul></> : null}
                    {rows[0].refusal_conditions?.length ? <><Text strong>拒答条件</Text><ul>{rows[0].refusal_conditions.map((condition) => <li key={condition}>{condition}</li>)}</ul></> : null}
                  </div></div>
                {rows.map((item) => <StrategyResult key={item.strategy} item={item} />)}
              </>,
            }))} />}
          </Card>
        </>}
      </>}
    </main>
  </div>;
}
