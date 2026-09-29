import { useEffect, useState } from 'react';
import { Alert, Button, Card, Drawer, Empty, Form, Input, Pagination, Select, Space, Spin, Tag, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, apiMode } from '../api';
import { ragApi } from '../api/rag';
import { PageHeader } from '../components/Common';
import type { RejectionReason, ReviewDetail, ReviewOriginal, ReviewQueueItem, ReviewStatus } from '../types';

const { Paragraph, Text, Title } = Typography;
const pageSize = 20;
const statusLabels: Record<ReviewStatus, string> = { pending: '待审核', approved: '已通过', rejected: '已驳回' };
const reasons: Array<{ value: RejectionReason; label: string }> = [
  { value: 'existing_knowledge_not_retrieved', label: '已有知识但当次未召回' },
  { value: 'not_reusable', label: '无意义或不可复用' },
  { value: 'outdated', label: '强时效或已过期' },
  { value: 'other', label: '其他' },
];
const reasonLabel = (reason: RejectionReason | null) => reasons.find((item) => item.value === reason)?.label ?? reason;
const errorText = (error: unknown) => error instanceof Error ? error.message : '请求失败，请检查连接后重试';
const dateText = (date: string) => new Date(date).toLocaleString('zh-CN');

function IngestionStatus({ item }: { item: ReviewQueueItem }) {
  if (item.review_status !== 'approved') return null;
  const status = item.ingestion_status;
  const label = status === 'ready' ? '知识已可检索'
    : status === 'failed' ? '同步失败'
    : status === 'manual_review' ? '知识块超长，需人工复核后重新同步'
    : status === 'not_started' ? '尚未启动同步（不可检索）'
    : '同步中 / 待向量化（尚不可检索）';
  return <Space direction="vertical" size={4}>
    <Tag color={status === 'ready' ? 'green' : status === 'failed' ? 'red' : 'gold'}>{label}</Tag>
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
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string>();
  const [jobError, setJobError] = useState('');
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const remote = apiMode === 'remote';
  const queue = useQuery({ queryKey: ['rag-review-queue', status, page], queryFn: () => ragApi.reviewQueue(status, (page - 1) * pageSize), enabled: remote, retry: 0 });
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
  return <div className="workspace-shell rag-shell"><PageHeader actor={actor.data} /><main className="rag-main">
    <div className="rag-heading"><div><Text className="section-kicker">KNOWLEDGE REVIEW</Text><Title level={2}>低置信度问题待审队列</Title><Paragraph type="secondary">按业务问题归并原话，核对当次召回证据后由员工决定是否补入 FAQ。</Paragraph></div><Space wrap><Button icon={<ReloadOutlined />} disabled={!remote} loading={queue.isFetching} onClick={() => void Promise.all([queue.refetch(), jobs.refetch()])}>刷新</Button><Button type="primary" disabled={!remote || organize.isPending || job?.status === 'queued' || job?.status === 'running'} loading={organize.isPending} onClick={() => organize.mutate()}>整理低置信度问题</Button></Space></div>
    {!remote ? <Alert showIcon type="warning" message="演示模式不可执行真实归并或审核" description="请切换 VITE_API_MODE=remote 并以员工账号登录。此页面不会伪造队列或入库结果。" /> : <>
      {jobError && <Alert type="error" showIcon message="启动整理失败" description={jobError} action={<Button size="small" onClick={() => organize.mutate()}>重试</Button>} />}
      {jobs.isError && <Alert type="error" showIcon message="任务状态加载失败" description={errorText(jobs.error)} action={<Button size="small" onClick={() => void jobs.refetch()}>重试</Button>} />}
      {job && <Alert type={job.status === 'failed' ? 'error' : job.status === 'completed' ? 'success' : 'info'} showIcon message={`整理任务：${job.status}`} description={job.error || job.progress || undefined} />}
      <Card className="rag-card" title="审核列表" extra={<Select value={status} style={{ width: 145 }} onChange={(next: ReviewStatus) => { setStatus(next); setPage(1); setSelectedId(undefined); }} options={Object.entries(statusLabels).map(([value, label]) => ({ value, label }))} />}>
        {queue.isPending ? <Spin /> : queue.isError ? <Alert type="error" showIcon message="队列加载失败" description={errorText(queue.error)} action={<Button onClick={() => void queue.refetch()}>重试</Button>} /> : !queue.data?.items.length ? <Empty description="当前状态下暂无待审问题" /> : <div className="rag-stack">{queue.data.items.map((item) => <Card key={item.id} size="small" title={<Space wrap><Text strong>{item.normalized_question}</Text><Tag>{statusLabels[item.review_status]}</Tag></Space>} extra={<Button onClick={() => setSelectedId(item.id)}>查看原话与审核</Button>}>
          <Paragraph>出现 {item.occurrence_count} 次 · 示例答案（仅供备查）：{item.example_answer || '暂无'}</Paragraph><IngestionStatus item={item} />
        </Card>)}</div>}
        <Pagination style={{ marginTop: 20 }} current={page} pageSize={pageSize} total={queue.data?.total ?? 0} showSizeChanger={false} onChange={setPage} />
      </Card>
    </>}
    {selectedId !== undefined && <ReviewDrawer key={selectedId} id={selectedId} onClose={() => setSelectedId(undefined)} />}
  </main></div>;
}
