import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Empty, Input, Select, Space, Spin, Tabs, Tag, Typography, message } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, apiMode } from '../api';
import { PageHeader } from '../components/Common';
import type { AfterSaleApplication, AfterSalePreview, ServiceTicket, SubmitAfterSaleInput } from '../types';

const requestText: Record<string, string> = { refund: '退款', return: '退货', exchange: '换货' };
const requestStatus: Record<string, string> = { pending: '待审核', approved: '审核通过，待处理', rejected: '已驳回', processing: '处理中', awaiting_external_refund: '待外部支付退款', completed: '已完成', cancelled: '已取消' };
const ticketStatus: Record<string, string> = { open: '待接单', in_progress: '处理中', resolved: '待关闭', closed: '已关闭' };

function ErrorLine({ error }: { error: unknown }) {
  return error ? <Alert type="error" showIcon message={error instanceof Error ? error.message : '操作失败'} style={{ margin: '12px 0' }} /> : null;
}

function ApplicationCard({ row, action }: { row: AfterSaleApplication; action?: React.ReactNode }) {
  return <Card size="small" style={{ marginBottom: 12 }} title={<Space><b>{row.requestNo}</b><Tag>{requestText[row.requestType]}</Tag><Tag color="blue">{requestStatus[row.status]}</Tag></Space>} extra={action}>
    <p>订单：{row.orderNo} · 原因：{row.reason}</p>
    {row.itemName && <p>商品：{row.itemName}</p>}
    {row.policyName && <p>提交依据：{row.policyName}</p>}
    {row.policyEvidence && <p style={{ whiteSpace: 'pre-wrap' }}>政策证据：{row.policyEvidence}</p>}
    {row.decisionNote && <p>处理说明：{row.decisionNote}</p>}
    <Typography.Text type="secondary">提交：{new Date(row.createdAt).toLocaleString('zh-CN')} {row.reviewedAt && `· 审核：${new Date(row.reviewedAt).toLocaleString('zh-CN')}`}</Typography.Text>
    {row.requestType === 'refund' && <Alert style={{ marginTop: 10 }} type="info" message="审核通过不代表款项已退；实际退款需外部支付系统执行。" />}
  </Card>;
}

function TicketCard({ row, action, linkedRequestNo }: { row: ServiceTicket; action?: React.ReactNode; linkedRequestNo?: string }) {
  const status = ticketStatus[row.status] ?? row.status;
  return <Card size="small" style={{ marginBottom: 12 }} title={<span style={{ overflowWrap: 'anywhere' }}>{row.ticketNo}</span>} extra={action}>
    <p><Typography.Text strong>当前状态：</Typography.Text><Tag color={row.status === 'closed' ? 'default' : row.status === 'open' ? 'gold' : 'blue'}>{status}</Tag></p>
    <p>{row.description}</p>
    {row.afterSaleRequestId && <p>关联售后申请：{linkedRequestNo ?? row.afterSaleRequestId}</p>}
    {row.resolutionNote && <p>处理结果：{row.resolutionNote}</p>}
    <Typography.Text type="secondary">创建：{new Date(row.createdAt).toLocaleString('zh-CN')}</Typography.Text>
  </Card>;
}

export function UserServicePage() {
  const [searchParams] = useSearchParams();
  const requestedOrderNo = searchParams.get('orderNo')?.trim() ?? '';
  const requestedType = searchParams.get('requestType');
  const requestedReason = searchParams.get('reason') ?? '';
  const validRequestType = requestedType === 'refund' || requestedType === 'return' || requestedType === 'exchange' ? requestedType : null;
  const client = useQueryClient();
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me });
  const orders = useQuery({ queryKey: ['service-orders'], queryFn: api.listServiceOrders });
  const applications = useQuery({ queryKey: ['after-sales'], queryFn: api.listAfterSales });
  const tickets = useQuery({ queryKey: ['tickets'], queryFn: api.listTickets });
  const [orderNo, setOrderNo] = useState(requestedOrderNo);
  const [kind, setKind] = useState<SubmitAfterSaleInput['requestType']>(validRequestType ?? 'refund');
  const [preview, setPreview] = useState<AfterSalePreview | null>(null);
  const [itemId, setItemId] = useState('');
  const [policyId, setPolicyId] = useState('');
  const [reason, setReason] = useState(requestedReason);
  const [sourceTicketNo, setSourceTicketNo] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [submissionKey, setSubmissionKey] = useState(() => crypto.randomUUID());
  const preserveSelectionRef = useRef(false);
  const previewSequence = useRef(0);
  const previewMutation = useMutation({
    mutationFn: ({ targetOrderNo, targetKind }: { targetOrderNo: string; targetKind: SubmitAfterSaleInput['requestType']; sequence: number }) => api.previewAfterSale(targetOrderNo, targetKind),
    onSuccess: (result, variables) => {
      if (variables.sequence !== previewSequence.current) return;
      const refreshingConflict = preserveSelectionRef.current;
      setPreview(result);
      setItemId((current) => refreshingConflict && result.items.some((item) => item.id === current) ? current : result.items.length === 1 ? result.items[0].id : '');
      setPolicyId((current) => refreshingConflict && result.policies.some((policy) => policy.id === current) ? current : result.policies.length === 1 ? result.policies[0].id : '');
      preserveSelectionRef.current = false;
      setConfirmed(false);
      if (refreshingConflict) message.info('当前售后政策已重新核对，请重新确认后再提交。');
    },
  });
  const requestPreview = (targetOrderNo: string, targetKind: SubmitAfterSaleInput['requestType']) => {
    setConfirmed(false);
    setPreview(null);
    previewMutation.mutate({ targetOrderNo, targetKind, sequence: ++previewSequence.current });
  };
  useEffect(() => {
    previewSequence.current += 1;
    setOrderNo(requestedOrderNo);
    setKind(validRequestType ?? 'refund');
    setReason(requestedReason);
    setPreview(null);
    setItemId('');
    setPolicyId('');
    setConfirmed(false);
    preserveSelectionRef.current = false;
    if (requestedOrderNo && validRequestType) requestPreview(requestedOrderNo, validRequestType);
  }, [requestedOrderNo, requestedType, requestedReason]);
  const selectedPolicy = preview?.policies.find((policy) => policy.id === policyId);
  const submit = useMutation({
    mutationFn: () => api.submitAfterSale({ orderNo, orderItemId: itemId, requestType: kind,
      reason: reason.trim(), policyReference: policyId, policySnapshot: selectedPolicy!.snapshot, submissionKey, confirmed,
      sourceTicketNo: sourceTicketNo || undefined }),
    onSuccess: async (row) => {
      message.success(row.alreadyExists ? `已有申请 ${row.requestNo}` : `已提交申请 ${row.requestNo}`);
      setSubmissionKey(crypto.randomUUID()); setConfirmed(false); setReason(''); setPreview(null);
      await Promise.all([client.invalidateQueries({ queryKey: ['after-sales'] }), client.invalidateQueries({ queryKey: ['tickets'] })]);
    },
    onError: (error) => {
      if (typeof error === 'object' && error !== null && 'status' in error && error.status === 409) {
        setConfirmed(false);
        preserveSelectionRef.current = true;
        requestPreview(orderNo, kind);
        message.warning('提交条件冲突，正在重新核对政策，请等待并重新确认。');
      }
    },
  });
  const cancel = useMutation({
    mutationFn: api.cancelAfterSale,
    onSuccess: async () => { message.success('申请已取消'); await client.invalidateQueries({ queryKey: ['after-sales'] }); },
  });
  if (!actor.data) return <Spin />;
  return <div className="workspace-shell"><PageHeader actor={actor.data} /><main style={{ maxWidth: 1000, margin: '28px auto', padding: 16 }}>
    <Typography.Title level={3}>我的售后与工单</Typography.Title>
    {apiMode === 'mock' && <Alert type="info" showIcon message="本地演示模式：申请、政策和进度仅保存在浏览器，不代表真实订单或退款。" style={{ marginBottom: 16 }} />}
    <Tabs items={[
      { key: 'submit', label: '提交售后申请', children: <Card title="申请退款、退货或换货">
        <Space wrap style={{ marginBottom: 12 }}>
          <Select disabled={submit.isPending} placeholder="选择本人订单" style={{ minWidth: 260 }} value={orderNo || undefined} options={(orders.data ?? []).map((row) => ({ label: `${row.orderNo} · ${row.items.map((item) => item.name).join('、')}`, value: row.orderNo }))} onChange={(value) => { previewSequence.current += 1; setOrderNo(value); setPreview(null); setConfirmed(false); }} />
          <Select disabled={submit.isPending} value={kind} style={{ width: 110 }} options={Object.entries(requestText).map(([value, label]) => ({ value, label }))} onChange={(value) => { previewSequence.current += 1; setKind(value); setPreview(null); setConfirmed(false); }} />
          <Button loading={previewMutation.isPending} disabled={!orderNo || submit.isPending} onClick={() => requestPreview(orderNo, kind)}>核对订单与政策</Button>
        </Space><ErrorLine error={orders.error ?? previewMutation.error} />
        {preview && <>
          <p>订单状态：{preview.orderStatus}。{preview.items.length > 1 ? '请选择本次申请涉及的具体商品（多商品订单不会自动选择）。' : '请选择本次申请涉及的商品：'}</p>
          <Select disabled={submit.isPending} style={{ width: '100%', marginBottom: 12 }} placeholder="选择商品" value={itemId || undefined} options={preview.items.map((item) => ({ value: item.id, label: `${item.name} × ${item.quantity} · ¥${item.lineTotal}` }))} onChange={(value) => { setItemId(value); setConfirmed(false); }} />
          <p>适用政策：</p>
          {!preview.policies.length && <Alert type="warning" showIcon message="当前没有可核验的有效政策，暂不能提交正式申请。可通过会话咨询客服。" />}
          {preview.policies.map((policy) => <Card size="small" key={policy.id} style={{ marginBottom: 10 }}><Checkbox disabled={submit.isPending} checked={policyId === policy.id} onChange={() => { setPolicyId(policy.id); setConfirmed(false); }}>{policy.name}（{policy.version}）</Checkbox><p style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{policy.content}</p></Card>)}
          <Input.TextArea disabled={submit.isPending} rows={3} value={reason} onChange={(event) => { setReason(event.target.value); setConfirmed(false); }} placeholder="请说明申请原因和商品问题" maxLength={2000} style={{ marginBottom: 12 }} />
          {(tickets.data ?? []).some((item) => !item.afterSaleRequestId) && <Select disabled={submit.isPending} allowClear placeholder="可选：关联已有工单" style={{ width: '100%', marginBottom: 12 }} value={sourceTicketNo || undefined} options={(tickets.data ?? []).filter((item) => !item.afterSaleRequestId).map((item) => ({ value: item.ticketNo, label: `${item.ticketNo} · ${item.description}` }))} onChange={(value) => setSourceTicketNo(value ?? '')} />}
          <Checkbox disabled={submit.isPending} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}>我已核对订单、商品和政策，确认提交正式售后申请供员工审核。</Checkbox>
          <div style={{ marginTop: 14 }}><Button type="primary" loading={submit.isPending} disabled={submit.isPending || previewMutation.isPending || !preview || !selectedPolicy || !itemId || !reason.trim() || !confirmed} onClick={() => submit.mutate()}>提交申请</Button></div>
          <ErrorLine error={submit.error} />
        </>}
      </Card> },
      { key: 'applications', label: '申请进度', children: <><ErrorLine error={applications.error ?? cancel.error} />{(applications.data ?? []).length ? applications.data!.map((row) => <ApplicationCard key={row.id} row={row} action={['pending', 'approved'].includes(row.status) && <Button size="small" danger loading={cancel.isPending} onClick={() => cancel.mutate(row.id)}>取消申请</Button>} />) : <Empty description="暂无售后申请" />}</> },
      { key: 'tickets', label: '工单进度', children: <><ErrorLine error={tickets.error} />{(tickets.data ?? []).length ? tickets.data!.map((row) => <TicketCard key={row.id} row={row} linkedRequestNo={applications.data?.find((app) => app.id === row.afterSaleRequestId)?.requestNo} />) : <Empty description="暂无工单；投诉或异常问题可在会话中请求登记。" />}</> },
    ]} />
  </main></div>;
}

export function StaffServicePage() {
  const client = useQueryClient();
  const actor = useQuery({ queryKey: ['me'], queryFn: api.me });
  const applications = useQuery({ queryKey: ['staff-after-sales'], queryFn: api.listStaffAfterSales, refetchInterval: apiMode === 'remote' ? 5000 : false });
  const tickets = useQuery({ queryKey: ['staff-tickets'], queryFn: api.listStaffTickets, refetchInterval: apiMode === 'remote' ? 5000 : false });
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [links, setLinks] = useState<Record<string, string>>({});
  const requestMutation = useMutation({ mutationFn: ({ id, status }: { id: string; status: string }) => api.transitionAfterSale(id, status, notes[id] ?? ''),
    onSuccess: async () => { message.success('申请状态已更新'); await client.invalidateQueries({ queryKey: ['staff-after-sales'] }); } });
  const ticketMutation = useMutation({ mutationFn: ({ id, status }: { id: string; status: string }) => api.transitionTicket(id, status, notes[id] ?? '', links[id] || undefined),
    onSuccess: async () => { message.success('工单状态已更新'); await client.invalidateQueries({ queryKey: ['staff-tickets'] }); } });
  if (!actor.data) return <Spin />;
  return <div className="workspace-shell"><PageHeader actor={actor.data} /><main style={{ maxWidth: 1100, margin: '28px auto', padding: 16 }}>
    <Typography.Title level={3}>员工售后与工单处理</Typography.Title>
    {apiMode === 'mock' && <Alert type="info" showIcon message="本地演示模式：处理状态仅保存在浏览器。" style={{ marginBottom: 16 }} />}
    <ErrorLine error={requestMutation.error ?? ticketMutation.error} />
    <Tabs items={[
      { key: 'tickets', label: `工单 ${tickets.data?.length ?? 0}`, children: <><ErrorLine error={tickets.error} />{(tickets.data ?? []).length ? tickets.data!.map((row) => <TicketCard key={row.id} row={row} linkedRequestNo={applications.data?.find((app) => app.id === row.afterSaleRequestId)?.requestNo} action={<Space>
        {row.status === 'open' && <Button size="small" onClick={() => ticketMutation.mutate({ id: row.id, status: 'in_progress' })}>接单</Button>}
        {row.status === 'in_progress' && <Button size="small" onClick={() => ticketMutation.mutate({ id: row.id, status: 'resolved' })}>标记已处理</Button>}
        {['in_progress', 'resolved'].includes(row.status) && <Button size="small" onClick={() => ticketMutation.mutate({ id: row.id, status: 'closed' })}>关闭</Button>}
      </Space>} />).map((card, index) => <div key={(tickets.data ?? [])[index]?.id}>{card}{(tickets.data ?? [])[index]?.status !== 'closed' && <Space wrap style={{ marginBottom: 16 }}>
        <Input placeholder="处理说明；处理或关闭时必填" style={{ width: 280 }} value={notes[(tickets.data ?? [])[index].id] ?? ''} onChange={(event) => setNotes({ ...notes, [(tickets.data ?? [])[index].id]: event.target.value })} />
        <Select allowClear placeholder="关联同一用户的申请" style={{ width: 260 }} value={links[(tickets.data ?? [])[index].id] || undefined} options={(applications.data ?? []).filter((app) => app.userId === (tickets.data ?? [])[index].userId).map((app) => ({ value: app.id, label: app.requestNo }))} onChange={(value) => setLinks({ ...links, [(tickets.data ?? [])[index].id]: value ?? '' })} />
      </Space>}</div>) : <Empty description="暂无工单" />}</> },
      { key: 'applications', label: `售后申请 ${applications.data?.length ?? 0}`, children: <><ErrorLine error={applications.error} />{(applications.data ?? []).length ? applications.data!.map((row) => <div key={row.id}><ApplicationCard row={row} action={<Space>
        {row.status === 'pending' && <><Button size="small" onClick={() => requestMutation.mutate({ id: row.id, status: 'approved' })}>审核通过</Button><Button size="small" danger onClick={() => requestMutation.mutate({ id: row.id, status: 'rejected' })}>驳回</Button></>}
        {row.status === 'approved' && <Button size="small" onClick={() => requestMutation.mutate({ id: row.id, status: row.requestType === 'refund' ? 'awaiting_external_refund' : 'processing' })}>{row.requestType === 'refund' ? '转待外部退款' : '开始处理'}</Button>}
        {row.status === 'processing' && <Button size="small" onClick={() => requestMutation.mutate({ id: row.id, status: 'completed' })}>处理完成</Button>}
      </Space>} />{['pending', 'approved', 'processing'].includes(row.status) && <Input style={{ width: 360, marginBottom: 16 }} placeholder="审核或处理说明" value={notes[row.id] ?? ''} onChange={(event) => setNotes({ ...notes, [row.id]: event.target.value })} />}</div>) : <Empty description="暂无售后申请" />}</> },
    ]} />
  </main></div>;
}
