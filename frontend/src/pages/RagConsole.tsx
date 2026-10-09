import { useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Select, Space, Spin, Statistic, Tabs, Tag, Typography } from 'antd';
import { DatabaseOutlined, ReloadOutlined, RocketOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, apiMode } from '../api';
import { ragApi } from '../api/rag';
import { PageHeader } from '../components/Common';
import type { RagChunk, RagJob } from '../types';

const { Text, Title, Paragraph } = Typography;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请检查后端服务';
}

function StatusCount({ title, count }: { title: string; count: number }) {
  return <Card size="small" className="rag-stat"><Statistic title={title} value={count} /></Card>;
}

function ChunkCard({ chunk }: { chunk: RagChunk }) {
  return <Card size="small" className="rag-chunk" title={<Space wrap><span>{chunk.sectionPath || '未命名章节'}</span><Tag>{chunk.category}</Tag></Space>}
    extra={<Space wrap><Tag color={chunk.isKeyClause ? 'gold' : 'default'}>{chunk.isKeyClause ? '关键条款' : '普通内容'}</Tag><Tag>{chunk.contentType}</Tag></Space>}>
    <div className="rag-chunk-meta">
      <span>问法：{chunk.questions.length ? chunk.questions.join(' / ') : '无'}</span>
      {chunk.sourceType && <span>来源：{chunk.sourceType}{chunk.sourcePath ? ` · ${chunk.sourcePath}` : ''}</span>}
      {chunk.vectorStatus && <span>向量状态：<Tag color={chunk.vectorStatus === 'vectorized' ? 'green' : chunk.vectorStatus === 'pending' ? 'blue' : 'orange'}>{chunk.vectorStatus}</Tag></span>}
      {chunk.needsManualReview && <Tag color="orange">需人工审核：{chunk.reviewReason || '请检查内容长度'}</Tag>}
    </div>
    <pre className="rag-chunk-body">{chunk.content}</pre>
  </Card>;
}

function JobPanel({ jobs }: { jobs: RagJob[] }) {
  if (!jobs.length) return <Empty description="尚无控制台任务" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  return <div className="rag-stack">{jobs.map((job) => <Card key={job.id} size="small" className="rag-job">
    <Space wrap><Text strong>{job.kind}</Text><Tag color={job.status === 'completed' ? 'green' : job.status === 'failed' ? 'red' : job.status === 'cancelled' ? 'orange' : job.status === 'stopping' ? 'gold' : 'blue'}>{job.status}</Tag><Text type="secondary">{new Date(job.createdAt).toLocaleString('zh-CN')}</Text></Space>
    {job.progress && <Paragraph className="rag-job-note">{job.progress}</Paragraph>}
    {job.error && <Alert type="error" showIcon message={job.error} />}
    {job.result != null && <pre className="rag-result">{JSON.stringify(job.result, null, 2)}</pre>}
  </Card>)}</div>;
}

export function RagConsole() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<string>();
  const [offset, setOffset] = useState(0);
  const [milvusOffset, setMilvusOffset] = useState(0);
  const [actionError, setActionError] = useState('');
  const actorQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const enabled = apiMode === 'remote';
  const overview = useQuery({ queryKey: ['rag-overview'], queryFn: ragApi.overview, enabled, refetchInterval: 5000 });
  const milvus = useQuery({ queryKey: ['rag-milvus', milvusOffset], queryFn: () => ragApi.milvus(milvusOffset), enabled });
  const preview = useQuery({ queryKey: ['rag-preview', file], queryFn: () => ragApi.preview(file!), enabled: enabled && !!file, retry: 0 });
  const chunks = useQuery({ queryKey: ['rag-chunks', offset], queryFn: () => ragApi.chunks(offset), enabled });
  const mining = useQuery({ queryKey: ['rag-mining'], queryFn: ragApi.mining, enabled, refetchInterval: 5000 });
  const jobs = useQuery({ queryKey: ['rag-jobs'], queryFn: ragApi.jobs, enabled, refetchInterval: 2000 });

  useEffect(() => {
    const documents = overview.data?.documents;
    if (documents?.length && (!file || !documents.includes(file))) setFile(documents[0]);
  }, [overview.data?.documents, file]);

  const refresh = async () => {
    await Promise.all(['rag-overview', 'rag-milvus', 'rag-preview', 'rag-chunks', 'rag-mining', 'rag-jobs'].map((key) => queryClient.invalidateQueries({ queryKey: [key] })));
  };
  const action = useMutation({
    mutationFn: (operation: () => Promise<unknown>) => operation(),
    onSuccess: () => { setActionError(''); void refresh(); },
    onError: (error) => setActionError(errorText(error)),
  });
  const run = (operation: () => Promise<unknown>) => action.mutate(operation);
  const working = action.isPending || (jobs.data?.items.some((item) => item.status === 'queued' || item.status === 'running' || item.status === 'stopping') ?? false);

  if (actorQuery.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actorQuery.data) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" /></div>;

  return <div className="workspace-shell rag-shell">
    <PageHeader actor={actorQuery.data} />
    <main className="rag-main">
      <div className="rag-heading"><div><Text className="section-kicker">KNOWLEDGE OPERATIONS</Text><Title level={2}>RAG 建库控制台</Title><Paragraph type="secondary">预览切块、导入来源、挖掘对话，再按 pending 状态补齐向量。</Paragraph></div><Space wrap><Link to="/staff/evals">查看 RAG 评估</Link>{apiMode === 'mock' && <Link to="/staff">返回员工工作台</Link>}<Button icon={<ReloadOutlined />} onClick={() => void refresh()} disabled={!enabled}>刷新</Button></Space></div>
      {!enabled ? <Alert showIcon type="warning" message="演示模式不可建库" description="此页面不会模拟启动向量服务、写入 MySQL 或 Milvus。请切换 VITE_API_MODE=remote 并使用员工账号登录。" /> : <>
        {actionError && <Alert type="error" showIcon message={actionError} closable onClose={() => setActionError('')} />}
        {overview.isError && <Alert type="error" showIcon message="建库总览加载失败" description={errorText(overview.error)} action={<Button size="small" onClick={() => void overview.refetch()}>重试</Button>} />}
        {overview.data?.databaseError && <Alert type="error" showIcon message="MySQL 状态读取失败" description={overview.data.databaseError} />}
        {overview.data?.documentsError && <Alert type="error" showIcon message="知识目录读取失败" description={overview.data.documentsError} />}
        <div className="rag-grid">
          <Card className="rag-card" title="本地 BGE 向量服务" extra={<Tag color={overview.data?.embedding.dimensionPending ? 'gold' : overview.data?.embedding.healthy ? 'green' : 'red'}>{overview.data?.embedding.dimensionPending ? '维度待验证' : overview.data?.embedding.healthy ? '健康' : '未就绪'}</Tag>}>
            <Paragraph>进程：{overview.data?.embedding.managed ? '本应用启动' : overview.data?.embedding.processState === 'failed' ? `启动失败（退出码 ${overview.data.embedding.exitCode}）` : '未由本应用启动'} · 模型：{overview.data?.embedding.model || '未探测到'} · 维度：{overview.data?.embedding.dimension ?? '—'}</Paragraph>
            {overview.data?.embedding.dimensionPending && <Alert type="info" showIcon message="服务已响应；首次编码后会确认向量维度" />}
            {overview.data?.embedding.healthError && <Alert type="warning" showIcon message={overview.data.embedding.healthError} />}
            <Space wrap><Button type="primary" icon={<RocketOutlined />} loading={action.isPending} disabled={working || !overview.data?.embedding.canStart || overview.data?.embedding.healthy} onClick={() => run(ragApi.startEmbedding)}>启动本地服务</Button><Button danger loading={action.isPending} disabled={!overview.data?.embedding.managed} onClick={() => run(ragApi.stopEmbedding)}>关闭本应用启动的服务</Button></Space>
            <Paragraph type="secondary" className="rag-hint">启动后模型加载可能需要几分钟。外部运行的服务只能查看，不能从这里关闭。</Paragraph>
          </Card>
          <Card className="rag-card" title="Milvus 集合配置" extra={<DatabaseOutlined />}>
            <div className="rag-config"><span>集合 <b>{overview.data?.milvus.collection ?? '—'}</b></span><span>向量维度 <b>{overview.data?.milvus.dimension ?? '—'}</b></span><span>距离指标 <b>{overview.data?.milvus.metric ?? '—'}</b></span><span>索引 <b>{overview.data?.milvus.index ?? '—'}</b></span></div>
            <Paragraph type="secondary" className="rag-hint">这里显示服务端配置；集合会在向量化时检查或创建。</Paragraph>
          </Card>
        </div>
        <div className="rag-counts">
          <StatusCount title="待向量化" count={overview.data?.chunksByStatus.pending ?? 0} />
          <StatusCount title="已向量化" count={overview.data?.chunksByStatus.vectorized ?? 0} />
          <StatusCount title="待人工审核" count={overview.data?.chunksByStatus.need_manual_review ?? 0} />
          <StatusCount title="已作废块" count={overview.data?.chunksByStatus.superseded ?? 0} />
        </div>
        <Card className="rag-card" title="Milvus 实际数据（只读）" extra={<Space><Button disabled={milvusOffset === 0} onClick={() => setMilvusOffset(Math.max(0, milvusOffset - 20))}>上一页</Button><Text>{Math.floor(milvusOffset / 20) + 1}</Text><Button disabled={!milvus.data?.count || milvusOffset + 20 >= milvus.data.count} onClick={() => setMilvusOffset(milvusOffset + 20)}>下一页</Button></Space>}>
          <Paragraph type="secondary">集合统计数可能短暂滞后；下方仅展示标量字段，不加载高维向量。MySQL 状态仍是知识是否有效的依据。</Paragraph>
          {milvus.isError && <Alert type="error" showIcon message="读取 Milvus 失败" description={errorText(milvus.error)} />}
          {milvus.data?.error && <Alert type="error" showIcon message="读取 Milvus 失败" description={milvus.data.error} />}
          {milvus.data?.mysqlError && <Alert type="warning" showIcon message="无法核对 MySQL 状态" description={milvus.data.mysqlError} />}
          {milvus.data?.exists === false && <Empty description="Milvus 集合尚未创建；首次向量化会创建集合" />}
          {milvus.data?.exists && <><Space wrap className="rag-status-tags"><Tag>集合 {milvus.data.collection}</Tag><Tag>实际维度 {milvus.data.dimension ?? '未知'}</Tag><Tag>集合统计数 {milvus.data.count ?? '未知'}</Tag><Tag>MySQL 已向量化 {overview.data?.databaseError ? '不可用' : overview.data?.chunksByStatus.vectorized ?? '未知'}</Tag></Space><div className="rag-stack">{milvus.data.items.map((item) => <Card key={item.chunkId} size="small"><Space wrap><Text code>{item.chunkId}</Text><Tag color={item.mysqlStatus === 'vectorized' ? 'green' : 'orange'}>MySQL {item.mysqlStatus}</Tag><Tag>{item.sourceType}</Tag><Text>{item.category}</Text></Space><Paragraph type="secondary" className="rag-job-note">{[item.sourcePath, item.sectionPath, item.contentType].filter(Boolean).join(' · ')}</Paragraph></Card>)}{!milvus.data.items.length && <Empty description="集合暂无记录" />}</div></>}
        </Card>
        <Card className="rag-card" title="Markdown 切块预览" extra={<Space wrap><Select className="rag-file-select" placeholder="选择知识文档" value={file} onChange={setFile} options={(overview.data?.documents ?? []).map((item) => ({ value: item, label: item }))} /><Button type="primary" disabled={!file || working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('import-markdown', file))}>导入此文档到 MySQL</Button></Space>}>
          <Paragraph type="secondary">预览仅读取 backend/knowledge 内的 Markdown，不写数据库。导入后新块先处于 pending。</Paragraph>
          {preview.isError && <Alert type="error" showIcon message="切块预览失败" description={errorText(preview.error)} />}
          {preview.isLoading && file && <Spin />}
          {preview.data && <><Text strong>{preview.data.title} · {preview.data.chunks.length} 块</Text><div className="rag-stack rag-preview-list">{preview.data.chunks.map((chunk, index) => <ChunkCard key={`${chunk.sectionPath}-${index}`} chunk={chunk} />)}</div></>}
        </Card>
        <Card className="rag-card" title="入库与双写任务">
          <Space wrap><Button disabled={working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('import-markdown-all'))}>导入全部 Markdown 到 MySQL</Button><Button disabled={working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('import-faq'))}>导入启用的 FAQ 到 MySQL</Button><Button disabled={working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('mine'))}>对话挖掘一轮</Button><Button type="primary" disabled={working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('vectorize'))}>向量化 pending 并双写</Button><Button disabled={working} loading={action.isPending} onClick={() => run(() => ragApi.startJob('cleanup'))}>清理已作废向量</Button></Space>
          <Paragraph type="secondary" className="rag-hint">挖掘任务先写候选和正式知识；向量化由下一步单独触发。清理只删除已作废块的 Milvus 向量。</Paragraph>
          <Title level={5}>任务状态</Title>{jobs.isError ? <Alert type="error" message={errorText(jobs.error)} /> : <JobPanel jobs={jobs.data?.items ?? []} />}
        </Card>
        <Card className="rag-card" title="对话挖掘批次与候选">
          <Space wrap className="rag-status-tags">{Object.entries(overview.data?.batchesByStatus ?? {}).map(([status, count]) => <Tag key={status}>{status}: {count}</Tag>)}{Object.entries(overview.data?.candidatesByStatus ?? {}).map(([status, count]) => <Tag key={status} color="blue">候选 {status}: {count}</Tag>)}</Space>
          {mining.isError ? <Alert type="error" message={errorText(mining.error)} /> : <Tabs items={[
            { key: 'batches', label: '最近批次', children: <div className="rag-stack">{mining.data?.batches.map((batch) => <Card key={batch.id} size="small"><Space wrap><Tag color={batch.status === 'failed' ? 'red' : batch.status === 'promoted' ? 'green' : 'blue'}>{batch.status}</Tag><Text>轮次 {batch.turnCount} · 候选 {batch.candidateCount}</Text><Text type="secondary">{batch.updatedAt ? new Date(batch.updatedAt).toLocaleString('zh-CN') : ''}</Text>{batch.runId && <Text type="secondary">运行 {batch.runId.slice(0, 8)}</Text>}</Space>{batch.error && <Paragraph type="danger">{batch.error}</Paragraph>}</Card>)}{!mining.data?.batches.length && <Empty description="暂无批次" />}</div> },
            ...(['staged', 'promoted', 'rejected'] as const).map((status) => ({ key: status, label: `候选 ${status}`, children: <div className="rag-stack">{(mining.data?.candidates[status] ?? []).map((candidate) => <Card key={candidate.candidate_id} size="small" title={candidate.question} extra={<Tag>{candidate.category}</Tag>}><Paragraph>{candidate.answer}</Paragraph>{candidate.rejection_reason && <Text type="danger">{candidate.rejection_reason}</Text>}</Card>)}{!mining.data?.candidates[status]?.length && <Empty description="暂无候选" />}</div> })),
          ]} />}
        </Card>
        <Card className="rag-card" title="已入库 chunks" extra={<Space><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button><Text>{Math.floor(offset / 20) + 1}</Text><Button disabled={(chunks.data?.items.length ?? 0) < 20} onClick={() => setOffset(offset + 20)}>下一页</Button></Space>}>
          {chunks.isError ? <Alert type="error" message={errorText(chunks.error)} /> : <div className="rag-stack">{chunks.data?.items.map((chunk) => <ChunkCard key={chunk.id} chunk={chunk} />)}{!chunks.data?.items.length && <Empty description="暂无已导入知识块" />}</div>}
        </Card>
      </>}
    </main>
  </div>;
}
