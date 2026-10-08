import { useMemo, useState } from 'react';
import { Alert, Button, Card, DatePicker, Descriptions, Drawer, Empty, Input, Pagination, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
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
const statusLabel = (status: string) => status === 'success' ? '成功' : status === 'error' ? '错误' : '未知';
const dateText = (value: string | null | undefined, zone: string) => value ? new Intl.DateTimeFormat('zh-CN', { timeZone: zone, dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value)) : '未记录';
const numberText = (value: number | null | undefined) => value == null ? '未记录' : new Intl.NumberFormat('zh-CN').format(value);
const costText = (cost: ObservabilityCost) => cost.known_cost == null || cost.known_generations === 0
  ? `${cost.currency} 费用未知（${cost.known_generations}/${cost.total_generations} generations 有已知金额）`
  : `${cost.known_generations === cost.total_generations ? '' : '已知小计 '}${cost.currency} ${cost.known_cost.toLocaleString('zh-CN', { maximumFractionDigits: 8 })} · ${cost.completeness === 'complete' ? '完整' : cost.completeness === 'partial' ? '部分' : '覆盖未知'} (${cost.known_generations}/${cost.total_generations})`;
const tokenText = (value: { input: number | null; output: number | null; total: number | null }) => `输入 ${value.input == null ? '未知' : numberText(value.input)} / 输出 ${value.output == null ? '未知' : numberText(value.output)} / 合计 ${value.total == null ? '未知' : numberText(value.total)}`;
function Costs({ costs }: { costs: ObservabilityCost[] }) { return costs.length ? <Space direction="vertical" size={2}>{costs.map((cost) => <Text key={cost.currency}>{costText(cost)} · {cost.source}</Text>)}</Space> : <Text type="secondary">未记录费用</Text>; }
function Usage({ value }: { value: ObservabilityObservation }) { return <Space direction="vertical" size={2}><Text>{value.type === 'GENERATION' ? tokenText(value.tokens) : '非 generation observation；不适用模型 token 用量'}</Text>{value.type === 'GENERATION' && <Text type="secondary">费用：<Costs costs={value.costs} /></Text>}{value.usage_details && <Text type="secondary">{value.usage_details_source === 'recorded_usage_unverified' ? 'Langfuse 记录值，来源未核验' : value.usage_details_source ?? 'Langfuse usage 记录'}：{Object.entries(value.usage_details).map(([key, amount]) => `${key} ${amount}`).join(' · ')}</Text>}{value.cost_details && <Text type="secondary">cost details：{Object.entries(value.cost_details).map(([key, amount]) => `${key} ${amount}`).join(' · ')}</Text>}</Space>; }
function Trend({ points }: { points: ObservabilityTrendPoint[] }) {
  if (!points.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此范围无趋势数据" />;
  const max = Math.max(1, ...points.map((point) => point.roots));
  return <div className="obs-trend-wrap"><svg className="obs-trend" viewBox={`0 0 ${Math.max(560, points.length * 72)} 180`} role="img" aria-label="按日根请求量趋势"><line x1="0" y1="150" x2={Math.max(560, points.length * 72)} y2="150" stroke="#dce1eb" />{points.map((point, index) => { const x = 24 + index * 72; const height = point.roots / max * 120; return <g key={point.date}><rect x={x} y={150 - height} width="30" height={height} rx="4" fill="#6672d8" /><text x={x + 15} y={Math.max(18, 145 - height)} textAnchor="middle" fontSize="11" fill="#454b63">{point.roots}</text><text x={x + 15} y="170" textAnchor="middle" fontSize="10" fill="#747b8f">{point.date.slice(5)}</text></g>; })}</svg><div className="obs-trend-table"><Table size="small" pagination={false} rowKey="date" dataSource={points} columns={[{ title: '时间', dataIndex: 'date' }, { title: '根请求', dataIndex: 'roots' }, { title: '生成', dataIndex: 'generations' }, { title: 'P95 时长', render: (_, row) => row.duration.p95_ms == null ? '未记录' : `${row.duration.p95_ms} ms` }, { title: 'Token（总）', render: (_, row) => numberText(row.tokens.total) }, { title: '已知费用（按币种）', render: (_, row) => row.known_costs.length ? row.known_costs.map((cost) => cost.known_cost == null ? `${cost.currency} 费用未知` : `${cost.currency} ${cost.known_cost.toLocaleString('zh-CN', { maximumFractionDigits: 8 })}`).join(' · ') : '未记录' }]} /></div></div>;
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
      if (ancestors.has(item.id)) return <div key={`cycle-${item.id}`} className="obs-tree-row">循环引用：{item.id}</div>;
      const next = new Set(ancestors); next.add(item.id);
      return <div className="obs-tree-node" key={item.id}><button className="obs-tree-row" onClick={() => onSelect(item)}><span>{item.name}</span><Tag color={item.status === 'error' ? 'red' : item.status === 'success' ? 'green' : 'default'}>{statusLabel(item.status)}</Tag><Text type="secondary">{item.model ?? item.type} · {item.duration_ms == null ? '时长未知' : `${item.duration_ms} ms`}</Text></button>{(children.get(item.id) ?? []).map((child) => walk(child, next))}</div>;
    };
    return { roots: rootItems.map((item) => walk(item, new Set())), orphaned: orphanItems.map((item) => walk(item, new Set())) };
  }, [observations, onSelect]);
  return <div className="obs-tree">{roots}{orphaned.length > 0 && <><Text strong>孤立观察项（父节点不在结果中）</Text>{orphaned}</>}{observations.length > 0 && roots.length === 0 && orphaned.length === 0 && <Text type="secondary">未能构建安全树形结构（可能存在循环引用）。</Text>}</div>;
}
function TraceDetails({ envelope, timezone, onSelect, selectedObservation }: { envelope: ObservabilityDetail; timezone: string; onSelect: (item: ObservabilityObservation) => void; selectedObservation?: ObservabilityObservation }) {
  const result = envelope.data;
  return <Space direction="vertical" style={{ width: '100%' }} size="middle">
    <Alert type={envelope.state === 'ready' ? 'success' : envelope.state === 'empty' ? 'info' : 'warning'} message={`详情状态：${envelope.state}`} description={`${envelope.message} · 查询 ${dateText(envelope.queried_at, timezone)}`} />
    {envelope.coverage.truncated && <Alert type="warning" message="详情 observation 已截断" description={`此树仅包含 ${envelope.coverage.observations_read}/${envelope.coverage.limit} 条记录。`} />}
    {result ? <>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="Trace">{result.trace.name} · {result.trace.id}</Descriptions.Item>
        <Descriptions.Item label="状态与时长">{statusLabel(result.trace.status)} · {result.trace.duration_ms == null ? '时长未知' : `${numberText(result.trace.duration_ms)} ms`}</Descriptions.Item>
        <Descriptions.Item label="开始时间">{dateText(result.trace.start_at, timezone)}</Descriptions.Item>
        <Descriptions.Item label="来源 / 意图">{result.trace.source}{result.trace.source_detail ? ` · ${result.trace.source_detail}` : ''} · {result.trace.intents_recorded === false ? '历史根意图未记录' : result.trace.intents.join(', ') || '意图未知'}</Descriptions.Item>
        <Descriptions.Item label="Token">{tokenText(result.trace.tokens)}</Descriptions.Item>
        <Descriptions.Item label="费用"><Costs costs={result.trace.costs} /></Descriptions.Item>
        <Descriptions.Item label="结构">观察项 {result.structure.observations} · generations {result.structure.generations} · roots {result.structure.roots} · orphaned {result.structure.orphaned} · missing parents {result.structure.missing_parents ?? 0} · 跨读取边界 {result.structure.cross_boundary == null ? '未记录' : result.structure.cross_boundary ? '可能存在' : '未发现'}</Descriptions.Item>
      </Descriptions>
      {result.notes.map((note, index) => <Alert key={`${index}-${note}`} type="info" message={note} />)}
      <ObservationTree observations={result.observations} onSelect={onSelect} />
      {selectedObservation && <Card size="small" title={`${selectedObservation.name} · ${selectedObservation.type}`}><Descriptions column={1} size="small"><Descriptions.Item label="ID / Parent">{selectedObservation.id} / {selectedObservation.parent_id ?? '无'}</Descriptions.Item><Descriptions.Item label="状态">{statusLabel(selectedObservation.status)}</Descriptions.Item><Descriptions.Item label="开始 / 结束">{dateText(selectedObservation.start_at, timezone)} / {dateText(selectedObservation.end_at, timezone)}</Descriptions.Item><Descriptions.Item label="模型 / 时长">{selectedObservation.model ?? '未记录'} / {selectedObservation.duration_ms == null ? '时长未知' : `${numberText(selectedObservation.duration_ms)} ms`}</Descriptions.Item><Descriptions.Item label="Usage 与费用"><Usage value={selectedObservation} /></Descriptions.Item><Descriptions.Item label="错误摘要">{selectedObservation.error_summary ?? '无已记录错误'}</Descriptions.Item></Descriptions></Card>}
    </> : <Empty description={`${envelope.state}：${envelope.message}`} />}
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
  const [page, setPage] = useState(1); const [selected, setSelected] = useState<string>(); const [selectedObservation, setSelectedObservation] = useState<ObservabilityObservation>();
  const [costModel, setCostModel] = useState('all'); const [costBucket, setCostBucket] = useState('all');
  const [rangeError, setRangeError] = useState('');
  const filters: ObservabilityFilters = useMemo(() => ({ start_at: applied.start, end_at: applied.end, source: appliedFilters.source, max_observations: 5000, model: appliedFilters.model || undefined, intent: appliedFilters.intent || undefined, status: appliedFilters.status as ObservabilityFilters['status'], conversation_id: appliedFilters.conversation_id || undefined, message_id: appliedFilters.message_id || undefined }), [applied, appliedFilters]);
  const remote = apiMode === 'remote';
  const availability = useQuery({ queryKey: ['observability', 'status'], queryFn: observabilityApi.status, enabled: remote, retry: 0 });
  const overview = useQuery({ queryKey: ['observability', 'overview', filters], queryFn: () => observabilityApi.overview(filters), enabled: remote, retry: 0 });
  const traces = useQuery({ queryKey: ['observability', 'traces', filters, page], queryFn: () => observabilityApi.traces(filters, page), enabled: remote, retry: 0 });
  const detail = useQuery({ queryKey: ['observability', 'detail', selected, applied.start, applied.end], queryFn: () => observabilityApi.detail(selected!, filters), enabled: remote && !!selected, retry: 0 });
  const overviewData = overview.data?.data;
  const costTrendRows = useMemo(() => (overviewData?.cost_trend ?? []).filter((row) => (costModel === 'all' || row.model === costModel) && (costBucket === 'all' || row.bucket === costBucket)), [overviewData?.cost_trend, costModel, costBucket]);
  const applyPreset = (value: WindowPreset) => { setPreset(value); setRangeError(''); if (value !== 'custom') { const end = dayjs(); const start = value === '31d' ? end.subtract(31, 'day') : end.subtract(value === '1h' ? 1 : value === '24h' ? 24 : 7, value === '7d' ? 'day' : 'hour'); setApplied({ start: start.toISOString(), end: end.toISOString() }); setPage(1); } };
  const applyCustom = () => { if (!customRange?.[0] || !customRange[1]) { setRangeError('请选择完整的起止时间。'); return; } const start = customRange[0]; const end = customRange[1]; if (!start.isBefore(end) || end.diff(start, 'millisecond') > 31 * 24 * 60 * 60 * 1000) { setRangeError('起止时间必须递增，且范围不超过 31 天。'); return; } setRangeError(''); setApplied({ start: start.toISOString(), end: end.toISOString() }); setPage(1); };
  if (actor.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;
  const envelope = overview.data; const data = overviewData; const list = traces.data?.data;
  const columns: ColumnsType<ObservabilityTrace> = [{ title: 'Trace ID / 名称', render: (_, item) => <Space direction="vertical" size={0}><Text code>{item.id}</Text><Button type="link" onClick={() => setSelected(item.id)}>{item.name || '未命名链路'}</Button></Space> }, { title: '来源', render: (_, item) => <Space direction="vertical" size={0}>{item.source}<Text type="secondary">{item.source_detail ?? ''}</Text></Space> }, { title: '开始时间', dataIndex: 'start_at', render: (v) => dateText(v, timezone) }, { title: '意图 / 模型', render: (_, item) => <Space direction="vertical" size={2}>{item.intents_recorded === false ? '未采集' : item.intents.join(', ') || '未记录'}<Text type="secondary">{item.models.join(', ') || '未记录'}</Text></Space> }, { title: '状态', dataIndex: 'status', render: (v) => <Tag color={v === 'error' ? 'red' : v === 'success' ? 'green' : 'default'}>{statusLabel(v)}</Tag> }, { title: '时长', dataIndex: 'duration_ms', render: (v) => v == null ? '未知' : `${numberText(v)} ms` }, { title: 'Token', render: (_, item) => tokenText(item.tokens) }, { title: '费用', render: (_, item) => <Costs costs={item.costs} /> }, { title: '关联', render: (_, item) => <Space direction="vertical" size={0}>{item.conversation_id && <Text>会话 {item.conversation_id}</Text>}{item.message_id && <Text>消息 {item.message_id}</Text>}</Space> }];
  return <div className="workspace-shell rag-shell observability-shell"><PageHeader actor={actor.data} /><main className="rag-main observability-main"><section className="rag-heading"><div><Text type="secondary">员工工具 · 只读观测</Text><Title level={2}>观测与成本</Title><Paragraph type="secondary">真实请求链路与模型用量；不展示原始输入、输出或任意 metadata。</Paragraph></div><Text type="secondary">显示时区：{timezone}</Text></section>
    {!remote && <Alert type="warning" showIcon message="模拟模式不提供真实观测数据" description="此页不会填入模拟观测；切换到 remote 并使用已授权的员工账户。" />}
    {availability.isError && <Alert type="error" showIcon message="观测状态读取失败" description={availability.error instanceof Error ? availability.error.message : '请求失败'} />}
    {availability.data && <Alert type={availability.data.state === 'configured_unverified' ? 'info' : 'warning'} showIcon message={`配置状态（不探测连接）：${availability.data.state}`} description={<>{availability.data.message} · 范围上限 {availability.data.data?.limits.max_days ?? 31} 天；状态接口不主动读取上游。</>} />}
    <Card className="rag-card" title="查询范围"><Space wrap><Select aria-label="时间范围" value={preset} onChange={applyPreset} options={[{ value: '1h', label: '最近 1 小时' }, { value: '24h', label: '最近 24 小时' }, { value: '7d', label: '最近 7 天' }, { value: '31d', label: '最近 31 天（最大范围）' }, { value: 'custom', label: '自定义' }]} />{preset === 'custom' && <><DatePicker.RangePicker showTime value={customRange} onChange={(v) => setCustomRange(v)} /><Button onClick={applyCustom}>应用时间</Button></>}<Select aria-label="数据源" value={source} onChange={setSource} options={[{ value: 'aiden_support', label: '仅在线客服（默认）' }, { value: 'other', label: '其他来源（与在线客服分开查询）' }]} /><Input aria-label="模型筛选" placeholder="模型" value={model} onChange={(e) => setModel(e.target.value)} /><Input aria-label="意图或归因桶筛选" placeholder="意图 / 归因桶" value={intent} onChange={(e) => setIntent(e.target.value)} /><Select aria-label="状态筛选" allowClear placeholder="全部状态" value={status} onChange={setStatus} options={[{ value: 'success', label: '成功' }, { value: 'error', label: '错误' }, { value: 'unknown', label: '未知' }]} /><Input aria-label="会话 ID" placeholder="会话 ID" value={conversationId} onChange={(e) => setConversationId(e.target.value)} /><Input aria-label="消息 ID" placeholder="消息 ID" value={messageId} onChange={(e) => setMessageId(e.target.value)} /><Button type="primary" onClick={() => { setAppliedFilters({ model, intent, status, conversation_id: conversationId, message_id: messageId, source }); setPage(1); }}>应用筛选</Button></Space><Paragraph type="secondary" className="rag-hint">固定提交 {dateText(applied.start, timezone)} — {dateText(applied.end, timezone)}（{timezone}）；UTC 时间边界。评测与旁路主题任务不属于默认在线客服范围，不会与在线客服费用混算。历史 intent 未记录时，服务端也可能仅能匹配 generation 归因桶；不能将其解释为根意图。{rangeError && <Text type="danger"> {rangeError}</Text>}</Paragraph></Card>
    {overview.isLoading && <Spin tip="正在读取真实观测概览…" />}
    {overview.isError && <Alert type="error" showIcon message="概览查询失败" description={overview.error instanceof Error ? overview.error.message : '请求失败'} />}
    {overview.data?.state && !['ready', 'empty'].includes(overview.data.state) && <Alert type="warning" showIcon message={`概览查询状态：${overview.data.state}`} description={overview.data.message} />}
    {overview.data?.coverage.truncated && <Alert type="warning" showIcon message="概览读取结果已截断" description={`上游读取 ${overview.data.coverage.observations_read}/${overview.data.coverage.limit} 条 observation；统计仅覆盖当前批次。`} />}
    {traces.isError && <Alert type="error" showIcon message="链路查询失败" description={traces.error instanceof Error ? traces.error.message : '请求失败'} />}
    {traces.data?.state && !['ready', 'empty'].includes(traces.data.state) && <Alert type="warning" showIcon message={`链路查询状态：${traces.data.state}`} description={traces.data.message} />}
    {traces.data?.coverage.truncated && <Alert type="warning" showIcon message="链路列表结果已截断" description={`当前读取批次 ${traces.data.coverage.observations_read}/${traces.data.coverage.limit} 条 observation。`} />}
    {data && <><section className="obs-counts"><Card className="rag-stat"><Statistic title="根请求（不是会话）" value={data.counts.roots} /></Card><Card className="rag-stat"><Statistic title="生成次数" value={data.counts.generations} /></Card><Card className="rag-stat"><Statistic title="成功 / 错误 / 未知" value={`${data.counts.success} / ${data.counts.error} / ${data.counts.unknown}`} /></Card><Card className="rag-stat"><Statistic title="时长样本 / 均值" value={`${data.duration.samples} / ${numberText(data.duration.mean_ms)} ms`} /><Text type="secondary">P50 {numberText(data.duration.p50_ms)} ms · P95 {numberText(data.duration.p95_ms)} ms</Text></Card></section>
    <section className="observability-sections"><Card className="rag-card" title="请求量趋势"><Trend points={data.trend} /></Card><Card className="rag-card" title="Token 与费用"><Descriptions column={1} size="small"><Descriptions.Item label="Token（null 按未知展示）">{tokenText(data.tokens)} · 已知完整输入/输出/合计 usage 的 generations {data.tokens.known_generations}/{data.tokens.total_generations}{data.tokens.field_coverage && <div>{Object.entries(data.tokens.field_coverage).map(([field, coverage]) => `${field} ${coverage.known} 已知/${coverage.missing} 缺失`).join(' · ')}</div>}</Descriptions.Item><Descriptions.Item label="费用（币种分别展示）"><Costs costs={data.costs} /></Descriptions.Item></Descriptions><Alert type="info" showIcon message="费用范围与限制" description="金额来自 Langfuse costDetails.total 字段（USD）；字段来源由后端逐项标注，不等于结算账单。未知不等于零；不包含工具、数据库、GPU、embedding、Milvus/RRF 或 rerank 成本/耗时（未测量不代表免费）。共享 intent_classification 与 unattributed 保持独立，不按根请求意图分摊；其他来源与在线客服分开展示。" /></Card><Card className="rag-card" title="Generation 归因桶"><Table size="small" rowKey="bucket" pagination={false} dataSource={data.attribution} columns={[{ title: '归因桶（并非历史根请求意图）', dataIndex: 'bucket' }, { title: 'Generations', dataIndex: 'generations' }, { title: 'Token', render: (_, row) => tokenText(row.tokens) }, { title: '费用', render: (_, row) => <Costs costs={row.costs} /> }]} /></Card><Card className="rag-card" title="模型汇总"><Table size="small" rowKey="model" pagination={false} dataSource={data.models} columns={[{ title: '模型', dataIndex: 'model' }, { title: 'Generations', dataIndex: 'generations' }, { title: 'Token', render: (_, row) => tokenText(row.tokens) }, { title: '费用', render: (_, row) => <Costs costs={row.costs} /> }]} /></Card><Card className="rag-card" title="分析"><ul>{data.analysis.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></Card></section></>}
    {data && <Card className="rag-card obs-cost-trend" title="按生成模型与业务桶的每日费用趋势">
      <Paragraph type="secondary">按唯一 generation 的 UTC 日期、模型与业务桶分组；共享分类与未归因各自保留，不按根请求意图复制或分摊。模型/业务桶筛选仅作用于本表；费用按币种分开，未知费用不等于 0。</Paragraph>
      <Space wrap className="obs-cost-filters">
        <Select aria-label="费用趋势模型" value={costModel} onChange={setCostModel} options={[{ value: 'all', label: '全部模型' }, ...Array.from(new Set((data.cost_trend ?? []).map((row) => row.model))).map((model) => ({ value: model, label: model }))]} />
        <Select aria-label="费用趋势业务桶" value={costBucket} onChange={setCostBucket} options={[{ value: 'all', label: '全部业务桶' }, ...Array.from(new Set((data.cost_trend ?? []).map((row) => row.bucket))).map((bucket) => ({ value: bucket, label: bucket }))]} />
      </Space>
      {!Array.isArray(data.cost_trend) && <Alert type="warning" showIcon message="接口未提供模型与业务桶分组趋势" description="此响应没有 cost_trend 字段；页面不会从请求总额推算、复制或伪造分组费用。" />}
      {Array.isArray(data.cost_trend) && data.cost_trend.length === 0 && <Text type="secondary">此筛选范围没有 generation 成本趋势记录。</Text>}
      {costTrendRows.length > 0 && <Table<ObservabilityCostTrendPoint> rowKey={(row) => `${row.date}:${row.model}:${row.bucket}`} dataSource={costTrendRows} pagination={false} scroll={{ x: 900 }} columns={[
        { title: 'UTC 日期', dataIndex: 'date', render: (value: string) => value === 'unknown' ? '日期未知' : value },
        { title: '模型', dataIndex: 'model' },
        { title: '业务桶', dataIndex: 'bucket' },
        { title: 'Generations', dataIndex: 'generations' },
        { title: 'Token', render: (_, row) => tokenText(row.tokens) },
        { title: '已知费用与覆盖', render: (_, row) => <Costs costs={row.costs} /> },
      ]} />}
    </Card>}
    <Card className="rag-card" title="请求链路"><Paragraph type="secondary">Trace ID、名称、状态、时长与 usage 仅按后端实际记录展示。search_faq 内部 embedding、Milvus、RRF 与 rerank 阶段耗时未采集。</Paragraph><Table rowKey="id" columns={columns} dataSource={list?.items ?? []} loading={traces.isLoading} pagination={false} scroll={{ x: 1200 }} locale={{ emptyText: traces.isLoading ? '正在读取链路…' : traces.data?.state === 'empty' ? '该范围没有匹配 Trace' : traces.data && traces.data.state !== 'ready' ? `该查询状态没有链路列表（${traces.data.state}）` : '暂无链路项目' }} /><Pagination current={list?.page ?? page} pageSize={list?.page_size ?? 20} total={list?.total ?? 0} onChange={(next) => setPage(next)} showSizeChanger={false} /></Card>
    <Card className="rag-card" title="数据状态与覆盖"><Descriptions column={{ xs: 1, md: 2 }} size="small"><Descriptions.Item label="最近查询">{dateText(envelope?.queried_at, timezone)}</Descriptions.Item><Descriptions.Item label="最新记录">{dateText(envelope?.latest_record_at, timezone)}</Descriptions.Item><Descriptions.Item label="覆盖范围">{envelope?.coverage?.scope ?? '未记录'}</Descriptions.Item><Descriptions.Item label="读取 observation">{envelope?.coverage?.observations_read ?? '未记录'} / {envelope?.coverage?.limit ?? '—'}</Descriptions.Item><Descriptions.Item label="Generations（读取/匹配）">{envelope?.coverage ? `${envelope.coverage.generations_read} / ${envelope.coverage.matched_generations}` : '未记录'}</Descriptions.Item><Descriptions.Item label="匹配 Trace">{envelope?.coverage?.matched_traces ?? '未记录'}</Descriptions.Item><Descriptions.Item label="截断">{envelope?.coverage?.truncated == null ? '未记录' : envelope.coverage.truncated ? '是（仅当前批次）' : '否'}</Descriptions.Item><Descriptions.Item label="上游状态">{envelope?.state ?? '尚未读取'}</Descriptions.Item></Descriptions><Paragraph type="secondary">异步采集可能有延迟。configured_unverified 仅表示已配置，连接未主动验证。缺失 usage 与 pricing 会降低覆盖；工具、数据库、GPU、embedding 与 rerank 未测量，不代表免费。数据不包含原始输入、输出或任意 metadata。</Paragraph></Card>
    <Drawer title="请求链路详情" width={760} open={!!selected} onClose={() => { setSelected(undefined); setSelectedObservation(undefined); }}><Space direction="vertical" style={{ width: '100%' }} size="middle">{detail.isLoading && <Spin tip="读取链路详情…" />}{detail.isError && <Alert type="error" message="详情读取失败" description={detail.error instanceof Error ? detail.error.message : '请求失败'} />}{detail.data && <TraceDetails envelope={detail.data} timezone={timezone} onSelect={setSelectedObservation} selectedObservation={selectedObservation} />}{selected && !remote && <Alert type="warning" message="模拟模式不提供详情数据" />}</Space></Drawer>
  </main></div>;
}
