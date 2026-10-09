import { useMemo, useState } from 'react';
import { Alert, AutoComplete, Button, Card, DatePicker, Descriptions, Drawer, Empty, Input, Pagination, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { Dayjs } from 'dayjs';
import dayjs from 'dayjs';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '../components/Common';
import { api, apiMode } from '../api';
import { observabilityApi } from '../api/observability';
import type { ObservabilityCost, ObservabilityCostTrendPoint, ObservabilityDetail, ObservabilityFilters, ObservabilityObservation, ObservabilityTrace, ObservabilityTrendPoint } from '../types';

const { Text, Title, Paragraph } = Typography;
type WindowPreset = '1h' | '24h' | '7d' | '31d' | 'custom';
function relativeRange(value: Exclude<WindowPreset, 'custom'>, end = dayjs()) {
  const start = value === '31d' ? end.subtract(31, 'day') : end.subtract(value === '1h' ? 1 : value === '24h' ? 24 : 7, value === '7d' ? 'day' : 'hour');
  return { start: start.toISOString(), end: end.toISOString() };
}
const statusLabel = (status: string) => status === 'success' ? '正常完成' : status === 'error' ? '处理报错' : '未确认';
const dateText = (value: string | null | undefined, zone: string) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: zone, dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value)) : '未记录';
const numberText = (value: number | null | undefined) => value == null ? '未记录' : new Intl.NumberFormat('zh-CN').format(value);
const durationText = (value: number | null | undefined) => value == null ? '未记录' : `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value / 1000)} 秒`;
const currencyText = (value: string) => value === 'USD' ? '美元（USD）' : value;
const costText = (cost: ObservabilityCost) => cost.known_cost == null || cost.known_generations === 0
  ? `${currencyText(cost.currency)} 金额尚未记录（${cost.known_generations}/${cost.total_generations} 次调用有金额）`
  : `${cost.known_generations === cost.total_generations ? '' : '已知金额小计 '}${currencyText(cost.currency)} ${cost.known_cost.toLocaleString('zh-CN', { maximumFractionDigits: 8 })} · ${cost.completeness === 'complete' ? '完整' : cost.completeness === 'partial' ? '部分' : '覆盖未知'} (${cost.known_generations}/${cost.total_generations})`;
const tokenText = (value: { input: number | null; output: number | null; total: number | null }) => `输入 ${value.input == null ? '未知' : numberText(value.input)} / 输出 ${value.output == null ? '未知' : numberText(value.output)} / 合计 ${value.total == null ? '未知' : numberText(value.total)}`;
const stateText: Record<string, string> = { ready: '已读取', empty: '没有匹配记录', unconfigured: '未配置', configured_unverified: '已配置但尚未验证', auth_failed: '观测数据源访问权限失败', connection_failed: '连接失败', unsupported: '暂不支持查询' };
const stateHint: Record<string, string> = { unconfigured: '请管理员完成观测服务配置后重试。', configured_unverified: '配置已存在，但尚未确认可以读取数据。', auth_failed: '观测数据源的访问权限验证失败；请联系管理员检查服务接入权限。', connection_failed: '无法连接观测数据源；请管理员检查服务连接。', unsupported: '当前服务暂不支持此查询。' };
const intentText = (value: string) => ({
  logistics: '物流查询',
  order: '订单查询',
  product: '商品咨询',
  refund_return: '退款退货',
  after_sales: '售后服务',
  complaint: '投诉处理',
  smalltalk: '日常交流',
  human: '转人工',
  other: '其他问题',
  intent_classification: '共享意图分类',
  unattributed: '未归因',
} as Record<string, string>)[value] ?? '业务分类未识别';
const intentFilterValues = ['logistics', 'order', 'product', 'refund_return', 'after_sales', 'complaint', 'smalltalk', 'human', 'other'] as const;
const stepLabelByName: Record<string, string> = {
  aiden_support: '在线客服处理链路',
  load_context: '读取对话上下文',
  recognize_intent: '识别业务诉求',
  route_intent: '安排处理方式',
  dispatch_tool_call: '选择业务工具',
  generate: '生成客服回复',
  assess_knowledge: '核验知识依据',
  after_tools: '整理工具结果',
  finish_request: '整理本轮处理结果',
  save_answer: '保存客服回复',
  search_faq: '查询知识（整体）',
  run_search_faq: '查询知识（整体）',
  query_order: '查询订单',
  run_query_order: '查询订单',
  query_logistics: '查询物流',
  run_query_logistics: '查询物流',
  query_refund_policy: '查询售后政策',
  run_query_refund_policy: '查询售后政策',
  query_after_sale: '查询售后记录',
  run_query_after_sale: '查询售后记录',
  query_ticket: '查询工单进度',
  run_query_ticket: '查询工单进度',
  create_ticket: '登记工单',
  run_create_ticket: '登记工单',
  submit_after_sale: '提交售后申请',
  run_submit_after_sale: '提交售后申请',
  cost_bucket_intent_classification: '共享意图分类模型调用',
};
const stepText = (value: string) => {
  if (value.startsWith('cost_intent_')) return `意图模型调用：${intentText(value.slice('cost_intent_'.length))}`;
  return stepLabelByName[value] ?? '其他处理步骤';
};
const observationTypeText = (value: string) => value === 'GENERATION' ? '模型调用' : value === 'TOOL' ? '工具调用' : '处理步骤';
const rangeText = (scope: string) => scope === 'requested_range' ? '当前查询时间范围' : scope === 'configured_default' ? '服务默认时间范围' : '本次查询时间范围';
const sourceText = (value: string) => value === 'aiden_support' ? '在线客服' : value === 'other' ? '其他来源' : '来源未记录';
function Costs({ costs }: { costs: ObservabilityCost[] }) {
  if (!costs.length) return <Text type="secondary">金额尚未记录</Text>;
  return <Space direction="vertical" size={2}>{costs.map((cost) => <Text key={cost.currency}>{costText(cost)}</Text>)}</Space>;
}
function Usage({ value }: { value: ObservabilityObservation }) {
  const detailLabels: Record<string, string> = {
    input: '输入用量',
    output: '输出用量',
    total: '分项记录的合计',
    input_cached: '缓存输入用量',
    input_cache_read: '读取缓存输入用量',
    input_cache_creation: '写入缓存用量',
    output_reasoning: '推理用量',
  };
  const details = Object.entries(value.usage_details ?? {})
    .filter(([key, amount]) => detailLabels[key] && typeof amount === 'number')
    .map(([key, amount]) => ({ key, label: detailLabels[key], amount: amount as number }));
  return <Space direction="vertical" size={2}>
    <Text>{value.type === 'GENERATION' ? tokenText(value.tokens) : '此处理步骤没有模型用量。'}</Text>
    {value.type === 'GENERATION' && <Text type="secondary"><Costs costs={value.costs} /></Text>}
    {details.length > 0 && <>
      <Text strong>观测平台另有这些用量分项</Text>
      {details.map((item) => <Text type="secondary" key={item.key}>{item.label}：{numberText(item.amount)}</Text>)}
      <Text type="secondary">{value.usage_details_source?.includes('历史来源未核验')
        ? '这些历史分项来源尚未核验，可能已包含在上方汇总；不会重复计入合计。'
        : '这些分项可能已包含在上方汇总；不会重复计入合计。完整确认用量以页面汇总为准。'}</Text>
    </>}
  </Space>;
}
function Trend({ points }: { points: ObservabilityTrendPoint[] }) {
  if (!points.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此范围没有趋势数据" />;
  const max = Math.max(1, ...points.map((point) => point.roots));
  const chartWidth = Math.max(560, points.length * 72);
  return <div className="obs-trend-wrap">
    <svg className="obs-trend" viewBox={`0 0 ${chartWidth} 180`} role="img" aria-label="每日客服请求量趋势">
      <line x1="0" y1="150" x2={chartWidth} y2="150" stroke="#dce1eb" />
      {points.map((point, index) => {
        const x = 24 + index * 72;
        const height = point.roots / max * 120;
        return <g key={point.date}>
          <rect x={x} y={150 - height} width="30" height={height} rx="4" fill="#6672d8" />
          <text x={x + 15} y={Math.max(18, 145 - height)} textAnchor="middle" fontSize="11" fill="#454b63">{point.roots}</text>
          <text x={x + 15} y="170" textAnchor="middle" fontSize="10" fill="#747b8f">{point.date.slice(5)}</text>
        </g>;
      })}
    </svg>
    <div className="obs-trend-table">
      <Table
        size="small"
        pagination={false}
        rowKey="date"
        dataSource={points}
        columns={[
          { title: '统计日期（UTC）', dataIndex: 'date' },
          { title: '客服请求数', dataIndex: 'roots' },
          { title: '模型调用数', dataIndex: 'generations' },
          { title: '较慢请求参考值', render: (_, row) => durationText(row.duration.p95_ms) },
          { title: '模型用量（Token）', render: (_, row) => numberText(row.tokens.total) },
          { title: '已记录费用（按币种）', render: (_, row) => row.known_costs.length ? row.known_costs.map((cost) => cost.known_cost == null ? `${currencyText(cost.currency)} 金额尚未记录` : `${currencyText(cost.currency)} ${cost.known_cost.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}`).join(' · ') : '未记录' },
        ]}
      />
    </div>
  </div>;
}
function ObservationTree({ observations, onSelect }: { observations: ObservabilityObservation[]; onSelect: (item: ObservabilityObservation) => void }) {
  const { roots, orphaned } = useMemo(() => {
    const ids = new Set(observations.map((item) => item.id));
    const children = new Map<string, ObservabilityObservation[]>();
    const rootItems: ObservabilityObservation[] = [];
    const orphanItems: ObservabilityObservation[] = [];
    observations.forEach((item) => {
      if (!item.parent_id) rootItems.push(item);
      else if (!ids.has(item.parent_id)) orphanItems.push(item);
      else children.set(item.parent_id, [...(children.get(item.parent_id) ?? []), item]);
    });
    const walk = (item: ObservabilityObservation, ancestors: Set<string>): React.ReactNode => {
      if (ancestors.has(item.id)) return <div key={`cycle-${item.id}`} className="obs-tree-row">发现循环父子关系；已停止展开。</div>;
      const next = new Set(ancestors); next.add(item.id);
      return <div className="obs-tree-node" key={item.id}>
        <button className="obs-tree-row" onClick={() => onSelect(item)}>
          <span>{stepText(item.name)} · {observationTypeText(item.type)}</span>
          <Tag color={item.status === 'error' ? 'red' : item.status === 'success' ? 'green' : 'default'}>{statusLabel(item.status)}</Tag>
          <Text type="secondary">{item.model ?? observationTypeText(item.type)} · {durationText(item.duration_ms)}</Text>
        </button>
        {(children.get(item.id) ?? []).map((child) => walk(child, next))}
      </div>;
    };
    return { roots: rootItems.map((item) => walk(item, new Set())), orphaned: orphanItems.map((item) => walk(item, new Set())) };
  }, [observations, onSelect]);
  return <div className="obs-tree">{roots}{orphaned.length > 0 && <><Text strong>上级步骤未包含在本次记录中</Text>{orphaned}</>}{observations.length > 0 && roots.length === 0 && orphaned.length === 0 && <Text type="secondary">未能显示处理层级，请查看下方记录。</Text>}</div>;
}
function TraceDetails({ envelope, timezone, onSelect, selectedObservation, selectedObservationId }: { envelope: ObservabilityDetail; timezone: string; onSelect: (item: ObservabilityObservation) => void; selectedObservation?: ObservabilityObservation; selectedObservationId?: string }) {
  const result = envelope.data;
  return <Space direction="vertical" style={{ width: '100%' }} size="middle">
    <Alert
      type={envelope.state === 'ready' ? 'success' : envelope.state === 'empty' ? 'info' : 'warning'}
      message={`请求详情：${stateText[envelope.state] ?? '状态未知'}`}
      description={`${stateHint[envelope.state] ?? '已完成详情读取。'} 查询时间：${dateText(envelope.queried_at, timezone)}`}
    />
    {envelope.coverage.truncated && <Alert type="warning" message="详情记录不完整" description={`本次最多读取 ${envelope.coverage.limit} 条处理记录，以下层级仅供本批次追溯。`} />}
    {result && <>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="记录编号">{result.trace.id}</Descriptions.Item>
        <Descriptions.Item label="后台处理结果与耗时">{statusLabel(result.trace.status)} · {durationText(result.trace.duration_ms)}</Descriptions.Item>
        <Descriptions.Item label="开始时间">{dateText(result.trace.start_at, timezone)}</Descriptions.Item>
        <Descriptions.Item label="渠道 / 业务分类">{sourceText(result.trace.source)} · {result.trace.intents_recorded === false ? '历史分类未记录' : result.trace.intents.map(intentText).join('、') || '未记录'}</Descriptions.Item>
        <Descriptions.Item label="模型用量（Token）">{tokenText(result.trace.tokens)}</Descriptions.Item>
        <Descriptions.Item label="费用"><Costs costs={result.trace.costs} /></Descriptions.Item>
        <Descriptions.Item label="本次读取的处理记录">共 {numberText(result.structure.observations)} 条，其中模型调用 {numberText(result.structure.generations)} 次；上级步骤缺失 {numberText(result.structure.missing_parents ?? 0)} 条。</Descriptions.Item>
      </Descriptions>
      {selectedObservationId && !selectedObservation && <Alert type="warning" message="此步骤不在当前详情读取结果中" description="详情记录可能被截断；请在下方已读取的处理树中选择其他步骤。" />}
      <ObservationTree observations={result.observations} onSelect={onSelect} />
      {selectedObservation && <Card size="small" title={stepText(selectedObservation.name)}>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="处理类型">{observationTypeText(selectedObservation.type)}</Descriptions.Item>
          <Descriptions.Item label="记录编号 / 父记录编号">{selectedObservation.id} / {selectedObservation.parent_id ?? '无'}</Descriptions.Item>
          <Descriptions.Item label="处理结果">{statusLabel(selectedObservation.status)}</Descriptions.Item>
          <Descriptions.Item label="开始 / 结束时间">{dateText(selectedObservation.start_at, timezone)} / {dateText(selectedObservation.end_at, timezone)}</Descriptions.Item>
          <Descriptions.Item label="模型 / 耗时">{selectedObservation.model ?? '未记录'} / {selectedObservation.duration_ms == null ? '耗时未记录' : `${numberText(selectedObservation.duration_ms)} 毫秒`}</Descriptions.Item>
          <Descriptions.Item label="模型用量与费用"><Usage value={selectedObservation} /></Descriptions.Item>
        </Descriptions>
      </Card>}
    </>}
    {envelope.data === null && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={stateHint[envelope.state] ?? '此请求没有可展示的详情记录。'} />}
  </Space>;
}
export function Observability() {
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const timezone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', []);
  const [preset, setPreset] = useState<WindowPreset>('24h');
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [applied, setApplied] = useState<{ start: string; end: string }>(() => { const end = dayjs(); return { start: end.subtract(24, 'hour').toISOString(), end: end.toISOString() }; });
  const [model, setModel] = useState(''); const [intent, setIntent] = useState(''); const [status, setStatus] = useState<string>();
  const [conversationId, setConversationId] = useState(''); const [messageId, setMessageId] = useState(''); const [source, setSource] = useState('aiden_support');
  const [appliedFilters, setAppliedFilters] = useState<{ model: string; intent: string; status?: string; conversation_id: string; message_id: string; source: string }>({ model: '', intent: '', conversation_id: '', message_id: '', source: 'aiden_support' });
  const [page, setPage] = useState(1); const [selected, setSelected] = useState<string>(); const [selectedObservationId, setSelectedObservationId] = useState<string>();
  const [costModel, setCostModel] = useState('all'); const [costBucket, setCostBucket] = useState('all');
  const [rangeError, setRangeError] = useState('');
  const filters: ObservabilityFilters = useMemo(() => ({ start_at: applied.start, end_at: applied.end, source: appliedFilters.source, max_observations: 5000, model: appliedFilters.model || undefined, intent: appliedFilters.intent || undefined, status: appliedFilters.status as ObservabilityFilters['status'], conversation_id: appliedFilters.conversation_id || undefined, message_id: appliedFilters.message_id || undefined }), [applied, appliedFilters]);
  const remote = apiMode === 'remote';
  const overview = useQuery({ queryKey: ['observability', 'overview', filters], queryFn: () => observabilityApi.overview(filters), enabled: remote, retry: 0 });
  const traces = useQuery({ queryKey: ['observability', 'traces', filters, page], queryFn: () => observabilityApi.traces(filters, page), enabled: remote, retry: 0 });
  const detail = useQuery({ queryKey: ['observability', 'detail', selected, filters], queryFn: () => observabilityApi.detail(selected!, filters), enabled: remote && !!selected, retry: 0 });
  const overviewData = overview.data?.data;
  const selectedObservation = detail.data?.data?.observations.find((item) => item.id === selectedObservationId);
  const costGroups = useMemo(() => {
    const groups = new Map<string, { dimension: 'model' | 'intent'; label: string; currency: string; knownCost: number; knownGenerations: number; missingGenerations: number }>();
    (overviewData?.cost_trend ?? []).forEach((row) => {
      row.costs.forEach((cost) => {
        const keys: Array<['model' | 'intent', string]> = [['model', row.model], ['intent', row.bucket]];
        keys.forEach(([dimension, label]) => {
          const key = `${dimension}:${label}:${cost.currency}`;
          const current = groups.get(key) ?? { dimension, label, currency: cost.currency, knownCost: 0, knownGenerations: 0, missingGenerations: 0 };
          current.knownCost += cost.known_cost ?? 0;
          current.knownGenerations += cost.known_generations;
          current.missingGenerations += cost.total_generations - cost.known_generations;
          groups.set(key, current);
        });
      });
    });
    return [...groups.values()].sort((a, b) => a.currency.localeCompare(b.currency) || b.knownCost - a.knownCost);
  }, [overviewData?.cost_trend]);
  const costTrendRows = useMemo(() => (overviewData?.cost_trend ?? []).filter((row) => (costModel === 'all' || row.model === costModel) && (costBucket === 'all' || row.bucket === costBucket)), [overviewData?.cost_trend, costModel, costBucket]);
  const applyPreset = (value: WindowPreset) => { setPreset(value); setRangeError(''); if (value !== 'custom') { setApplied(relativeRange(value)); setPage(1); } };
  const refetchQueries = () => void Promise.all([overview.refetch(), traces.refetch(), ...(selected ? [detail.refetch()] : [])]);
  const applyCustom = () => {
    if (!customRange?.[0] || !customRange[1]) { setRangeError('请选择完整的起止时间。'); return; }
    const start = customRange[0]; const end = customRange[1];
    if (!start.isBefore(end) || end.diff(start, 'millisecond') > 31 * 24 * 60 * 60 * 1000) { setRangeError('起止时间必须递增，且范围不超过 31 天。'); return; }
    setRangeError('');
    const next = { start: start.toISOString(), end: end.toISOString() };
    if (next.start === applied.start && next.end === applied.end) refetchQueries();
    else setApplied(next);
    setPage(1);
  };
  const applyFilters = () => {
    const next = { model, intent, status, conversation_id: conversationId, message_id: messageId, source };
    const sameFilters = Object.keys(next).every((key) => next[key as keyof typeof next] === appliedFilters[key as keyof typeof appliedFilters]);
    if (preset === 'custom' && sameFilters) refetchQueries();
    else setAppliedFilters(next);
    if (preset !== 'custom') setApplied(relativeRange(preset));
    setPage(1);
  };
  const refresh = () => { if (preset !== 'custom') { setApplied(relativeRange(preset)); setPage(1); return; } refetchQueries(); };
  const clearOtherFilters = () => {
    const unchanged = !appliedFilters.model && !appliedFilters.intent && !appliedFilters.status
      && !appliedFilters.conversation_id && !appliedFilters.message_id;
    setModel(''); setIntent(''); setStatus(undefined); setConversationId(''); setMessageId('');
    setSource(appliedFilters.source);
    if (unchanged && page === 1) refetchQueries();
    else setAppliedFilters({ ...appliedFilters, model: '', intent: '', status: undefined, conversation_id: '', message_id: '' });
    setPage(1);
  };
  if (actor.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;
  const envelope = overview.data; const data = overview.isError ? undefined : overviewData; const list = traces.isError ? undefined : traces.data?.data;
  const knownCosts = (data?.costs ?? []).filter((cost) => cost.known_generations > 0);
  const missingCostCalls = data?.data_quality.cost_missing ?? 0;
  const feeSummary = knownCosts.length
    ? `${knownCosts.map((cost) => {
      const label = cost.completeness === 'complete' && missingCostCalls === 0 ? '' : '已知金额小计 ';
      const amount = cost.known_cost == null ? '金额小计未提供' : cost.known_cost.toLocaleString('zh-CN', { maximumFractionDigits: 8 });
      return `${label}${currencyText(cost.currency)} ${amount}（${cost.known_generations}/${cost.total_generations} 次调用有金额）`;
    }).join('；')}${missingCostCalls > 0 ? `；金额尚未记录（${missingCostCalls} 次调用）` : ''}`
    : missingCostCalls > 0 ? `金额尚未记录（${missingCostCalls} 次调用）`
      : data?.costs.length ? data.costs.map((cost) => `${currencyText(cost.currency)} 金额尚未记录`).join('；') : '未记录';
  const columns: ColumnsType<ObservabilityTrace> = [
    {
      title: '请求',
      render: (_, item) => <Button type="link" onClick={() => { setSelectedObservationId(undefined); setSelected(item.id); }}>
        {item.intents_recorded === false ? '历史业务分类未记录' : item.intents.map(intentText).join('、') || '未记录业务分类'}
      </Button>,
    },
    { title: '渠道', render: (_, item) => sourceText(item.source) },
    { title: '开始时间', dataIndex: 'start_at', render: (value) => dateText(value, timezone) },
    {
      title: '业务分类 / 模型',
      render: (_, item) => <Space direction="vertical" size={2}>
        {item.intents_recorded === false ? '历史业务分类未记录' : item.intents.map(intentText).join('、') || '未记录'}
        <Text type="secondary">{item.models.join('、') || '未记录'}</Text>
      </Space>,
    },
    { title: '后台处理结果', dataIndex: 'status', render: (value) => <Tag color={value === 'error' ? 'red' : value === 'success' ? 'green' : 'default'}>{statusLabel(value)}</Tag> },
    { title: '耗时', dataIndex: 'duration_ms', render: durationText },
    { title: '模型用量（Token）', render: (_, item) => tokenText(item.tokens) },
    { title: '费用', render: (_, item) => <Costs costs={item.costs} /> },
  ];
  return <div className="workspace-shell rag-shell observability-shell">
    <PageHeader actor={actor.data} />
    <main className="rag-main observability-main">
      <section className="rag-heading">
        <div>
          <Text type="secondary">员工工具 · 只读观测</Text>
          <Title level={2}>观测与成本</Title>
          <Paragraph type="secondary">查看客服请求、模型用量与已记录费用；不展示原始聊天内容。</Paragraph>
        </div>
        <Text type="secondary">显示时区：{timezone}</Text>
      </section>
    {!remote && <Alert type="warning" showIcon message="模拟模式不提供真实观测数据" description="此页不会填入模拟观测；切换到 remote 并使用已授权的员工账户。" />}
    <Button onClick={refresh} disabled={!remote} loading={overview.isFetching || traces.isFetching}>刷新数据</Button>
    <Card className="rag-card" title="查询范围">
      <Space wrap>
        <Select aria-label="时间范围" value={preset} onChange={applyPreset} options={[{ value: '1h', label: '最近 1 小时' }, { value: '24h', label: '最近 24 小时' }, { value: '7d', label: '最近 7 天' }, { value: '31d', label: '最近 31 天（最大范围）' }, { value: 'custom', label: '自定义' }]} />
        {preset === 'custom' && <><DatePicker.RangePicker showTime value={customRange} onChange={(value) => setCustomRange(value)} /><Button onClick={applyCustom}>应用时间</Button></>}
        <Select aria-label="数据源" value={source} onChange={setSource} options={[{ value: 'aiden_support', label: '仅在线客服（默认）' }, { value: 'other', label: '其他来源（与在线客服分开查询）' }]} />
        <AutoComplete
          aria-label="模型全名筛选"
          placeholder="模型全名（可输入）"
          allowClear
          value={model}
          onChange={setModel}
          options={(overviewData?.models ?? []).map((row) => ({ value: row.model }))}
          filterOption={(input, option) => String(option?.value ?? '').toLocaleLowerCase().includes(input.toLocaleLowerCase())}
        />
        <Select
          aria-label="业务分类筛选"
          placeholder="全部业务分类"
          allowClear
          value={intent || undefined}
          onChange={(value) => setIntent(value ?? '')}
          options={intentFilterValues.map((value) => ({ value, label: intentText(value) }))}
        />
        <Select aria-label="处理结果筛选" allowClear placeholder="全部处理结果" value={status} onChange={setStatus} options={[{ value: 'success', label: '正常完成' }, { value: 'error', label: '处理报错' }, { value: 'unknown', label: '未确认' }]} />
        <Input aria-label="会话编号" placeholder="会话编号" value={conversationId} onChange={(event) => setConversationId(event.target.value)} />
        <Input aria-label="消息编号" placeholder="消息编号" value={messageId} onChange={(event) => setMessageId(event.target.value)} />
        <Button type="primary" onClick={applyFilters}>应用筛选</Button>
      </Space>
      <Paragraph type="secondary" className="rag-hint">
        当前显示 {dateText(applied.start, timezone)} — {dateText(applied.end, timezone)}（{timezone}）。模型和业务分类筛选会选择符合条件的整条请求，并保留其中全部模型调用；只看某模型费用请用下方模型分组或费用趋势筛选。其他来源不会混入在线客服。
        {rangeError && <Text type="danger"> {rangeError}</Text>}
      </Paragraph>
    </Card>
    <Paragraph type="secondary">如无数据，可显式扩大到最近 7 天或 31 天、清除已应用筛选后点击“应用筛选”，或点击“刷新数据”；页面不会自行改变范围。</Paragraph>
    <Button onClick={clearOtherFilters}>清除其他筛选并查询</Button>
    {overview.isLoading && <Spin tip="正在读取观测概览…" />}
    {overview.isError && <Alert type="error" showIcon message="概览读取失败" description={overview.error instanceof Error ? overview.error.message : '请求失败'} />}
    {!overview.isError && overview.data?.state === 'ready' && <Alert type="success" showIcon message={`已读取真实观测数据 · 查询时间 ${dateText(overview.data.queried_at, timezone)}`} description={`查询来源：${sourceText(filters.source)}。以下数字来自当前查询范围的实际读取结果。`} />}
    {!overview.isError && overview.data?.state && !['ready', 'empty'].includes(overview.data.state) && <Alert type="warning" showIcon message={`观测读取状态：${stateText[overview.data.state] ?? '状态未知'}`} description={stateHint[overview.data.state] ?? '请管理员检查观测服务后重试。'} />}
    {!overview.isError && overview.data?.coverage.truncated && <Alert type="warning" showIcon message="结果不完整" description={`上游读取 ${overview.data.coverage.observations_read}/${overview.data.coverage.limit} 条记录；数字只代表已读取部分，不能代表完整时间范围。`} />}
    {traces.isError && <Alert type="error" showIcon message="请求列表读取失败" description={traces.error instanceof Error ? traces.error.message : '请求失败'} />}
    {!traces.isError && traces.data?.state && !['ready', 'empty'].includes(traces.data.state) && <Alert type="warning" showIcon message={`请求列表状态：${stateText[traces.data.state] ?? '状态未知'}`} description={stateHint[traces.data.state] ?? '请管理员检查观测服务后重试。'} />}
    {!overview.isError && overview.data?.state === 'empty' && <Alert
      type="info"
      showIcon
      message="当前查询未发现匹配请求"
      description={<Space direction="vertical">
        <Text>
          {overview.data.coverage.truncated
            ? '本次已读取记录中未找到匹配请求。由于读取结果不完整，不能据此判断完整查询范围。'
            : overview.data.coverage.empty_reason === 'source_excluded'
              ? `所选来源中没有匹配请求；另有 ${overview.data.coverage.excluded_by_source} 组其他来源请求未纳入。可手动切换来源，数据不会混算。`
              : overview.data.coverage.empty_reason === 'filters_excluded'
                ? `${overview.data.coverage.source_matched_traces} 条来源请求被其他筛选条件排除（${overview.data.coverage.excluded_by_filters} 条）。`
                : overview.data.coverage.empty_reason === 'no_observations'
                  ? `查询已完成，但在 ${sourceText(filters.source)} 中没有读取到记录。`
                  : '本次已读取记录中未找到匹配请求。'}
        </Text>
        <Text>当前来源为“{sourceText(filters.source)}”；切换来源需要您手动选择，不会自动改为其他来源。</Text>
        <Space wrap>
          <Button onClick={() => applyPreset('1h')}>缩小到最近 1 小时</Button>
          <Button onClick={() => applyPreset('24h')}>查看最近 24 小时</Button>
          <Button onClick={() => applyPreset('7d')}>扩大到最近 7 天</Button>
          <Button onClick={() => applyPreset('31d')}>扩大到最近 31 天</Button>
          <Button onClick={clearOtherFilters}>清除其他筛选并查询</Button>
        </Space>
      </Space>}
    />}
    {traces.data?.coverage.truncated && <Alert type="warning" showIcon message="请求列表不完整" description={`读取 ${traces.data.coverage.observations_read}/${traces.data.coverage.limit} 条记录。`} />}
    {data && <>
      <section className="obs-counts">
        <Card className="rag-stat"><Statistic title="客服请求数" value={data.counts.roots} /></Card>
        <Card className="rag-stat"><Statistic title="模型调用次数" value={data.counts.generations} /></Card>
        <Card className="rag-stat"><Statistic title="后台处理：正常完成 / 处理报错 / 未确认" value={`${data.counts.success} / ${data.counts.error} / ${data.counts.unknown}`} /></Card>
        <Card className="rag-stat">
          <Statistic title="请求平均耗时" value={durationText(data.duration.mean_ms)} />
          <Text type="secondary">基于 {numberText(data.duration.samples)} 条已记录耗时 · 中位耗时 {durationText(data.duration.p50_ms)} · 较慢请求参考 {durationText(data.duration.p95_ms)}</Text>
        </Card>
      </section>
      <Paragraph type="secondary">这里的结果只表示后台客服处理是否正常完成，不代表订单问题已解决或退款已到账。</Paragraph>
      <section className="obs-counts">
        <Card className="rag-stat">
          <Statistic title="模型用量（Token）" value={data.tokens.total == null ? '未记录' : numberText(data.tokens.total)} />
          <Text type="secondary">Token 是模型处理文本的计量单位，不等同于字数；用量按已确认字段分别汇总，完整覆盖要求输入、输出和合计均有记录。缺失字段不补算，历史未核验数据不计入已确认汇总。完整覆盖：{data.tokens.known_generations}/{data.tokens.total_generations} 次。</Text>
        </Card>
        <Card className="rag-stat">
          <Statistic title="已记录费用" value={feeSummary} />
          <Text type="secondary">仅统计观测平台确认记录的金额，不等同结算账单；金额缺失不会按 0 计算。</Text>
        </Card>
      </section>
      <Paragraph type="secondary">此处费用汇总包含当前筛选请求中的全部模型；按模型查看请用下方分组或费用趋势筛选。不同币种分开比较。</Paragraph>
    <Card className="rag-card" title="本次查询结论">
      <Paragraph>{data.insights.facts.join(' ')}</Paragraph>
      {data.insights.limitations.map((item, index) => <Paragraph type="secondary" key={`limit-${index}`}>{item}</Paragraph>)}
      <Paragraph type="secondary">本页未计入本地算力等其他成本。</Paragraph>
      {data.data_quality.cost_missing > 0 && <Alert
        type="warning"
        showIcon
        message={`${data.data_quality.cost_missing} 次模型调用没有记录到费用金额`}
        description="这表示观测数据没有金额，无法据此判断是否实际收费。请管理员检查金额记录是否正常写入及计价配置；缺失金额不会按 0 计算。"
      />}
      <Paragraph strong>费用贡献（只按已知金额比较）</Paragraph>
      <Table
        size="small"
        rowKey={(row) => `${row.dimension}:${row.label}:${row.currency}`}
        pagination={false}
        dataSource={costGroups.filter((row) => row.knownGenerations > 0)}
        locale={{ emptyText: '当前没有可比较的已记录费用；缺失金额无法用于排序。' }}
        columns={[
          { title: '分组', render: (_, row) => `${row.dimension === 'model' ? '模型' : '业务分类'}：${row.dimension === 'intent' ? intentText(row.label) : row.label}` },
          { title: '币种', render: (_, row) => currencyText(row.currency) },
          { title: '已知金额小计', render: (_, row) => `${currencyText(row.currency)} ${row.knownCost.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}` },
          { title: '有金额的调用数', dataIndex: 'knownGenerations' },
          { title: '未记录金额的调用数', dataIndex: 'missingGenerations' },
        ]}
      />
    </Card>
    <Card className="rag-card" title="模型与业务分类用量"><Space direction="vertical" style={{ width: '100%' }}><Table size="small" rowKey="model" pagination={false} dataSource={data.models} locale={{ emptyText: '当前没有模型调用记录' }} columns={[{ title: '模型', dataIndex: 'model' }, { title: '调用数', dataIndex: 'generations' }, { title: '模型用量（Token）', render: (_, row) => tokenText(row.tokens) }, { title: '费用', render: (_, row) => <Costs costs={row.costs} /> }]} /><Table size="small" rowKey="bucket" pagination={false} dataSource={data.attribution} locale={{ emptyText: '当前没有业务分类记录' }} columns={[{ title: '业务分类', dataIndex: 'bucket', render: intentText }, { title: '调用数', dataIndex: 'generations' }, { title: '模型用量（Token）', render: (_, row) => tokenText(row.tokens) }, { title: '费用', render: (_, row) => <Costs costs={row.costs} /> }]} /></Space></Card>
    <Card className="rag-card" title="较慢的请求与步骤">
      <Paragraph type="secondary">展示已记录的较慢处理步骤。步骤按真实父子关系追溯；并行处理不表示先后顺序。点击步骤后会打开所属请求详情并定位到该步骤。</Paragraph>
      <Table
        size="small"
        rowKey="trace_id"
        pagination={false}
        dataSource={data.slow_items.requests}
        locale={{ emptyText: '当前范围没有可比较的慢请求。' }}
        columns={[
          { title: '开始时间', render: (_, row) => dateText(row.start_at, timezone) },
          { title: '总耗时', render: (_, row) => `${numberText(row.duration_ms)} 毫秒` },
          { title: '业务分类', render: (_, row) => row.intents.map(intentText).join('、') || '未记录' },
          { title: '模型', render: (_, row) => row.models.join('、') || '未记录' },
          { title: '详情', render: (_, row) => <Button type="link" onClick={() => { setSelectedObservationId(undefined); setSelected(row.trace_id); }}>打开请求详情</Button> },
        ]}
      />
      <Table
        size="small"
        rowKey="observation_id"
        pagination={false}
        dataSource={data.slow_items.steps}
        locale={{ emptyText: '当前范围没有可单独计时的处理步骤。' }}
        columns={[
          { title: '处理步骤', render: (_, row) => stepText(row.name) },
          { title: '步骤类别', render: (_, row) => observationTypeText(row.type) },
          { title: '耗时', render: (_, row) => `${numberText(row.duration_ms)} 毫秒` },
          { title: '模型', render: (_, row) => row.model ?? '未记录' },
          { title: '详情', render: (_, row) => <Button type="link" onClick={() => { setSelected(row.trace_id); setSelectedObservationId(row.observation_id); }}>定位此步骤</Button> },
        ]}
      />
    </Card>
    <Card className="rag-card" title="每日处理趋势"><Trend points={data.trend} /></Card></>}
    {data && <Card className="rag-card" title="数据完整情况">
      <Paragraph type="secondary">这里只表示后台处理耗时；用户端总等待时间、首字响应时间未采集。知识查询只记录整体耗时，内部阶段未分别记录。</Paragraph>
      <Descriptions column={{ xs: 1, md: 2 }} size="small">
        <Descriptions.Item label="模型调用总数">{numberText(data.data_quality.generations)}</Descriptions.Item>
        <Descriptions.Item label="来源已核验的完整用量">{numberText(data.data_quality.provider_verified)} / {numberText(data.data_quality.generations)} 次</Descriptions.Item>
        <Descriptions.Item label="没有用量记录">{numberText(data.data_quality.usage_unrecorded)}</Descriptions.Item>
        <Descriptions.Item label="用量已记录但来源未核验">{numberText(data.data_quality.usage_unverified)}</Descriptions.Item>
        <Descriptions.Item label="已记录金额的调用">{numberText(data.data_quality.cost_recorded)}</Descriptions.Item>
        <Descriptions.Item label="尚无金额记录的调用">{numberText(data.data_quality.cost_missing)}</Descriptions.Item>
      </Descriptions>
    </Card>}
    {data && <Card className="rag-card obs-cost-trend" title="每日模型与业务费用">
      <Paragraph type="secondary">按模型、业务分类和 UTC 日期查看调用量及已知金额；详情时间按页面时区显示。共享意图分类单独列出。此处筛选只改变本表，不改变上方请求总览；金额按币种分别比较，未记录金额不当作 0。</Paragraph>
      <Space wrap className="obs-cost-filters">
        <Select aria-label="费用趋势模型" value={costModel} onChange={setCostModel} options={[{ value: 'all', label: '全部模型' }, ...Array.from(new Set((data.cost_trend ?? []).map((row) => row.model))).map((model) => ({ value: model, label: model }))]} />
        <Select aria-label="费用趋势业务分类" value={costBucket} onChange={setCostBucket} options={[{ value: 'all', label: '全部业务分类' }, ...Array.from(new Set((data.cost_trend ?? []).map((row) => row.bucket))).map((bucket) => ({ value: bucket, label: intentText(bucket) }))]} />
      </Space>
      {!Array.isArray(data.cost_trend) && <Alert type="warning" showIcon message="暂时没有模型与业务分类费用分组" description="本页不会根据总额推算、复制或伪造分组金额。" />}
      {Array.isArray(data.cost_trend) && data.cost_trend.length === 0 && <Text type="secondary">此范围没有模型费用趋势记录。</Text>}
      {costTrendRows.length > 0 && <Table<ObservabilityCostTrendPoint>
        rowKey={(row) => `${row.date}:${row.model}:${row.bucket}`}
        dataSource={costTrendRows}
        pagination={false}
        scroll={{ x: 900 }}
        columns={[
          { title: '统计日期（UTC）', dataIndex: 'date', render: (value: string) => value === 'unknown' ? '日期未记录' : value },
          { title: '模型', dataIndex: 'model' },
          { title: '业务分类', dataIndex: 'bucket', render: intentText },
          { title: '模型调用数', dataIndex: 'generations' },
          { title: '模型用量（Token）', render: (_, row) => tokenText(row.tokens) },
          { title: '金额记录', render: (_, row) => <Costs costs={row.costs} /> },
        ]}
      />}
    </Card>}
    <Card className="rag-card" title="处理记录与请求列表">
      <Paragraph type="secondary">列表展示当前范围内已读取的客服请求；更细的处理步骤仅按实际记录显示，不推测未采集的耗时。</Paragraph>
      <Table
        rowKey="id"
        columns={columns}
        dataSource={list?.items ?? []}
        loading={traces.isLoading}
        pagination={false}
        scroll={{ x: 1200 }}
        locale={{ emptyText: traces.isLoading ? '正在读取请求…' : traces.isError ? '请求列表读取失败。请查看上方提示并手动刷新。' : traces.data?.state === 'empty' ? '本次查询没有匹配请求。' : traces.data && traces.data.state !== 'ready' ? stateHint[traces.data.state] ?? '当前无法读取请求列表。' : '暂无请求记录。' }}
      />
      <Pagination current={list?.page ?? page} pageSize={list?.page_size ?? 20} total={list?.total ?? 0} onChange={(next) => setPage(next)} showSizeChanger={false} />
    </Card>
    <Card className="rag-card" title="数据状态与覆盖">
      <Descriptions column={{ xs: 1, md: 2 }} size="small">
        <Descriptions.Item label="读取状态">{overview.isError ? '读取失败' : stateText[envelope?.state ?? ''] ?? '等待读取'}</Descriptions.Item>
        <Descriptions.Item label="最近查询">{dateText(envelope?.queried_at, timezone)}</Descriptions.Item>
        <Descriptions.Item label="最新记录">{dateText(envelope?.latest_record_at, timezone)}</Descriptions.Item>
        <Descriptions.Item label="查询时间范围">{envelope?.coverage ? `${rangeText(envelope.coverage.scope)}：${dateText(envelope.coverage.start_at, timezone)} 至 ${dateText(envelope.coverage.end_at, timezone)}` : '未读取'}</Descriptions.Item>
        <Descriptions.Item label="已读取处理步骤">{envelope?.coverage ? `${numberText(envelope.coverage.observations_read)} / ${numberText(envelope.coverage.limit)} 条上限` : '未读取'}</Descriptions.Item>
        <Descriptions.Item label="匹配处理步骤">{numberText(envelope?.coverage?.matched_observations)}</Descriptions.Item>
        <Descriptions.Item label="模型调用（读取 / 匹配）">{envelope?.coverage ? `${numberText(envelope.coverage.generations_read)} / ${numberText(envelope.coverage.matched_generations)}` : '未读取'}</Descriptions.Item>
        <Descriptions.Item label="匹配客服请求">{numberText(envelope?.coverage?.matched_traces)}</Descriptions.Item>
        <Descriptions.Item label="当前来源请求">{numberText(envelope?.coverage?.source_matched_traces)}</Descriptions.Item>
        <Descriptions.Item label="被来源条件排除">{numberText(envelope?.coverage?.excluded_by_source)}</Descriptions.Item>
        <Descriptions.Item label="被其他筛选排除">{numberText(envelope?.coverage?.excluded_by_filters)}</Descriptions.Item>
        <Descriptions.Item label="读取完整性">{envelope?.coverage ? envelope.coverage.truncated ? '不完整，仅代表已读取部分' : '本次读取完成' : '未读取'}</Descriptions.Item>
      </Descriptions>
    </Card>
    <Drawer title="请求详情" width={760} open={!!selected} onClose={() => { setSelected(undefined); setSelectedObservationId(undefined); }}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {detail.isLoading && <Spin tip="正在读取请求详情…" />}
        {detail.isError && <Alert type="error" message="详情读取失败" description={detail.error instanceof Error ? detail.error.message : '请求失败，请手动重试。'} />}
        {detail.data && <TraceDetails envelope={detail.data} timezone={timezone} onSelect={(item) => setSelectedObservationId(item.id)} selectedObservation={selectedObservation} selectedObservationId={selectedObservationId} />}
        {selected && !remote && <Alert type="warning" message="演示模式不提供详情数据" />}
      </Space>
    </Drawer>
  </main></div>;
}
