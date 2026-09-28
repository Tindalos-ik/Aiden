import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Alert, Avatar, Button, Drawer, Empty, Input, Popconfirm, Spin, Tag, message } from 'antd';
import { ArrowUpOutlined, CheckOutlined, ClockCircleOutlined, CustomerServiceOutlined, MessageOutlined, UserOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import { api, apiMode } from '../api';
import { PageHeader, StatusPill } from '../components/Common';
import { MessageBubble } from './UserWorkspace';
import type { Actor, Conversation, Message as ChatMessageType } from '../types';

function QueueList({ items, selectedId, onSelect, compact = false }: { items: Conversation[]; selectedId?: string; onSelect: (item: Conversation) => void; compact?: boolean }) {
  const waiting = items.filter((item) => item.status === 'waiting');
  const active = items.filter((item) => item.status === 'staff');
  const closed = items.filter((item) => item.status === 'closed');
  return <div className={`queue-pane ${compact ? 'queue-pane-compact' : ''}`}>
    <div className="queue-pane-title"><span className="section-kicker">INBOX</span><h2>服务队列</h2><p>接入会话并提供及时协助</p></div>
    <div className="queue-scroll">
      <div className="queue-group"><div className="queue-group-heading"><span>待接入</span><Tag className="queue-count">{waiting.length}</Tag></div>
        {!waiting.length ? <div className="queue-group-empty"><CheckOutlined /> 当前没有等待中的会话</div> : waiting.map((item) => <QueueItem key={item.id} item={item} selected={selectedId === item.id} onClick={() => onSelect(item)} />)}
      </div>
      <div className="queue-group queue-active-group"><div className="queue-group-heading"><span>服务中</span><Tag className="queue-count queue-count-active">{active.length}</Tag></div>
        {!active.length ? <div className="queue-group-empty queue-group-empty-quiet">接入会话后会显示在这里</div> : active.map((item) => <QueueItem key={item.id} item={item} selected={selectedId === item.id} onClick={() => onSelect(item)} />)}
      </div>
      <div className="queue-group"><div className="queue-group-heading"><span>已结束</span><Tag className="queue-count">{closed.length}</Tag></div>
        {closed.map((item) => <QueueItem key={item.id} item={item} selected={selectedId === item.id} onClick={() => onSelect(item)} />)}
      </div>
    </div>
    <div className="queue-pane-footer"><span className="queue-online-dot" />客服工作台在线<span className="queue-footer-divider">·</span>{apiMode === 'mock' ? '标签页实时同步' : '自动刷新中'}</div>
  </div>;
}

function QueueItem({ item, selected, onClick }: { item: Conversation; selected: boolean; onClick: () => void }) {
  return <button className={`queue-item ${selected ? 'queue-item-selected' : ''}`} onClick={onClick}>
    <Avatar className={`queue-avatar ${item.status === 'waiting' ? 'queue-avatar-waiting' : ''}`} icon={<UserOutlined />} />
    <span className="queue-item-copy"><span className="queue-item-name">{item.userName || `用户 ${item.userId.slice(-4)}`}<time>{new Date(item.updatedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></span><span className="queue-item-subject">{item.subject || '订单咨询'}</span><span className="queue-item-preview">{item.lastMessagePreview || '申请人工客服接入'}</span></span>
    {item.status === 'waiting' && <span className="queue-item-wait-dot" />}
  </button>;
}

export function StaffWorkspace() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { conversationId } = useParams();
  const [draft, setDraft] = useState('');
  const [mobileQueueOpen, setMobileQueueOpen] = useState(false);
  const [selectedSnapshot, setSelectedSnapshot] = useState<Conversation | undefined>();
  const [mutationError, setMutationError] = useState('');
  const endRef = useRef<HTMLDivElement>(null);
  const actorQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const actor = actorQuery.data as Actor | undefined;
  const queueQuery = useQuery({ queryKey: ['staff-queue'], queryFn: api.listQueue, refetchInterval: apiMode === 'remote' ? 3_000 : false });
  const queue = queueQuery.data ?? [];
  const fromQueue = queue.find((item) => item.id === conversationId);
  const conversation = fromQueue ?? (selectedSnapshot?.id === conversationId ? selectedSnapshot : undefined);
  const messagesQuery = useQuery({
    queryKey: ['messages', conversationId], queryFn: () => api.listStaffMessages(conversationId!), enabled: Boolean(conversationId),
    refetchInterval: apiMode === 'remote' && conversation?.status !== 'closed' ? 2_500 : false,
  });
  const messages = messagesQuery.data ?? [];
  const waitingCount = queue.filter((item) => item.status === 'waiting').length;

  useEffect(() => {
    if (!conversationId && queue.length) {
      setSelectedSnapshot(queue[0]);
      navigate(`/staff/${queue[0].id}`, { replace: true });
    }
  }, [conversationId, queue, navigate]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages]);
  useEffect(() => {
    if (fromQueue) setSelectedSnapshot(fromQueue);
    else if (conversationId && selectedSnapshot?.id === conversationId && selectedSnapshot.status !== 'closed' && !queueQuery.isLoading && !queueQuery.isFetching && !queueQuery.isError) {
      setSelectedSnapshot(undefined);
      navigate('/staff', { replace: true });
    }
  }, [fromQueue, selectedSnapshot, conversationId, queueQuery.isLoading, queueQuery.isFetching, queueQuery.isError, navigate]);

  const choose = (item: Conversation) => {
    setSelectedSnapshot(item);
    navigate(`/staff/${item.id}`);
    setMobileQueueOpen(false);
    setMutationError('');
  };
  const refresh = async () => {
    await Promise.all([queryClient.invalidateQueries({ queryKey: ['staff-queue'] }), queryClient.invalidateQueries({ queryKey: ['messages', conversationId] })]);
  };
  const accept = useMutation({
    mutationFn: () => api.acceptConversation(conversationId!),
    onSuccess: async (updated) => {
      setSelectedSnapshot({ ...updated, userName: conversation?.userName });
      setMutationError('');
      await refresh();
      message.success('已接入会话');
    },
    onError: (error) => setMutationError(error instanceof Error ? error.message : '接入失败，请重试'),
  });
  const close = useMutation({
    mutationFn: () => api.closeConversation(conversationId!),
    onSuccess: async (updated) => {
      setSelectedSnapshot({ ...updated, userName: conversation?.userName });
      setMutationError('');
      await refresh();
      message.success('服务已结束');
    },
    onError: (error) => setMutationError(error instanceof Error ? error.message : '结束服务失败'),
  });
  const send = useMutation({
    mutationFn: () => api.sendStaffMessage(conversationId!, draft.trim()),
    onSuccess: async (sent) => {
      setDraft('');
      queryClient.setQueryData<ChatMessageType[]>(['messages', conversationId], (items = []) => items.some((item) => item.id === sent.id) ? items : [...items, sent]);
      setSelectedSnapshot((previous) => previous ? { ...previous, updatedAt: sent.createdAt, lastMessagePreview: sent.content } : previous);
      await queryClient.invalidateQueries({ queryKey: ['staff-queue'] });
    },
    onError: (error) => setMutationError(error instanceof Error ? error.message : '消息发送失败'),
  });
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (draft.trim() && conversation?.status === 'staff') send.mutate();
    }
  };

  const queuePane = <QueueList items={queue} selectedId={conversationId} onSelect={choose} />;
  if (actorQuery.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" description="请重新登录后继续使用。" /></div>;

  return <div className="workspace-shell staff-shell">
    <PageHeader actor={actor} />
    <main className="staff-workspace">
      <aside className="staff-sidebar">
        {queueQuery.isLoading ? <div className="sidebar-loading"><Spin /></div> : queueQuery.isError ? <div className="sidebar-error"><Alert type="error" showIcon message="服务队列加载失败" description={queueQuery.error instanceof Error ? queueQuery.error.message : ''} action={<Button size="small" onClick={() => void queueQuery.refetch()}>重试</Button>} /></div> : queuePane}
      </aside>
      <section className="staff-chat-panel">
        {!conversation && !queueQuery.isLoading ? <div className="staff-inbox-empty">
          <div className="inbox-empty-art"><span className="inbox-empty-ring" /><span className="inbox-empty-icon"><CustomerServiceOutlined /></span><i className="inbox-empty-spark">✦</i></div>
          <div className="inbox-empty-eyebrow">CUSTOMER CARE</div><h2>{queue.length ? '选择一段会话' : '此刻，一切从容'}</h2><p>{queue.length ? '打开左侧队列查看完整对话并接入服务。' : '当前没有待接入会话。用户申请转人工后，会实时出现在这里。'}</p>
          {waitingCount > 0 && <Button type="primary" onClick={() => choose(queue.find((item) => item.status === 'waiting')!)}>查看 {waitingCount} 个待接入会话</Button>}
          {queueQuery.isError && <Button onClick={() => void queueQuery.refetch()}>重新载入队列</Button>}
        </div> : queueQuery.isLoading && !conversation ? <div className="chat-load-state"><Spin /><span>正在连接客服队列…</span></div> : conversation && <>
          <div className="staff-chat-header">
            <div className="chat-title-cluster"><Button className="mobile-tools" type="text" icon={<MessageOutlined />} onClick={() => setMobileQueueOpen(true)} aria-label="打开服务队列" /><Avatar className="staff-customer-avatar" icon={<UserOutlined />} /><div><div className="chat-title">{conversation.userName || `用户 ${conversation.userId.slice(-4)}`}<span className="staff-user-id">{conversation.userId}</span></div><div className="chat-title-sub">{conversation.subject || '订单咨询'} · {messages.length} 条消息</div></div></div>
            <div className="staff-header-actions"><StatusPill status={conversation.status} />{conversation.status === 'waiting' && <Button type="primary" icon={<CheckOutlined />} loading={accept.isPending} onClick={() => accept.mutate()}>接入会话</Button>}{conversation.status === 'staff' && <Popconfirm title="结束本次服务？" description="结束后，用户仍可查看完整对话。" okText="结束服务" cancelText="继续服务" onConfirm={() => close.mutate()}><Button className="close-service-button" danger loading={close.isPending}>结束服务</Button></Popconfirm>}</div>
          </div>
          {mutationError && <Alert className="inline-alert" type="error" showIcon message={mutationError} closable onClose={() => setMutationError('')} />}
          <div className="staff-conversation-area">
            {conversation.status === 'waiting' && <div className="staff-waiting-banner"><ClockCircleOutlined /><span>用户正在等待人工客服接入。请先查看完整对话，再选择接入。</span></div>}
            {conversation.status === 'closed' && <div className="closed-banner"><span>此会话已结束，用户可以查看完整服务记录。</span><Tag>已归档</Tag></div>}
            {messagesQuery.isLoading ? <div className="chat-load-state"><Spin /><span>正在载入完整对话…</span></div> : messagesQuery.isError ? <div className="chat-load-state"><Alert type="error" showIcon message="对话载入失败" description={messagesQuery.error instanceof Error ? messagesQuery.error.message : '请检查连接后重试'} action={<Button size="small" onClick={() => void messagesQuery.refetch()}>重试</Button>} /></div> : messages.length ? <div className="staff-message-timeline">{messages.map((item) => <MessageBubble item={item} viewerRole="staff" userName={conversation.userName} key={item.id} />)}<div ref={endRef} /></div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前会话还没有消息" />}
          </div>
          <div className="staff-composer-wrap">
            {conversation.status === 'staff' ? <div className="staff-composer-box"><Input.TextArea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown} autoSize={{ minRows: 1, maxRows: 5 }} placeholder="回复用户，清晰说明处理进度…" disabled={send.isPending} aria-label="客服回复" /><div className="composer-toolbar"><span className="composer-hint">Enter 发送 · Shift + Enter 换行</span><Button type="primary" icon={<ArrowUpOutlined />} loading={send.isPending} disabled={!draft.trim()} onClick={() => send.mutate()}>发送回复</Button></div></div> : <div className="staff-composer-locked"><span className="staff-composer-icon"><CustomerServiceOutlined /></span><span>{conversation.status === 'waiting' ? '接入会话后即可向用户发送消息' : '会话已结束，消息记录仅供查看'}</span></div>}
            <div className="staff-composer-footer"><span>服务对象：{conversation.userName || conversation.userId}</span><span><span className="queue-online-dot" />服务记录实时同步</span></div>
          </div>
        </>}
      </section>
    </main>
    <Drawer title={<span>服务队列 <Tag className="queue-count">{waitingCount}</Tag></span>} placement="left" width={330} open={mobileQueueOpen} onClose={() => setMobileQueueOpen(false)} styles={{ body: { padding: 0 } }}>{queuePane}</Drawer>
  </div>;
}
