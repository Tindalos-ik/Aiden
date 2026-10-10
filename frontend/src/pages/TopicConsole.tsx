import { createContext, useContext, useEffect, useState } from 'react';
import { Alert, Button, Card, Collapse, DatePicker, Descriptions, Drawer, Empty, InputNumber, Pagination, Select, Space, Spin, Table, Tabs, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import type { Dayjs } from 'dayjs';
import { api, apiMode } from '../api';
import { topicApi } from '../api/topics';
import { StaffLayout } from '../components/StaffLayout';
import type { TopicBatchInput, TopicCursor, TopicEvaluation, TopicFilters, TopicJob, TopicPrediction, TopicResult } from '../types';

const { Title, Text, Paragraph } = Typography;
const active = (job: TopicJob) => job.state === 'queued' || job.state === 'running';
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败';
const statusNames: Record<string, string> = { predicted: '已分类', unclassified: '未分类（无当前预测）', uncertain: '不确定', insufficient_context: '上下文不足', confirmed: '人工已确认', other: '其他（业务主题标签）', queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败', interrupted: '已中断' };
const labelText = (label: string) => statusNames[label] || label;
const TopicLabels = createContext(labelText);
const dateText = (date: string | null | undefined) => date ? new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(date) ? date : `${date}Z`).toLocaleString('zh-CN') : '未记录';
const metric = (value: number | null | undefined) => value == null ? '未评估 / 不适用' : value.toFixed(4);
const experimentWarning = '合成实验模型，仅用于预标；评估限于合成数据，业务应用需另行验收。';
const modeText = (mode: string | null | undefined) => mode === 'human_reviewed' ? '人工审核数据' : mode === 'synthetic_experiment' ? '合成实验' : '来源未确认';
function ModelSource({ value }: { value: { model_key?: string | null; data_mode?: string | null; evaluation_source?: string | null } }) {
  return <Paragraph>模型标识 {value.model_key ?? '未记录'} · 数据来源 {modeText(value.data_mode)} · 评估来源 {value.evaluation_source ?? '未确认'}</Paragraph>;
}
function JsonEvidence({ value }: { value: unknown }) { return <pre className="rag-result topic-evidence" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value == null ? '未记录' : JSON.stringify(value, null, 2)}</pre>; }
function QueryError({ error, retry }: { error: unknown; retry?: () => void }) { return <Alert type="error" showIcon message="数据读取失败" description={<span style={{ whiteSpace: 'pre-wrap' }}>{errorText(error)}</span>} action={retry && <Button onClick={retry}>重试</Button>} />; }
function CountTable({ counts, title }: { counts: Record<string, number>; title: string }) {
  const labelText = useContext(TopicLabels);
  return <Table size="small" scroll={{ x: 420 }} pagination={false} rowKey="label" dataSource={Object.entries(counts).map(([label, count]) => ({ label, count }))} columns={[{ title, dataIndex: 'label', render: labelText }, { title: '计数', dataIndex: 'count' }]} />;
}
function Prediction({ item }: { item: TopicPrediction }) {
  const labelText = useContext(TopicLabels);
  return <Card size="small" title={<Space wrap><Tag color={item.current ? 'blue' : 'orange'}>{item.current ? '当前预测' : '历史 / 过期预测'}</Tag><Text>{labelText(item.status)}</Text></Space>}>
    <Paragraph>标签：{item.predicted_labels.map(labelText).join('、') || '无'}；模型 {item.model_version}；分类体系 {item.taxonomy_version}</Paragraph>
    <ModelSource value={item} />
    <Paragraph type="secondary">创建 {dateText(item.created_at)} · 更新 {dateText(item.updated_at)} · 输入哈希 {item.input_hash}</Paragraph>
    <Collapse size="small" items={[{ key: 'scores', label: '完整标签分数', children: <><Paragraph>各标签独立评分，供分类参考；分类正确率需结合评测报告判断。</Paragraph><Table size="small" scroll={{ x: 420 }} pagination={false} rowKey="label" dataSource={Object.entries(item.scores).map(([label, score]) => ({ label, score }))} columns={[{ title: '标签', dataIndex: 'label', render: labelText }, { title: '标签分数', dataIndex: 'score', render: (score: number) => score.toFixed(6) }]} /></> }]} />
  </Card>;
}
function ResultEvidence({ item }: { item: TopicResult }) {
  const labelText = useContext(TopicLabels);
  const prediction = item.current_prediction || item.latest_prediction;
  const human = item.human_current || item.human_latest;
  return <Space direction="vertical" style={{ width: '100%' }} size="middle">
    <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.question}</Paragraph>
    <Paragraph>来源 ID {item.source_id} · 时间 {dateText(item.created_at)}</Paragraph>
    <Space wrap>{item.conversation_id && <Link to={`/staff/${encodeURIComponent(item.conversation_id)}`}>关联会话</Link>}{item.matched_review_id && <Link to={`/staff/reviews?review_id=${encodeURIComponent(item.matched_review_id)}`}>关联归并审核</Link>}</Space>
    {!item.current_prediction && <Alert type="warning" message="当前输入、模型版本与分类体系尚无匹配预测" />}
    {prediction && <Prediction item={prediction} />}
    {item.current_prediction && item.latest_prediction && item.latest_prediction.id !== item.current_prediction.id && <Prediction item={item.latest_prediction} />}
    <Card size="small" title="人工最终标注（只读）">{human ? <><Tag color={human.current ? 'green' : 'orange'}>{human.current ? '当前人工标注' : '过期人工标注'}</Tag><Paragraph>状态：{labelText(human.status)}；标签：{human.labels.map(labelText).join('、') || '无'}；更新时间 {dateText(human.updated_at)}</Paragraph><Text type="secondary">标注 ID {human.id} · 输入哈希 {human.input_hash}</Text></> : <Text>尚无人工标注。</Text>}</Card>
  </Space>;
}
function ReportEvidence({ report }: { report: TopicEvaluation }) {
  const labelText = useContext(TopicLabels);
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Paragraph>模型 {report.model_version} · 分类体系 {report.taxonomy_version} · 冻结测试划分 {report.test_split_hash}</Paragraph>
    <ModelSource value={report} />
    {report.data_mode === 'synthetic_experiment' && <Alert type="warning" showIcon message={experimentWarning} />}
    <Paragraph>冻结数据集哈希：{report.dataset_hash ?? '未记录'}；训练划分 {report.split_hashes?.train ?? '未记录'}；验证划分 {report.split_hashes?.validation ?? '未记录'}；测试划分 {report.split_hashes?.test ?? report.test_split_hash ?? '未记录'}。</Paragraph>
    <Descriptions bordered size="small" items={[
      { key: 'micro', label: 'Micro F1', children: metric(report.metrics.micro_f1) }, { key: 'macro', label: '有支持类别 Macro F1', children: metric(report.metrics.macro_f1_supported_classes) },
      { key: 'supported', label: '有支持类别数', children: report.metrics.supported_class_count ?? '未记录' }, { key: 'duration', label: '真实耗时（秒）', children: report.elapsed_seconds ?? '未记录' },
      { key: 'throughput', label: '真实吞吐（样本/秒）', children: report.samples_per_second ?? '未记录' }, { key: 'cost', label: '成本', children: report.cost ?? '未记录 / 不适用' },
    ]} />
    <Alert type="info" message="无测试样本的类别证据不足；空值表示未评估或不适用。报告结论适用于报告版本。" />
    <Table size="small" pagination={false} rowKey="label" scroll={{ x: 650 }} dataSource={Object.entries(report.metrics.per_class).map(([label, values]) => ({ label, ...values }))} columns={[
      { title: '类别', dataIndex: 'label', render: labelText }, { title: 'Support', dataIndex: 'support' }, { title: '证据', dataIndex: 'evaluable', render: (value: boolean) => value ? '有测试支持' : '证据不足' },
      ...['precision', 'recall', 'f1'].map((key) => ({ title: key, dataIndex: key, render: metric })),
    ]} />
    <Collapse items={[
      { key: 'coverage', label: '样本覆盖 / 多标签原始证据', children: <JsonEvidence value={report.coverage} /> },
      { key: 'status', label: '状态混淆、标签混淆与修订需求', children: <><JsonEvidence value={report.status_metrics} /><Paragraph>上下文不足单独记录；编码器评测按“不确定”比较。人工评估证据需有明确的人工审核来源。</Paragraph></> },
      { key: 'resources', label: '真实资源与基线配置', children: <JsonEvidence value={{ baseline_configuration: report.baseline_configuration, resource_usage: report.resource_usage, usage: report.usage }} /> },
    ]} />
    <Card size="small" title="错误样例与分来源参考标签">{report.errors.length ? report.errors.map((item) => <Card size="small" key={item.id}>
      <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.redacted_text}</Paragraph>
      {report.data_mode === 'human_reviewed' ? <Paragraph>人工真值：{item.human_truth_labels?.map(labelText).join('、') || '未记录'}</Paragraph> : <Paragraph>人工真值：缺少已核验的人工审核来源。</Paragraph>}
      <Paragraph>合成预标标签：{item.synthetic_prelabel_labels?.map(labelText).join('、') || '未记录'}</Paragraph>
      <Paragraph>预期状态 {labelText(item.expected_status)} · 漏标 {item.missing.join('、') || '无'} · 多标 {item.extra.join('、') || '无'}</Paragraph>
      <Collapse size="small" items={[{ key: 'raw', label: '原始预测与人工修订证据', children: <JsonEvidence value={{ raw_prediction: item.raw_prediction, human_revision: item.human_revision, unverified_reference_labels: report.data_mode == null ? item.human_truth_labels : undefined }} /> }]} />
    </Card>) : <Empty description="此报告未记录错误样例" />}</Card>
  </Space>;
}

export function TopicConsole() {
  const client = useQueryClient();
  const remote = apiMode === 'remote';
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const [tab, setTab] = useState('overview');
  const [modelKey, setModelKey] = useState<string>();
  const [selectionRevision, setSelectionRevision] = useState(0);
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [filters, setFilters] = useState<TopicFilters>({});
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(100);
  const [batchSize, setBatchSize] = useState(32);
  const [cursor, setCursor] = useState<TopicCursor>();
  const [cursorIdentity, setCursorIdentity] = useState<{ model_key: string; model_version: string }>();
  const [jobId, setJobId] = useState<string>();
  const [sourceId, setSourceId] = useState<string>();
  const [reportId, setReportId] = useState<string>();
  const [actionError, setActionError] = useState('');
  const registry = useQuery({ queryKey: ['topics', 'registry'], queryFn: () => topicApi.availability(), enabled: remote, retry: 0 });
  const availability = useQuery({ queryKey: ['topics', 'availability', modelKey, selectionRevision], queryFn: () => topicApi.availability(modelKey), enabled: remote && !!modelKey, retry: 0, placeholderData: () => undefined });
  const selected = availability.data?.selected_model_key === modelKey ? availability.data : undefined;
  const selectedVersion = modelKey ? selected?.selected_model_version : undefined;
  const selectedCandidate = (availability.data?.models ?? registry.data?.models)?.find((item) => item.model_key === modelKey);
  const displayNames: Record<string, string> = Object.fromEntries((selected?.taxonomy.labels ?? registry.data?.taxonomy.labels ?? []).map((item) => [item.id, item.display]));
  const labelText = (label: string) => displayNames[label] || statusNames[label] || label;
  const viewVersion = selectedVersion ? filters.model_version || selectedVersion : undefined;
  const resultFilters = { ...filters, model_version: viewVersion };
  const jobs = useQuery({ queryKey: ['topics', 'jobs'], queryFn: topicApi.jobs, enabled: remote, retry: 0, refetchInterval: (query) => query.state.data?.items.some(active) ? 2000 : false });
  const job = useQuery({ queryKey: ['topics', 'job', modelKey, jobId], queryFn: () => topicApi.job(jobId!), enabled: remote && !!jobId, retry: 0, placeholderData: () => undefined, refetchInterval: (query) => query.state.data && active(query.state.data) ? 2000 : false });
  const results = useQuery({ queryKey: ['topics', 'results', modelKey, resultFilters, page], queryFn: () => topicApi.results(resultFilters, (page - 1) * 20), enabled: remote && tab === 'results' && !!viewVersion, retry: 0, placeholderData: () => undefined });
  const result = useQuery({ queryKey: ['topics', 'result', modelKey, sourceId, viewVersion], queryFn: () => topicApi.result(sourceId!, viewVersion), enabled: remote && !!sourceId && !!viewVersion, retry: 0, placeholderData: () => undefined });
  const statsScope = { start_at: filters.start_at, end_at: filters.end_at, model_version: viewVersion };
  const stats = useQuery({ queryKey: ['topics', 'stats', modelKey, statsScope], queryFn: () => topicApi.stats(statsScope), enabled: remote && tab === 'stats' && !!viewVersion, retry: 0, placeholderData: () => undefined });
  const reports = useQuery({ queryKey: ['topics', 'reports', modelKey, viewVersion], queryFn: () => topicApi.reports(viewVersion!), enabled: remote && tab === 'reports' && !!viewVersion, retry: 0, placeholderData: () => undefined });
  const report = useQuery({ queryKey: ['topics', 'report', modelKey, viewVersion, reportId], queryFn: () => topicApi.report(reportId!), enabled: remote && !!reportId && !!viewVersion, retry: 0, placeholderData: () => undefined });
  const refresh = () => client.invalidateQueries({ queryKey: ['topics'] });
  const mutation = useMutation({
    mutationFn: (input: TopicBatchInput | { model_key: string; batch_size: number; kind: 'evaluation' }) => 'kind' in input ? topicApi.startEvaluation(input.model_key, input.batch_size) : topicApi.startBatch(input),
    onSuccess: (started) => { setActionError(''); setJobId(started.id); client.setQueryData(['topics', 'job', started.model_key, started.id], started); void refresh(); },
    onError: (error) => { setActionError(errorText(error)); void availability.refetch(); void jobs.refetch(); },
  });
  const busy = mutation.isPending || !!selected?.active_job && active(selected.active_job) || !!registry.data?.active_job && active(registry.data.active_job) || jobs.data?.items.some(active) === true;
  const runningJobs = [...new Map([...(jobs.data?.items ?? []), ...(selected?.active_job ? [selected.active_job] : []), ...(registry.data?.active_job ? [registry.data.active_job] : [])].filter(active).map((item) => [item.id, item] as const)).values()];
  const terminalJobs = jobs.data?.items.filter((item) => !active(item)).map((item) => `${item.id}:${item.state}`).join('|');
  useEffect(() => {
    // 完成轮询后解除可用性锁，并重新读取真实结果与报告。
    if (!terminalJobs) return;
    for (const key of ['registry', 'availability', 'results', 'stats', 'reports']) {
      void client.invalidateQueries({ queryKey: ['topics', key] });
    }
  }, [client, terminalJobs]);
  const update = (patch: Partial<TopicFilters>) => {
    setFilters((old) => ({ ...old, ...patch })); setPage(1);
    if ('model_version' in patch) { setSourceId(undefined); setReportId(undefined); }
  };
  const cursorMatches = !cursor || !!selectedVersion && cursorIdentity?.model_key === modelKey && cursorIdentity?.model_version === selectedVersion;
  const resume = (item: TopicJob) => {
    if (busy || !selectedVersion || item.model_key !== modelKey || item.model_version !== selectedVersion || !item.result?.next_cursor) return;
    setCursor(item.result.next_cursor); setCursorIdentity({ model_key: modelKey!, model_version: selectedVersion }); setJobId(undefined); setTab('overview');
  };
  if (!actor.data) return <Spin />;
  const dateControls = <DatePicker.RangePicker value={range} onChange={(value) => { setRange(value); update({ start_at: value?.[0]?.startOf('day').toISOString(), end_at: value?.[1]?.add(1, 'day').startOf('day').toISOString() }); }} />;
  const scopeControls = <Space wrap className="topic-filters">{dateControls}<Text>查看版本（仅用于历史查询）</Text><Select aria-label="历史查看模型版本" allowClear disabled={!selectedVersion} placeholder={selectedVersion || '请先选择执行模型'} style={{ minWidth: 240 }} value={filters.model_version} onChange={(model_version) => update({ model_version })} options={[...new Set([...(selected?.model_versions ?? []), ...(registry.data?.model_versions ?? []), ...(selectedVersion ? [selectedVersion] : [])])].map((value) => ({ value, label: value }))} /></Space>;
  const renderJob = (item: TopicJob) => <Card size="small" key={item.id} title={<Space wrap><Text>{item.kind === 'batch' ? '有限批次分类' : '真实编码器评测'}</Text><Tag color={item.state === 'failed' || item.state === 'interrupted' ? 'red' : item.state === 'succeeded' ? 'green' : 'blue'}>{labelText(item.state)}</Tag></Space>} extra={<Button onClick={() => setJobId(item.id)}>任务详情</Button>}>
    <Paragraph>ID {item.id} · 实际模型 {item.model_version ?? '未记录'} · 分类体系 {item.taxonomy_version} · 创建 {dateText(item.created_at)}</Paragraph>
    <ModelSource value={item} />{item.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
    {item.result && <Paragraph>已处理 {item.result.processed ?? '未记录'} · 已持久化 {item.result.persisted ?? '未记录'} · 来源过期 {item.result.stale_source ?? '未记录'}；完整结果见任务详情。</Paragraph>}{item.error && <Alert type="error" message={`${item.error.code}: ${item.error.message}`} />}
    {item.result?.next_cursor && <Button disabled={busy || !selectedVersion || item.model_key !== modelKey || item.model_version !== selectedVersion} onClick={() => resume(item)}>继续同模型版本任务</Button>}
    {item.report_id && <Button disabled={!selectedVersion || !item.model_version} onClick={() => { update({ model_version: item.model_version! }); setReportId(item.report_id!); setTab('reports'); }}>查看生成报告（任务版本）</Button>}
  </Card>;
  return <TopicLabels.Provider value={labelText}><StaffLayout actor={actor.data}><main className="rag-main topic-main">
    <div className="rag-heading"><div><Title level={2}>旁路主题分类</Title><Paragraph>分析低置信度问题，查看模型预测、人工标注与评测证据。</Paragraph></div><Button icon={<ReloadOutlined />} disabled={!remote} onClick={() => void refresh()}>刷新状态</Button></div>
    <Card size="small" className="staff-query" title="执行模型">
      {registry.isLoading && <Spin />}{registry.isError && <QueryError error={registry.error} retry={() => void registry.refetch()} />}
      <Select aria-label="执行模型" allowClear placeholder="请选择执行模型" style={{ width: '100%', maxWidth: 720 }} value={modelKey} disabled={!remote || busy || jobs.isPending || jobs.isError} options={(registry.data?.models ?? []).map((item) => ({ value: item.model_key, label: `${item.display_name} · ${item.model_key} · ${modeText(item.data_mode)}` }))} onChange={(key: string | undefined) => {
        setModelKey(key); setSelectionRevision((revision) => revision + 1); setFilters((old) => ({ ...old, model_version: undefined })); setPage(1); setCursor(undefined); setCursorIdentity(undefined); setSourceId(undefined); setReportId(undefined); setJobId(undefined); setActionError('');
      }} />
      <Paragraph type="secondary">选择模型后检查可用性；任务运行期间锁定模型选择。</Paragraph>
      <Collapse size="small" items={(registry.data?.models ?? []).map((item) => ({ key: item.model_key, label: `${item.display_name} · ${item.model_key} · ${modeText(item.data_mode)} · ${item.validated ? '已校验候选' : '尚未校验'}`, children: <><Paragraph>候选实际版本 {item.model_version ?? '未确认'} · 评估来源 {item.evaluation_source ?? '未确认'}</Paragraph><JsonEvidence value={item.training_summary} /></> }))} />
      {(registry.data?.models ?? []).filter((item) => item.blocked_reason).map((item) => <Alert key={item.model_key} type="warning" message={`${item.display_name} · ${item.blocked_reason!.code}: ${item.blocked_reason!.message}`} />)}
      <Paragraph>当前执行：{selectedCandidate?.display_name ?? modelKey ?? '尚未选择'} · 模型标识 {modelKey ?? '无'} · 核验版本 {selectedVersion ?? '未确认'}</Paragraph>
      {selected && <ModelSource value={{ ...selected, model_key: selected.selected_model_key }} />}
      {selectedCandidate && <><Collapse size="small" items={[{ key: 'training', label: '当前模型训练摘要', children: <JsonEvidence value={selectedCandidate.training_summary} /> }]} />{selectedCandidate.blocked_reason && <Alert type="warning" message={`${selectedCandidate.blocked_reason.code}: ${selectedCandidate.blocked_reason.message}`} />}</>}
    </Card>
    {busy && <Alert type="info" showIcon message={mutation.isPending ? '正在启动任务，暂时锁定模型选择与启动操作' : '任务处理中，暂时锁定模型选择与启动操作'} description={<Space wrap>{runningJobs.map((item) => <Button key={item.id} size="small" onClick={() => setJobId(item.id)}>{labelText(item.state)} · {item.id} · 任务详情</Button>)}</Space>} />}
    {(selectedCandidate?.data_mode === 'synthetic_experiment' || selected?.data_mode === 'synthetic_experiment') && <Alert type="warning" showIcon message={experimentWarning} />}
    {remote && !viewVersion && <Alert type="warning" showIcon message="请先选择模型并确认版本，再查看结果、统计和报告。" />}
    {!remote && <Alert type="warning" showIcon message="演示模式暂不提供主题分类与评测数据" />}
    {actionError && <QueryError error={new Error(actionError)} />}
    {tab === 'stats' && remote && !statsScope.model_version && <Alert type="warning" showIcon message="统计暂不可用：执行模型版本尚未确认。" />}
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'overview', label: '运行概览', children: <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {availability.isLoading && <Spin />}{availability.isError && <QueryError error={availability.error} retry={() => void availability.refetch()} />}
        <Card title="模型可用性"><Paragraph>选定模型版本：{availability.data?.selected_model_version ?? '未确认'}；分类体系：{availability.data?.taxonomy.version ?? '未确认'}。</Paragraph>{availability.data?.checks.map((check) => <Alert key={check.name} type={check.ready ? 'success' : 'warning'} showIcon message={`${check.name} · ${check.ready ? '通过' : '阻塞'} · ${check.code}`} description={check.message} />)}{!availability.data && <Text>可用性尚未确认，暂不可启动任务。</Text>}</Card>
        {availability.data?.checks.filter((check) => check.missing_tables?.length || Object.keys(check.missing_columns ?? {}).length).map((check) => <Collapse key={`${check.name}-schema`} size="small" items={[{ key: 'schema', label: '数据结构异常详情（供管理员处理）', children: <Alert type="error" message="数据库结构阻塞" description={`缺少表：${check.missing_tables?.join('、') || '未报告'}；缺少列：${Object.entries(check.missing_columns ?? {}).map(([table, columns]) => `${table}: ${columns.join('、')}`).join('；') || '未报告'}`} /> }]} />)}
        <Card title="有限批次分类">
          <Paragraph>日期范围包含首尾日期，按本地日历筛选来源创建时间；不选日期则按条数上限处理。单次最多处理 5000 条，每批最多 500 条。</Paragraph>
          <Space wrap className="topic-filters">{dateControls}<Text>执行版本：{selectedVersion ?? '未确认'}</Text></Space>
          <Space wrap><Text>总条数上限</Text><InputNumber aria-label="批次总条数" min={1} max={5000} value={limit} onChange={(value) => value != null && setLimit(value)} /><Text>每批条数</Text><InputNumber aria-label="模型批大小" min={1} max={500} value={batchSize} onChange={(value) => value != null && setBatchSize(value)} />
            <Button type="primary" loading={mutation.isPending} disabled={!remote || !modelKey || !selectedVersion || !selected?.can_classify || !cursorMatches || busy || jobs.isError || jobs.isPending} onClick={() => modelKey && mutation.mutate({ model_key: modelKey, start_at: filters.start_at, end_at: filters.end_at, limit, batch_size: batchSize, after: cursor })}>启动旁路分类</Button>
          </Space>
          {cursor && <><Collapse size="small" items={[{ key: 'cursor', label: '续跑位置详情', children: <><Paragraph>适用模型 {cursorIdentity?.model_key} · 版本 {cursorIdentity?.model_version}</Paragraph><JsonEvidence value={cursor} /></> }]} />{!cursorMatches && <Alert type="warning" message="模型版本已变化，请清除续跑位置后重新启动。" />}<Button onClick={() => { setCursor(undefined); setCursorIdentity(undefined); }}>清除续跑位置</Button></>}
        </Card>
        <Card title="任务历史">{jobs.isError ? <QueryError error={jobs.error} /> : jobs.isLoading ? <Spin /> : jobs.data?.items.length ? <div className="rag-stack">{jobs.data.items.map(renderJob)}</div> : <Empty description={remote ? '暂无任务' : '演示模式暂不提供任务数据'} />}</Card>
      </Space> },
      { key: 'results', label: '分类结果', children: <Card title="原始来源与分类预测（只读）">{scopeControls}<Space wrap className="topic-filters"><Select aria-label="主题标签" allowClear placeholder="主题标签" value={filters.topic} onChange={(topic) => update({ topic })} style={{ width: 180 }} options={(availability.data?.taxonomy.labels ?? []).map((item) => ({ value: item.id, label: item.display }))} /><Select aria-label="分类状态" allowClear placeholder="分类状态" style={{ width: 235 }} value={filters.status} onChange={(status) => update({ status })} options={['predicted', 'uncertain', 'unclassified'].map((value) => ({ value, label: labelText(value) }))} /><Select aria-label="是否有当前预测" allowClear placeholder="当前预测" style={{ width: 205 }} value={filters.current_prediction} onChange={(current_prediction) => update({ current_prediction })} options={[{ value: true, label: '有当前预测' }, { value: false, label: '无当前预测' }]} /><Select aria-label="是否有人工标注" allowClear placeholder="人工标注" style={{ width: 160 }} value={filters.has_human} onChange={(has_human) => update({ has_human })} options={[{ value: true, label: '有当前人工标注' }, { value: false, label: '无当前人工标注' }]} /></Space><Collapse size="small" items={[{ key: 'states', label: '分类状态说明', children: <Paragraph>未分类：尚无当前预测；不确定：模型暂难判断；上下文不足：人工标注状态；其他：业务主题标签。当前分类使用匹配当前输入、模型版本与分类体系的预测，历史预测可在详情中查看。</Paragraph> }]} />{results.isError ? <QueryError error={results.error} /> : results.isLoading ? <Spin /> : <><div className="rag-stack">{results.data?.items.map((item) => <Card size="small" key={item.source_id} title={<Space wrap><Text>{item.source_id}</Text><Tag>{item.current_prediction ? labelText(item.current_prediction.status) : labelText('unclassified')}</Tag>{!item.current_prediction && item.latest_prediction && <Tag color="orange">有过期预测</Tag>}</Space>} extra={<Button onClick={() => setSourceId(item.source_id)}>查看分数与人工标注</Button>}><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.question}</Paragraph><Text type="secondary">{dateText(item.created_at)} · 当前标签 {item.current_prediction?.predicted_labels.map(labelText).join('、') || '无'}</Text></Card>)}</div>{results.data?.total === 0 && <Empty description="当前范围没有来源记录" />}{results.data && <Pagination current={page} pageSize={20} total={results.data.total} showSizeChanger={false} onChange={setPage} />}</>}</Card> },
      { key: 'stats', label: '主题统计', children: <Card title="当前输入 / 单一模型 / 当前分类体系统计">
        {scopeControls}<Alert type="info" showIcon message="统计仅按时间和模型版本筛选，其他筛选请用于“分类结果”。每条来源可有多个标签，标签计数总和可能高于来源数。" />
        <Paragraph>时间范围：{filters.start_at || '不限开始时间'} 至 {filters.end_at || '不限结束时间'}；模型版本 {viewVersion ?? '未确认'}；分类体系 {stats.data?.taxonomy_version ?? '未确认'}。</Paragraph>
        {stats.isError ? <QueryError error={stats.error} /> : stats.isLoading ? <Spin /> : stats.data && viewVersion ? <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <ModelSource value={stats.data} />{stats.data.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
          <Descriptions bordered items={[{ key: 'sources', label: '唯一原始来源', children: stats.data.source_unique_count }, { key: 'current', label: '当前模型预测', children: stats.data.model.source_count }, { key: 'unclassified', label: '未分类', children: stats.data.model.unclassified_count }, { key: 'uncertain', label: '不确定', children: stats.data.model.status_counts.uncertain ?? '未返回该状态计数' }, { key: 'human', label: '当前人工标注', children: stats.data.human.source_count }, { key: 'review', label: '归并审核去重数', children: stats.data.review_unique_count }, { key: 'lifetime', label: '关联审核全生命周期出现数（非本范围）', children: stats.data.linked_reviews_lifetime_occurrence_count }]} />
          <CountTable counts={stats.data.model.label_counts} title="模型主题分布" /><CountTable counts={stats.data.model.status_counts} title="模型状态" /><CountTable counts={stats.data.human.label_counts} title="独立人工主题分布" /><CountTable counts={stats.data.human.status_counts} title="独立人工状态" /><CountTable counts={stats.data.merged_review_label_counts.model} title="归并审核去重 · 模型标签" /><CountTable counts={stats.data.merged_review_label_counts.human} title="归并审核去重 · 人工标签" />
        </Space> : <Text>暂无可用统计。</Text>}
      </Card> },
      { key: 'reports', label: '评测报告', children: <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Card title="选定执行模型的冻结测试集评测">
          <Paragraph>执行模型 {modelKey ?? '未选择'} · 核验版本 {selectedVersion ?? '未确认'} · 数据来源 {modeText(selected?.data_mode)} · 评估来源 {selected?.evaluation_source ?? '未确认'}。使用该模型绑定的冻结测试集。可评测：{selected?.can_evaluate ? '是' : '否 / 未确认'}。</Paragraph>
          <Button type="primary" disabled={!remote || !modelKey || !selectedVersion || !selected?.can_evaluate || busy || jobs.isError || jobs.isPending} loading={mutation.isPending} onClick={() => modelKey && mutation.mutate({ kind: 'evaluation', model_key: modelKey, batch_size: batchSize })}>启动编码器评测</Button>
          {selected?.checks.filter((check) => !check.ready).map((check) => <Alert key={check.name} type="warning" message={`${check.name} · ${check.code}`} description={check.message} />)}
        </Card>
        <Card title="评测报告">
          {scopeControls}<Paragraph>查看版本：{viewVersion ?? '未确认'}。报告结论适用于对应模型版本。</Paragraph>
          {reports.isError ? <QueryError error={reports.error} /> : reports.isLoading ? <Spin /> : reports.data?.items.length && viewVersion ? <div className="rag-stack">{reports.data.items.map((item) => <Card size="small" key={item.id} title={`${item.model_version} · ${dateText(item.created_at)}`} extra={<Button disabled={item.model_version !== viewVersion} onClick={() => setReportId(item.id)}>查看实际报告</Button>}>
            <ModelSource value={item} />{item.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
            <Paragraph>报告 {item.id} · 分类体系 {item.taxonomy_version} · 数据集 {item.dataset_hash} · 冻结测试 {item.test_split_hash}</Paragraph>
            {!item.available && <Alert type="warning" message={item.reason || '报告不可用'} />}
          </Card>)}</div> : <Alert type="warning" showIcon message="暂无可用评测报告" description="请确认模型版本，并查看运行概览中的阻塞原因。" />}
        </Card>
      </Space> },
    ]} />
    <Drawer rootClassName="staff-detail-drawer" open={!!sourceId} onClose={() => setSourceId(undefined)} width={820} title="主题脱敏原话与分类证据">{result.isError ? <QueryError error={result.error} /> : result.isLoading ? <Spin /> : result.data && <ResultEvidence item={result.data} />}</Drawer>
    <Drawer rootClassName="staff-detail-drawer" open={!!jobId} onClose={() => setJobId(undefined)} width={760} title="任务详情">{job.isError ? <QueryError error={job.error} /> : job.isLoading ? <Spin /> : job.data && <>
      <Paragraph>ID {job.data.id} · {labelText(job.data.state)} · 实际模型 {job.data.model_version ?? '未记录'} · 分类体系 {job.data.taxonomy_version}</Paragraph>
      <ModelSource value={job.data} />{job.data.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
      <Paragraph>创建 {dateText(job.data.created_at)} · 开始 {dateText(job.data.started_at)} · 结束 {dateText(job.data.finished_at)}</Paragraph>
      {job.data.result && <Paragraph>已处理 {job.data.result.processed ?? '未记录'} · 已持久化 {job.data.result.persisted ?? '未记录'} · 来源过期 {job.data.result.stale_source ?? '未记录'}；{job.data.result.next_cursor ? '有实际续跑游标' : '无实际续跑游标'}。</Paragraph>}
      <Collapse items={[
        { key: 'identity', label: '模型与数据集标识', children: <JsonEvidence value={{ artifact_identity: job.data.artifact_identity, dataset_identity: job.data.dataset_identity, dataset_hash: job.data.dataset_hash, split_hashes: job.data.split_hashes }} /> },
        { key: 'parameters', label: '任务参数', children: <JsonEvidence value={job.data.parameters} /> },
        { key: 'result', label: '处理结果与续跑信息', children: <JsonEvidence value={job.data.result} /> },
      ]} />
      {job.data.error && <Alert type="error" message={`${job.data.error.code}: ${job.data.error.message}`} />}
      {job.data.result?.next_cursor && <Button disabled={busy || !selectedVersion || job.data.model_key !== modelKey || job.data.model_version !== selectedVersion} onClick={() => resume(job.data!)}>继续同模型版本任务</Button>}
    </>}</Drawer>
    <Drawer rootClassName="staff-detail-drawer" open={!!reportId} onClose={() => setReportId(undefined)} width={1000} title="评测报告与结论">
      {report.isError ? <><QueryError error={report.error} /><Alert type="warning" message="尚无法形成分类质量结论" /></> : report.isLoading ? <Spin /> : report.data && report.data.report.model_version === viewVersion ? <>
        <Paragraph>报告 {report.data.id} · 生成 {dateText(report.data.created_at)}</Paragraph>
        <ModelSource value={report.data} />
        <Alert type="info" message="结论适用于报告版本；生产验收阈值尚未确认。" />
        <Card size="small" title="评测结论">
          <Title level={5}>分类质量与逐主题证据</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.quality}</Paragraph>
          <Title level={5}>测试覆盖与局限</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.coverage}</Paragraph>
          <Title level={5}>真实性能</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.performance}</Paragraph>
          <Title level={5}>阈值结论</Title><Paragraph>生产验收阈值尚待确认。</Paragraph>
        </Card>
        <ReportEvidence report={report.data.report} />
      </> : report.data && <Alert type="error" message="报告实际版本与当前查看版本不一致，未展示其结论。" />}
    </Drawer>
  </main></StaffLayout></TopicLabels.Provider>;
}
