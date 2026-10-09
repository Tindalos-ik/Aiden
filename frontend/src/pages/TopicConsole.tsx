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
const experimentWarning = '合成实验模型，仅作预标；合成评估不代表真实业务或生产验收。';
const modeText = (mode: string | null | undefined) => mode === 'human_reviewed' ? '人工审核数据' : mode === 'synthetic_experiment' ? '合成实验' : '来源未确认';
function ModelSource({ value }: { value: { model_key?: string | null; data_mode?: string | null; evaluation_source?: string | null } }) {
  return <Paragraph>执行键 {value.model_key ?? '未记录'} · 数据模式 {modeText(value.data_mode)} · 评估来源 {value.evaluation_source ?? '未确认'}</Paragraph>;
}
function JsonEvidence({ value }: { value: unknown }) { return <pre className="rag-result topic-evidence" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value == null ? '未记录' : JSON.stringify(value, null, 2)}</pre>; }
function QueryError({ error, retry }: { error: unknown; retry?: () => void }) { return <Alert type="error" showIcon message="真实 API 读取失败" description={<span style={{ whiteSpace: 'pre-wrap' }}>{errorText(error)}</span>} action={retry && <Button onClick={retry}>重试</Button>} />; }
function CountTable({ counts, title }: { counts: Record<string, number>; title: string }) {
  const labelText = useContext(TopicLabels);
  return <Table size="small" scroll={{ x: 420 }} pagination={false} rowKey="label" dataSource={Object.entries(counts).map(([label, count]) => ({ label, count }))} columns={[{ title, dataIndex: 'label', render: labelText }, { title: '真实计数', dataIndex: 'count' }]} />;
}
function Prediction({ item }: { item: TopicPrediction }) {
  const labelText = useContext(TopicLabels);
  return <Card size="small" title={<Space wrap><Tag color={item.current ? 'blue' : 'orange'}>{item.current ? '当前预测' : '历史 / 过期预测，不计入当前分类'}</Tag><Text>{labelText(item.status)}</Text></Space>}>
    <Paragraph>标签：{item.predicted_labels.map(labelText).join('、') || '无'}；模型 {item.model_version}；分类体系 {item.taxonomy_version}</Paragraph>
    <ModelSource value={item} />
    <Paragraph type="secondary">创建 {dateText(item.created_at)} · 更新 {dateText(item.updated_at)} · 输入哈希 {item.input_hash}</Paragraph>
    <Paragraph>以下为各标签独立 sigmoid 分数，不是校准后的分类正确概率；多标签分数无需加总为 1。</Paragraph>
    <Collapse size="small" items={[{ key: 'scores', label: '完整标签分数', children: <Table size="small" scroll={{ x: 420 }} pagination={false} rowKey="label" dataSource={Object.entries(item.scores).map(([label, score]) => ({ label, score }))} columns={[{ title: '标签', dataIndex: 'label', render: labelText }, { title: 'sigmoid 分数', dataIndex: 'score', render: (score: number) => score.toFixed(6) }]} /> }]} />
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
    {!item.current_prediction && <Alert type="warning" message="未分类：没有匹配当前输入、模型版本与分类体系的预测" />}
    {prediction && <Prediction item={prediction} />}
    {item.current_prediction && item.latest_prediction && item.latest_prediction.id !== item.current_prediction.id && <Prediction item={item.latest_prediction} />}
    <Card size="small" title="人工最终标注（独立、只读）">{human ? <><Tag color={human.current ? 'green' : 'orange'}>{human.current ? '当前人工标注' : '过期人工标注'}</Tag><Paragraph>状态：{labelText(human.status)}；标签：{human.labels.map(labelText).join('、') || '无'}；更新时间 {dateText(human.updated_at)}</Paragraph><Text type="secondary">标注 ID {human.id} · 输入哈希 {human.input_hash}</Text></> : <Text>尚无人工标注；不将模型预测冒充人工结论。</Text>}</Card>
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
    <Alert type="info" message="无测试支持的类别证据不足；null 表示未评估或不适用，不当作 0。历史报告不能证明当前模型质量。" />
    <Table size="small" pagination={false} rowKey="label" scroll={{ x: 650 }} dataSource={Object.entries(report.metrics.per_class).map(([label, values]) => ({ label, ...values }))} columns={[
      { title: '类别', dataIndex: 'label', render: labelText }, { title: 'Support', dataIndex: 'support' }, { title: '证据', dataIndex: 'evaluable', render: (value: boolean) => value ? '有测试支持' : '证据不足' },
      ...['precision', 'recall', 'f1'].map((key) => ({ title: key, dataIndex: key, render: metric })),
    ]} />
    <Collapse items={[
      { key: 'coverage', label: '样本覆盖 / 多标签原始证据', children: <JsonEvidence value={report.coverage} /> },
      { key: 'status', label: '状态混淆、标签混淆与修订需求', children: <><JsonEvidence value={report.status_metrics} /><Paragraph>上下文不足是独立状态；仅在编码器评测比较中映射为 uncertain，不等同于 other 或未分类。只有明确人工审核来源才能作为人工评估证据。</Paragraph></> },
      { key: 'resources', label: '真实资源与基线配置', children: <JsonEvidence value={{ baseline_configuration: report.baseline_configuration, resource_usage: report.resource_usage, usage: report.usage }} /> },
    ]} />
    <Card size="small" title="错误样例与分来源参考标签">{report.errors.length ? report.errors.map((item) => <Card size="small" key={item.id}>
      <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.redacted_text}</Paragraph>
      {report.data_mode === 'human_reviewed' ? <Paragraph>人工真值：{item.human_truth_labels?.map(labelText).join('、') || '未记录'}</Paragraph> : <Paragraph>人工真值：来源未确认或不是人工审核数据，不作为人工真值展示。</Paragraph>}
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
  const scopeControls = <Space wrap className="topic-filters">{dateControls}<Text>历史查看版本（不改变执行模型）</Text><Select aria-label="历史查看模型版本" allowClear disabled={!selectedVersion} placeholder={selectedVersion || '先选择执行模型并获取实际版本'} style={{ minWidth: 240 }} value={filters.model_version} onChange={(model_version) => update({ model_version })} options={[...new Set([...(selected?.model_versions ?? []), ...(registry.data?.model_versions ?? []), ...(selectedVersion ? [selectedVersion] : [])])].map((value) => ({ value, label: value }))} /></Space>;
  const renderJob = (item: TopicJob) => <Card size="small" key={item.id} title={<Space wrap><Text>{item.kind === 'batch' ? '有限批次分类' : '真实编码器评测'}</Text><Tag color={item.state === 'failed' || item.state === 'interrupted' ? 'red' : item.state === 'succeeded' ? 'green' : 'blue'}>{labelText(item.state)}</Tag></Space>} extra={<Button onClick={() => setJobId(item.id)}>任务详情</Button>}>
    <Paragraph>ID {item.id} · 实际模型 {item.model_version ?? '未记录'} · 分类体系 {item.taxonomy_version} · 创建 {dateText(item.created_at)}</Paragraph>
    <ModelSource value={item} />{item.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
    {item.result && <Paragraph>已处理 {item.result.processed ?? '未记录'} · 已持久化 {item.result.persisted ?? '未记录'} · 来源过期 {item.result.stale_source ?? '未记录'}；完整结果见任务详情。</Paragraph>}{item.error && <Alert type="error" message={`${item.error.code}: ${item.error.message}`} />}
    {item.result?.next_cursor && <Button disabled={busy || !selectedVersion || item.model_key !== modelKey || item.model_version !== selectedVersion} onClick={() => resume(item)}>同执行键与实际版本续跑</Button>}
    {item.report_id && <Button disabled={!selectedVersion || !item.model_version} onClick={() => { update({ model_version: item.model_version! }); setReportId(item.report_id!); setTab('reports'); }}>查看生成报告（任务版本）</Button>}
  </Card>;
  return <TopicLabels.Provider value={labelText}><StaffLayout actor={actor.data}><main className="rag-main topic-main">
    <div className="rag-heading"><div><Text className="section-kicker">SIDECAR TOPIC CLASSIFICATION</Text><Title level={2}>旁路主题分类</Title><Paragraph>低置信度原话的模型预测、人工真值与冻结测试证据。</Paragraph></div><Button icon={<ReloadOutlined />} disabled={!remote} onClick={() => void refresh()}>刷新服务端状态</Button></div>
    <Alert type="info" showIcon message="旁路主题分类用于低置信度问题分析，不影响在线客服回答、意图路由或工具授权。" />
    <Card size="small" className="staff-query" title="执行模型选择（与历史查看版本独立）">
      {registry.isLoading && <Spin />}{registry.isError && <QueryError error={registry.error} retry={() => void registry.refetch()} />}
      <Select aria-label="执行模型" allowClear placeholder="明确选择注册模型；不自动选择实验" style={{ width: '100%', maxWidth: 720 }} value={modelKey} disabled={!remote || busy || jobs.isPending || jobs.isError} options={(registry.data?.models ?? []).map((item) => ({ value: item.model_key, label: `${item.display_name} · ${item.model_key} · ${modeText(item.data_mode)}` }))} onChange={(key: string | undefined) => {
        setModelKey(key); setSelectionRevision((revision) => revision + 1); setFilters((old) => ({ ...old, model_version: undefined })); setPage(1); setCursor(undefined); setCursorIdentity(undefined); setSourceId(undefined); setReportId(undefined); setJobId(undefined); setActionError('');
      }} />
      <Paragraph>候选信息不是执行就绪证明；必须选择后由服务端加载并冻结校验。活动任务或启动请求期间不可切换。</Paragraph>
      <Collapse size="small" items={(registry.data?.models ?? []).map((item) => ({ key: item.model_key, label: `${item.display_name} · ${item.model_key} · ${modeText(item.data_mode)} · ${item.validated ? '已校验候选' : '尚未校验'}`, children: <><Paragraph>候选实际版本 {item.model_version ?? '未确认'} · 评估来源 {item.evaluation_source ?? '未确认'}</Paragraph><JsonEvidence value={item.training_summary} /></> }))} />
      {(registry.data?.models ?? []).filter((item) => item.blocked_reason).map((item) => <Alert key={item.model_key} type="warning" message={`${item.display_name} · ${item.blocked_reason!.code}: ${item.blocked_reason!.message}`} />)}
      <Paragraph>当前执行：{selectedCandidate?.display_name ?? modelKey ?? '尚未选择'} · 执行键 {modelKey ?? '无'} · 核验实际版本 {selectedVersion ?? '未确认'}（执行就绪另行检查）</Paragraph>
      {selected && <ModelSource value={{ ...selected, model_key: selected.selected_model_key }} />}
      {selectedCandidate && <><Collapse size="small" items={[{ key: 'training', label: '当前候选训练摘要（不表示执行就绪）', children: <JsonEvidence value={selectedCandidate.training_summary} /> }]} />{selectedCandidate.blocked_reason && <Alert type="warning" message={`${selectedCandidate.blocked_reason.code}: ${selectedCandidate.blocked_reason.message}`} />}</>}
    </Card>
    {(selectedCandidate?.data_mode === 'synthetic_experiment' || selected?.data_mode === 'synthetic_experiment') && <Alert type="warning" showIcon message={experimentWarning} />}
    {remote && !viewVersion && <Alert type="warning" showIcon message="尚未获取选定执行模型的实际版本：结果、统计和报告请求已禁用。" />}
    {!remote && <Alert type="warning" showIcon message="演示模式明确禁用真实分类与评测" description="不调用主题 API，不生成模拟报告，不回退模拟结果。切换 VITE_API_MODE=remote 后使用员工登录；模型、设备与数据集路径仅由服务端配置。" />}
    {actionError && <QueryError error={new Error(actionError)} />}
    {tab === 'stats' && remote && !statsScope.model_version && <Alert type="warning" showIcon message="统计阻塞：尚未确认明确选择的执行模型实际版本" description="不请求无版本统计，也不以历史版本或零填补缺失的执行身份。" />}
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'overview', label: '运行概览', children: <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {availability.isLoading && <Spin />}{availability.isError && <QueryError error={availability.error} retry={() => void availability.refetch()} />}
        <Card title="服务端可用性与精确阻塞条件"><Paragraph>选定模型 / model_version：{availability.data?.selected_model_version ?? '未确认'}；分类体系：{availability.data?.taxonomy.version ?? '未确认'}。模型与设备路径不接受浏览器输入。</Paragraph>{availability.data?.checks.map((check) => <Alert key={check.name} type={check.ready ? 'success' : 'warning'} showIcon message={`${check.name} · ${check.ready ? '通过' : '阻塞'} · ${check.code}`} description={check.message} />)}{!availability.data && <Text>未取得真实可用性；所有启动操作禁用。</Text>}</Card>
        {availability.data?.checks.filter((check) => check.missing_tables?.length || Object.keys(check.missing_columns ?? {}).length).map((check) => <Alert key={`${check.name}-schema`} type="error" message="数据库结构阻塞" description={`缺少表：${check.missing_tables?.join('、') || '未报告'}；缺少列：${Object.entries(check.missing_columns ?? {}).map(([table, columns]) => `${table}: ${columns.join('、')}`).join('；') || '未报告'}`} />)}
        <Card title="有限批次分类">
          <Paragraph>日期按浏览器本地日历日输入（包含首尾日期），转换为 UTC 来源创建时间半开区间。不填写日期则按 limit 有限处理；最多 5000 条，batch_size 最多 500，不做无限循环。启动返回真实任务 ID，不显示估算进度。</Paragraph>
          <Space wrap className="topic-filters">{dateControls}<Text>执行实际版本：{selectedVersion ?? '未确认'}（不使用历史查看筛选）</Text></Space>
          <Space wrap><Text>limit</Text><InputNumber aria-label="批次总条数" min={1} max={5000} value={limit} onChange={(value) => value != null && setLimit(value)} /><Text>batch_size</Text><InputNumber aria-label="模型批大小" min={1} max={500} value={batchSize} onChange={(value) => value != null && setBatchSize(value)} />
            <Button type="primary" loading={mutation.isPending} disabled={!remote || !modelKey || !selectedVersion || !selected?.can_classify || !cursorMatches || busy || jobs.isError || jobs.isPending} onClick={() => modelKey && mutation.mutate({ model_key: modelKey, start_at: filters.start_at, end_at: filters.end_at, limit, batch_size: batchSize, after: cursor })}>启动旁路分类</Button>
          </Space>
          {cursor && <><Paragraph>实际续跑游标（仅适用于执行键 {cursorIdentity?.model_key} 与实际版本 {cursorIdentity?.model_version}）</Paragraph><JsonEvidence value={cursor} />{!cursorMatches && <Alert type="warning" message="实际模型版本已变化，禁止使用此游标续跑；请清除游标。" />}<Button onClick={() => { setCursor(undefined); setCursorIdentity(undefined); }}>清除续跑游标</Button></>}
          {busy && <Alert type="info" message="已有活动任务，重复启动已禁用；跨客户端冲突由服务端 409 拒绝。" />}
        </Card>
        <Card title="服务端任务历史（刷新后恢复）">{jobs.isError ? <QueryError error={jobs.error} /> : jobs.isLoading ? <Spin /> : jobs.data?.items.length ? <div className="rag-stack">{jobs.data.items.map(renderJob)}</div> : <Empty description={remote ? '服务端尚无任务' : '演示模式未读取真实任务'} />}</Card>
      </Space> },
      { key: 'results', label: '分类结果', children: <Card title="原始来源与当前 / 历史预测（只读）">{scopeControls}<Space wrap className="topic-filters"><Select aria-label="主题标签" allowClear placeholder="主题标签" value={filters.topic} onChange={(topic) => update({ topic })} style={{ width: 180 }} options={(availability.data?.taxonomy.labels ?? []).map((item) => ({ value: item.id, label: item.display }))} /><Select aria-label="分类状态" allowClear placeholder="classification_status" style={{ width: 235 }} value={filters.status} onChange={(status) => update({ status })} options={['predicted', 'uncertain', 'unclassified'].map((value) => ({ value, label: labelText(value) }))} /><Select aria-label="是否有当前预测" allowClear placeholder="has_current_prediction" style={{ width: 205 }} value={filters.current_prediction} onChange={(current_prediction) => update({ current_prediction })} options={[{ value: true, label: '有当前预测' }, { value: false, label: '无当前预测' }]} /><Select aria-label="是否有人工标注" allowClear placeholder="has_human" style={{ width: 160 }} value={filters.has_human} onChange={(has_human) => update({ has_human })} options={[{ value: true, label: '有当前人工标注' }, { value: false, label: '无当前人工标注' }]} /></Space><Paragraph>未分类＝无当前预测；不确定＝编码器 uncertain；上下文不足＝独立人工 insufficient_context；其他＝业务标签 other，四者不混用。过期预测不计作当前。问题正文只来自服务端主题脱敏字段。</Paragraph>{results.isError ? <QueryError error={results.error} /> : results.isLoading ? <Spin /> : <><div className="rag-stack">{results.data?.items.map((item) => <Card size="small" key={item.source_id} title={<Space wrap><Text>{item.source_id}</Text><Tag>{item.current_prediction ? labelText(item.current_prediction.status) : labelText('unclassified')}</Tag>{!item.current_prediction && item.latest_prediction && <Tag color="orange">有过期预测</Tag>}</Space>} extra={<Button onClick={() => setSourceId(item.source_id)}>查看全部分数与人工真值</Button>}><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.question}</Paragraph><Text type="secondary">{dateText(item.created_at)} · 当前标签 {item.current_prediction?.predicted_labels.map(labelText).join('、') || '无'}</Text></Card>)}</div>{results.data?.total === 0 && <Empty description="当前范围没有来源记录" />}{results.data && <Pagination current={page} pageSize={20} total={results.data.total} showSizeChanger={false} onChange={setPage} />}</>}</Card> },
      { key: 'stats', label: '主题统计', children: <Card title="当前输入 / 单一模型 / 当前分类体系统计">
        {scopeControls}<Alert type="info" showIcon message="统计仅应用时间范围和模型版本；分类结果页的标签、状态、当前预测、人工标注筛选不应用于统计。多标签计数之和可大于来源数。" />
        <Paragraph>应用范围：{filters.start_at || '无开始限制'} 至 {filters.end_at || '无结束限制'}（UTC 半开区间）；查看单模型 {viewVersion ?? '未确认'}；分类体系 {stats.data?.taxonomy_version ?? '未确认'}。</Paragraph>
        {stats.isError ? <QueryError error={stats.error} /> : stats.isLoading ? <Spin /> : stats.data && viewVersion ? <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <ModelSource value={stats.data} />{stats.data.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
          <Descriptions bordered items={[{ key: 'sources', label: '唯一原始来源', children: stats.data.source_unique_count }, { key: 'current', label: '当前模型预测', children: stats.data.model.source_count }, { key: 'unclassified', label: '未分类', children: stats.data.model.unclassified_count }, { key: 'uncertain', label: '不确定', children: stats.data.model.status_counts.uncertain ?? '未返回该状态计数' }, { key: 'human', label: '当前人工标注', children: stats.data.human.source_count }, { key: 'review', label: '归并审核去重数', children: stats.data.review_unique_count }, { key: 'lifetime', label: '关联审核全生命周期出现数（非本范围）', children: stats.data.linked_reviews_lifetime_occurrence_count }]} />
          <CountTable counts={stats.data.model.label_counts} title="模型主题分布" /><CountTable counts={stats.data.model.status_counts} title="模型状态" /><CountTable counts={stats.data.human.label_counts} title="独立人工主题分布" /><CountTable counts={stats.data.human.status_counts} title="独立人工状态" /><CountTable counts={stats.data.merged_review_label_counts.model} title="归并审核去重 · 模型标签" /><CountTable counts={stats.data.merged_review_label_counts.human} title="归并审核去重 · 人工标签" />
        </Space> : <Text>真实统计未取得，不以零填补。</Text>}
      </Card> },
      { key: 'reports', label: '评测报告', children: <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Card title="选定执行模型的冻结测试集评测">
          <Paragraph>仅运行明确选择的执行键 {modelKey ?? '未选择'}、核验实际版本 {selectedVersion ?? '未确认'} 及其服务端绑定的冻结数据集；不使用历史查看版本。数据模式 {modeText(selected?.data_mode)}，评估来源 {selected?.evaluation_source ?? '未确认'}。无浏览器路径、设备配置或 LLM 评审入口。当前可评测：{selected?.can_evaluate ? '是' : '否 / 未确认'}。</Paragraph>
          <Button type="primary" disabled={!remote || !modelKey || !selectedVersion || !selected?.can_evaluate || busy || jobs.isError || jobs.isPending} loading={mutation.isPending} onClick={() => modelKey && mutation.mutate({ kind: 'evaluation', model_key: modelKey, batch_size: batchSize })}>启动真实编码器评测</Button>
          {selected?.checks.filter((check) => !check.ready).map((check) => <Alert key={check.name} type="warning" message={`${check.name} · ${check.code}`} description={check.message} />)}
        </Card>
        <Card title="实际报告列表（按历史查看版本隔离）">
          {scopeControls}<Paragraph>仅查看实际版本 {viewVersion ?? '未确认'} 的报告，历史报告不代表当前执行模型质量。</Paragraph>
          {reports.isError ? <QueryError error={reports.error} /> : reports.isLoading ? <Spin /> : reports.data?.items.length && viewVersion ? <div className="rag-stack">{reports.data.items.map((item) => <Card size="small" key={item.id} title={`${item.model_version} · ${dateText(item.created_at)}`} extra={<Button disabled={item.model_version !== viewVersion} onClick={() => setReportId(item.id)}>查看实际报告</Button>}>
            <ModelSource value={item} />{item.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
            <Paragraph>报告 {item.id} · 分类体系 {item.taxonomy_version} · 数据集 {item.dataset_hash} · 冻结测试 {item.test_split_hash}</Paragraph>
            {!item.available && <Alert type="warning" message={item.reason || '报告不可用'} />}<Text type="secondary">来源未确认的报告不标记为人工审核证据；不推断生产就绪。</Text>
          </Card>)}</div> : <Alert type="warning" showIcon message="尚无法形成分类质量结论" description="当前查看版本没有可读取的真实报告，或尚未确认选定模型的实际版本；检查运行概览中的精确阻塞条件。" />}
        </Card>
      </Space> },
    ]} />
    <Drawer open={!!sourceId} onClose={() => setSourceId(undefined)} width={820} title="主题脱敏原话与分类证据">{result.isError ? <QueryError error={result.error} /> : result.isLoading ? <Spin /> : result.data && <ResultEvidence item={result.data} />}</Drawer>
    <Drawer open={!!jobId} onClose={() => setJobId(undefined)} width={760} title="服务端真实任务详情">{job.isError ? <QueryError error={job.error} /> : job.isLoading ? <Spin /> : job.data && <>
      <Paragraph>ID {job.data.id} · {labelText(job.data.state)} · 实际模型 {job.data.model_version ?? '未记录'} · 分类体系 {job.data.taxonomy_version}</Paragraph>
      <ModelSource value={job.data} />{job.data.data_mode === 'synthetic_experiment' && <Alert type="warning" message={experimentWarning} />}
      <Paragraph>创建 {dateText(job.data.created_at)} · 开始 {dateText(job.data.started_at)} · 结束 {dateText(job.data.finished_at)}</Paragraph>
      {job.data.result && <Paragraph>已处理 {job.data.result.processed ?? '未记录'} · 已持久化 {job.data.result.persisted ?? '未记录'} · 来源过期 {job.data.result.stale_source ?? '未记录'}；{job.data.result.next_cursor ? '有实际续跑游标' : '无实际续跑游标'}。</Paragraph>}
      <Collapse items={[
        { key: 'identity', label: '冻结执行身份（安全哈希，不含路径）', children: <JsonEvidence value={{ artifact_identity: job.data.artifact_identity, dataset_identity: job.data.dataset_identity, dataset_hash: job.data.dataset_hash, split_hashes: job.data.split_hashes }} /> },
        { key: 'parameters', label: '实际参数', children: <JsonEvidence value={job.data.parameters} /> },
        { key: 'result', label: '实际结果 / processed、persisted、stale_source、next_cursor', children: <JsonEvidence value={job.data.result} /> },
      ]} />
      {job.data.error && <Alert type="error" message={`${job.data.error.code}: ${job.data.error.message}`} />}
      {job.data.result?.next_cursor && <Button disabled={busy || !selectedVersion || job.data.model_key !== modelKey || job.data.model_version !== selectedVersion} onClick={() => resume(job.data!)}>同执行键与实际版本续跑</Button>}
    </>}</Drawer>
    <Drawer open={!!reportId} onClose={() => setReportId(undefined)} width={1000} title="真实报告与服务端确定性结论">
      {report.isError ? <><QueryError error={report.error} /><Alert type="warning" message="尚无法形成分类质量结论" /></> : report.isLoading ? <Spin /> : report.data && report.data.report.model_version === viewVersion ? <>
        <Paragraph>报告 {report.data.id} · 生成 {dateText(report.data.created_at)}</Paragraph>
        <ModelSource value={report.data} />
        <Alert type="info" message="历史报告独立证据，不代表当前模型；阈值仍未确认，不推断生产就绪。" />
        <Card size="small" title="服务端确定性结论（不由前端发明阈值）">
          <Title level={5}>分类质量与逐主题证据</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.quality}</Paragraph>
          <Title level={5}>测试覆盖与局限</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.coverage}</Paragraph>
          <Title level={5}>真实性能</Title><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{report.data.conclusions.performance}</Paragraph>
          <Title level={5}>阈值结论</Title><Paragraph>阈值尚未确认；不能据此宣称生产就绪。</Paragraph>
        </Card>
        <ReportEvidence report={report.data.report} />
      </> : report.data && <Alert type="error" message="报告实际版本与当前查看版本不一致，未展示其结论。" />}
    </Drawer>
  </main></StaffLayout></TopicLabels.Provider>;
}
