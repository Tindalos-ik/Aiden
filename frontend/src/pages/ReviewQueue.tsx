import { useEffect, useState } from 'react';
import { Alert, Button, Card, DatePicker, Drawer, Empty, Form, Input, Pagination, Select, Space, Spin, Statistic, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, apiMode } from '../api';
import { ragApi } from '../api/rag';
import { PageHeader } from '../components/Common';
import type { Dayjs } from 'dayjs';
import type { RejectionReason, ReviewDetail, ReviewIssueType, ReviewOriginal, ReviewQueueItem, ReviewSort, ReviewStatus } from '../types';

const { Paragraph, Text, Title } = Typography;
const { RangePicker } = DatePicker;
const pageSize = 20;
const statusLabels: Record<ReviewStatus, string> = { pending: '待审核', approved: '已通过', rejected: '已驳回', all: '全部状态' };
const issueLabels: Record<ReviewIssueType, string> = {
  knowledge_gap: '疑似知识缺口（待核实）',
  retrieval_miss: '检索漏召回（人工确认）',
  policy_gap: '政策缺口',
  service_failure: '服务故障',
  unclassified: '未判定',
};
const issueTypes = Object.keys(issueLabels) as ReviewIssueType[];
const reasons: Array<{ value: RejectionReason; label: string }> = [
  { value: 'existing_knowledge_not_retrieved', label: '已有知识但当次未召回' },
  { value: 'not_reusable', label: '无意义或不可复用' },
  { value: 'outdated', label: '强时效或已过期' },
  { value: 'other', label: '其他' },
];
const reasonLabel = (reason: RejectionReason | null) => reasons.find((item) => item.value === reason)?.label ?? reason;
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请检查连接后重试';
const dateText = (date: string) => {
  const utcDate = /(?:Z|[+-]\d{2}:\d{2})$/i.test(date) ? date : `${date}Z`;
  return `${new Date(utcDate).toLocaleString('zh-CN', { timeZone: 'UTC' })} UTC`;
};
const localDayStartUtc = (date: Dayjs) => date.startOf('day').toDate().toISOString();
const issueCaveats: Partial<Record<ReviewIssueType, string>> = {
  knowledge_gap: '疑似知识缺口仍需人工核实修复对象。',
  retrieval_miss: '检索漏召回需检查检索链路；FAQ 入库就绪不代表召回问题已修复。',
  policy_gap: '政策缺口需修复商家政策；FAQ 入库就绪不代表政策已修复。',
  service_failure: '服务故障需修复对应服务；FAQ 入库就绪不代表故障已修复。',
};
const ingestionLabels: Record<ReviewQueueItem['ingestion_status'], string> = {
  not_started: '未启动',
  pending: '同步中',
  ready: 'FAQ 已就绪（可检索）',
  manual_review: '需人工复核',
  failed: '同步失败',
};
function IngestionStatus({ item }: { item: ReviewQueueItem }) {
  if (item.review_status !== 'approved') return null;
  const status = item.ingestion_status;
  return <Space direction="vertical" size={4}>
    <Tag color={status === 'ready' ? 'green' : status === 'failed' ? 'red' : 'gold'}>{ingestionLabels[status]}</Tag>
    {status === 'ready' && <Text type="secondary">FAQ 可检索，不代表复问已命中。</Text>}
    {item.ingestion_error && <Text type="danger">{item.ingestion_error}</Text>}
  </Space>;
}

function OriginalEvidence({ original }: { original: ReviewOriginal }) {
  const snapshot = original.retrieval_snapshot;
  return <Card size="small" title={<Space wrap><Text>原话 #{original.id}</Text><Text type="secondary">{dateText(original.created_at)}</Text></Space>}>
    <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{original.raw_question}</Paragraph>
    <Space wrap><Tag>入口：{original.source}</Tag><Tag>原因：{original.reason}</Tag></Space>
    {original.retrieval_query && <Paragraph style={{ marginTop: 12 }}>当次检索问题：{original.retrieval_query}</Paragraph>}
    {snapshot === null ? <Alert style={{ marginTop: 12 }} type="info" message="历史记录未保存召回快照" />
      : original.retrieval_status === 'not_searched' ? <Alert style={{ marginTop: 12 }} type="info" message="当次未执行检索" />
      : original.retrieval_status === 'error' ? <Alert style={{ marginTop: 12 }} type="error" message="当次检索失败，不能判断是否有可召回知识" />
      : snapshot.length === 0 ? <Alert style={{ marginTop: 12 }} type="warning" message="当次检索无召回片段" />
      : <div className="rag-stack" style={{ marginTop: 12 }}>{snapshot.map((chunk, index) => <Card size="small" key={`${chunk.chunk_id}-${index}`} title={<Space wrap><Tag>第 {chunk.rank} 位</Tag><Text code>{chunk.chunk_id}</Text><Text>可比较分数：{chunk.score ?? '未记录'}</Text></Space>}>
        <Text type="secondary">来源：{chunk.source || '未记录'} · 章节：{chunk.section || '未记录'}</Text>
        <pre className="rag-chunk-body">{chunk.content}</pre>
      </Card>)}</div>}
  </Card>;
}

function ReviewDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const client = useQueryClient();
  const [answer, setAnswer] = useState('');
  const [category, setCategory] = useState('');
  const [note, setNote] = useState('');
  const [reason, setReason] = useState<RejectionReason>();
  const [actionError, setActionError] = useState('');
  const detail = useQuery({ queryKey: ['rag-review-detail', id], queryFn: () => ragApi.reviewDetail(id), retry: 0 });
  useEffect(() => {
    if (!detail.data) return;
    setAnswer(detail.data.approved_answer ?? '');
    setCategory(detail.data.category ?? '');
    setNote(detail.data.review_note ?? '');
    setReason(detail.data.rejection_reason ?? undefined);
  }, [detail.data]);
  const mutation = useMutation({
    mutationFn: (operation: () => Promise<ReviewDetail>) => operation(),
    onSuccess: (updated) => {
      setActionError('');
      client.setQueryData(['rag-review-detail', id], updated);
      void client.invalidateQueries({ queryKey: ['rag-review-queue'] });
    },
    onError: (error) => setActionError(errorText(error)),
  });
  const item = detail.data;
  return <Drawer open onClose={onClose} width={760} title={item?.normalized_question ?? '审核详情'} destroyOnHidden>
    {detail.isPending ? <Spin /> : detail.isError ? <Alert type="error" showIcon message="详情加载失败" description={errorText(detail.error)} action={<Button onClick={() => void detail.refetch()}>重试</Button>} /> : item && <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap><Text>详情中的全部关联原话 {item.scoped_occurrence_count} 条</Text><Text>全部关联反馈 {item.scoped_feedback_count} 次</Text><Text>全生命周期出现 {item.occurrence_count} 次</Text></Space>
      <Space wrap>{item.issue_types.map((type) => <Tag key={type}>{issueLabels[type] ?? type}</Tag>)}</Space>
      {item.issue_types.map((type) => issueCaveats[type] && <Alert key={type} type={type === 'knowledge_gap' ? 'info' : 'warning'} showIcon message={issueCaveats[type]} />)}
      {item.review_status === 'approved' && <Alert type="info" showIcon message="补库效果复问核对" description={`以原话再次提问，检查回答中的实际引用内容是否包含 FAQ（ID ${item.faq_id ?? '未记录'}）对应知识。FAQ“可检索”仅代表同步就绪，不证明复问已命中；此页面不记录或伪造复问成功。`} />}
      <Space wrap><Tag color={item.review_status === 'pending' ? 'blue' : item.review_status === 'approved' ? 'green' : 'default'}>{statusLabels[item.review_status]}</Tag><Text>归并原话 {item.occurrence_count} 条</Text><Text type="secondary">创建于 {dateText(item.created_at)}</Text></Space>
      <Card size="small" title="模型示例答案（未核实，不会自动入库）"><Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.example_answer || '暂无示例答案'}</Paragraph></Card>
      <Title level={5}>逐条原话与当次召回证据</Title>
      {item.originals.length ? item.originals.map((original) => <OriginalEvidence key={original.id} original={original} />) : <Empty description="暂无归并原话" />}
      {item.review_status === 'pending' ? <Card size="small" title="人工审核（请确认真实业务依据）">
        <Form layout="vertical">
          <Form.Item label="人工核准答案" required><Input.TextArea value={answer} onChange={(event) => setAnswer(event.target.value)} rows={5} placeholder="请核实后填写答案；模型示例不能直接当成已核实事实" /></Form.Item>
          <Form.Item label="分类" required><Input value={category} onChange={(event) => setCategory(event.target.value)} placeholder="填写 FAQ 分类" /></Form.Item>
          <Form.Item label="审核备注"><Input.TextArea value={note} onChange={(event) => setNote(event.target.value)} rows={2} /></Form.Item>
          <Space wrap><Button type="primary" disabled={!answer.trim() || !category.trim() || mutation.isPending} loading={mutation.isPending} onClick={() => mutation.mutate(() => ragApi.approveReview(id, { approved_answer: answer.trim(), category: category.trim(), review_note: note.trim() }))}>通过并补库</Button><Select style={{ width: 235 }} value={reason} onChange={setReason} placeholder="选择驳回原因" options={reasons} /><Button danger disabled={!reason || mutation.isPending} loading={mutation.isPending} onClick={() => mutation.mutate(() => ragApi.rejectReview(id, { rejection_reason: reason!, review_note: note.trim() }))}>驳回（不补库）</Button></Space>
        </Form>
      </Card> : <Card size="small" title="审核结果"><Paragraph>人工核准答案：{item.approved_answer || '—'}</Paragraph><Paragraph>分类：{item.category || '—'} · 驳回原因：{reasonLabel(item.rejection_reason) || '—'}</Paragraph><Paragraph>审核备注：{item.review_note || '—'}</Paragraph><IngestionStatus item={item} />{item.faq_id != null && <Paragraph>FAQ ID：{item.faq_id}</Paragraph>}{item.review_status === 'approved' && (item.ingestion_status === 'failed' || item.ingestion_status === 'pending' || item.ingestion_status === 'not_started') && <Button loading={mutation.isPending} onClick={() => mutation.mutate(() => ragApi.retryReviewIngestion(id))}>重试同步</Button>}</Card>}
      {actionError && <Alert type="error" showIcon message="操作失败，可检查后重试" description={actionError} closable onClose={() => setActionError('')} />}
    </Space>}
  </Drawer>;
}

export function ReviewQueue() {
  const client = useQueryClient();
  const [status, setStatus] = useState<ReviewStatus>('pending');
  const [sort, setSort] = useState<ReviewSort>('recent');
  const [category, setCategory] = useState('all');
  const [issueType, setIssueType] = useState<ReviewIssueType>();
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [page, setPage] = useState(1);
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedId = searchParams.get('review_id') || undefined;
  const setSelectedId = (id: string | undefined) => {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      if (id) next.set('review_id', id);
      else next.delete('review_id');
      return next;
    });
  };
  const [jobError, setJobError] = useState('');
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const remote = apiMode === 'remote';
  const startAt = dateRange ? localDayStartUtc(dateRange[0]) : undefined;
  const endAt = dateRange ? localDayStartUtc(dateRange[1].add(1, 'day')) : undefined;
  const filters = {
    status, sort, offset: (page - 1) * pageSize,
    ...(category === 'uncategorized' ? { uncategorized: true } : category.startsWith('category:') ? { category: category.slice('category:'.length) } : {}),
    ...(startAt ? { start_at: startAt, end_at: endAt } : {}),
    ...(issueType ? { issue_type: issueType } : {}),
  };
  const queue = useQuery({
    queryKey: ['rag-review-queue', filters],
    queryFn: () => ragApi.reviewQueue(filters),
    enabled: remote,
    retry: 0,
  });
  const jobs = useQuery({ queryKey: ['rag-jobs'], queryFn: ragApi.jobs, enabled: remote, refetchInterval: 2000 });
  const organize = useMutation({
    mutationFn: () => ragApi.startJob('low-confidence-review'),
    onSuccess: () => { setJobError(''); void client.invalidateQueries({ queryKey: ['rag-jobs'] }); void client.invalidateQueries({ queryKey: ['rag-review-queue'] }); },
    onError: (error) => setJobError(errorText(error)),
  });
  const job = jobs.data?.items.find((item) => item.kind === 'low-confidence-review');
  useEffect(() => {
    if (job?.status === 'completed') void client.invalidateQueries({ queryKey: ['rag-review-queue'] });
  }, [job?.id, job?.status, client]);
  if (actor.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;

  const changeFilter = (change: () => void) => {
    change();
    setPage(1);
    setSelectedId(undefined);
  };
  const stats = queue.data?.statistics;
  return <div className="workspace-shell rag-shell"><PageHeader actor={actor.data} /><main className="rag-main">
    <div className="rag-heading"><div><Text className="section-kicker">KNOWLEDGE REVIEW</Text><Title level={2}>低置信度问题待审队列</Title><Paragraph type="secondary">按业务问题归并原话，核对当次召回证据后由员工决定是否补入 FAQ。类别按范围内原始问题证据判定。</Paragraph></div><Space wrap><Button icon={<ReloadOutlined />} disabled={!remote} loading={queue.isFetching} onClick={() => void Promise.all([queue.refetch(), jobs.refetch()])}>刷新</Button><Button type="primary" disabled={!remote || organize.isPending || job?.status === 'queued' || job?.status === 'running'} loading={organize.isPending} onClick={() => organize.mutate()}>整理低置信度问题</Button></Space></div>
    {!remote ? <Alert showIcon type="warning" message="演示模式不可执行真实归并或审核" description="请切换 VITE_API_MODE=remote 并以员工账号登录。此页面不会伪造队列或入库结果。" /> : <>
      {jobError && <Alert type="error" showIcon message="启动整理失败" description={jobError} action={<Button size="small" onClick={() => organize.mutate()}>重试</Button>} />}
      {jobs.isError && <Alert type="error" showIcon message="任务状态加载失败" description={errorText(jobs.error)} action={<Button size="small" onClick={() => void jobs.refetch()}>重试</Button>} />}
      {job && <Alert type={job.status === 'failed' ? 'error' : job.status === 'completed' ? 'success' : 'info'} showIcon message={`整理任务：${job.status}`} description={job.error || job.progress || undefined} />}
      {queue.isError && <Alert type="error" showIcon message="队列加载失败" description={errorText(queue.error)} action={<Button onClick={() => void queue.refetch()}>重试</Button>} />}
      {stats && !queue.isError && <Card className="rag-card" title="当前筛选统计">
        <Space wrap size="large">
          <Statistic title="原始问题" value={stats.original_question_count} />
          <Statistic title="归并问题" value={stats.merged_question_count} />
          <Statistic title="负反馈次数" value={stats.feedback_count} />
          {Object.entries(stats.review_status_counts).map(([key, value]) => <Statistic key={key} title={statusLabels[key as Exclude<ReviewStatus, 'all'>]} value={value} />)}
          {Object.entries(stats.issue_type_counts).map(([key, value]) => <Statistic key={key} title={issueLabels[key as ReviewIssueType]} value={value} />)}
        </Space>
        <Space wrap size="large" style={{ marginTop: 16 }}>
          {Object.entries(stats.ingestion_status_counts).map(([key, value]) => <Statistic key={key} title={`入库：${ingestionLabels[key as ReviewQueueItem['ingestion_status']]}`} value={value} />)}
        </Space>
        <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>统计覆盖全部匹配记录（不受分页影响）。原始问题数仅统计筛选后归并问题关联的原话，不含尚未归并的问题池；归并问题数按队列项。负反馈数仅统计不满意反馈，按 source_assistant_message_id 跨归并行去重（旧记录无 ID 按记录计数），因此行内负反馈数相加可能大于此总数。类别可多标签，类型统计可能重叠。</Paragraph>
      </Card>}
      <Card className="rag-card" title="审核列表">
        <Space wrap style={{ marginBottom: 16 }}>
          <Select aria-label="审核状态" value={status} style={{ width: 145 }} onChange={(next: ReviewStatus) => changeFilter(() => setStatus(next))} options={Object.entries(statusLabels).map(([value, label]) => ({ value, label }))} />
          <Select aria-label="排序方式" value={sort} style={{ width: 170 }} onChange={(next: ReviewSort) => changeFilter(() => setSort(next))} options={[{ value: 'recent', label: '最近创建' }, { value: 'heat', label: '热度（全生命周期）' }]} />
          <Select aria-label="分类" value={category} style={{ width: 180 }} onChange={(value: string) => changeFilter(() => setCategory(value))} options={[{ value: 'all', label: '全部分类' }, { value: 'uncategorized', label: '未分类' }, ...(queue.data?.categories ?? []).filter((value): value is string => value !== null && value !== '').map((value) => ({ value: `category:${value}`, label: value }))]} />
          <Select aria-label="问题类型" allowClear placeholder="全部问题类型" style={{ width: 210 }} value={issueType} onChange={(value: ReviewIssueType | undefined) => changeFilter(() => setIssueType(value))} options={issueTypes.map((value) => ({ value, label: issueLabels[value] }))} />
          <RangePicker aria-label="原始问题日期范围" value={dateRange} onChange={(values) => changeFilter(() => setDateRange(values?.[0] && values[1] ? [values[0], values[1]] : null))} />
        </Space>
        <Paragraph type="secondary">日期按原始问题创建时间筛选（浏览器本地日期，包含首尾日期）；发送为 UTC 半开区间。服务器时间按 UTC 标注。热度使用全生命周期出现次数，不受日期范围影响；卡片同时显示范围内问题与反馈计数。</Paragraph>
        {queue.isPending ? <Spin /> : queue.isError ? null : !queue.data?.items.length ? <Empty description="当前筛选下暂无问题" /> : <div className="rag-stack">{queue.data.items.map((item) => <Card key={item.id} size="small" title={<Space wrap><Text strong>{item.normalized_question}</Text><Tag>{statusLabels[item.review_status]}</Tag>{item.issue_types.map((type) => <Tag key={type}>{issueLabels[type]}</Tag>)}</Space>} extra={<Button onClick={() => setSelectedId(item.id)}>查看原话与审核</Button>}>
          <Paragraph>当前筛选原始问题 {item.scoped_occurrence_count} 条 · 当前筛选不满意反馈 {item.scoped_feedback_count} 次 · 全生命周期出现 {item.occurrence_count} 次</Paragraph>
          {item.issue_types.map((type) => issueCaveats[type] && <Alert key={type} type={type === 'knowledge_gap' ? 'info' : 'warning'} showIcon message={issueCaveats[type]} />)}
          <Paragraph>分类：{item.category || '未分类'} · 示例答案（仅供备查）：{item.example_answer || '暂无'}</Paragraph><IngestionStatus item={item} />
        </Card>)}</div>}
        <Pagination style={{ marginTop: 20 }} current={page} pageSize={pageSize} total={queue.data?.total ?? 0} showSizeChanger={false} onChange={setPage} />
      </Card>
    </>}
    {selectedId !== undefined && <ReviewDrawer key={selectedId} id={selectedId} onClose={() => setSelectedId(undefined)} />}
  </main></div>;
}
