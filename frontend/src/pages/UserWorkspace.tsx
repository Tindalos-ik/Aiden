import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Alert, Avatar, Button, Drawer, Empty, Input, Popover, Spin, Tooltip, message } from 'antd';
import { ArrowUpOutlined, CheckCircleFilled, ClockCircleOutlined, CustomerServiceOutlined, DislikeFilled, DislikeOutlined, FileTextOutlined, LikeFilled, LikeOutlined, PlusOutlined, StopOutlined, UserOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import dayjs from 'dayjs';
import { api, apiMode } from '../api';
import { PageHeader, StatusPill } from '../components/Common';
import type { Actor, Citation, Conversation, Message as ChatMessageType, OrderCard, StreamEvent } from '../types';

type AnswerFeedback = 'satisfied' | 'unsatisfied';

function FeedbackControls({ item }: { item: ChatMessageType }) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (selected: AnswerFeedback) => {
    if (item.feedback || pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setError(null);
    try {
      const result = await api.submitFeedback(item.conversationId, item.id, selected);
      queryClient.setQueryData<ChatMessageType[]>(['messages', item.conversationId], (items) =>
        items?.map((entry) => entry.id === item.id ? { ...entry, feedback: result.rating } : entry));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '反馈提交失败，请重试');
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };
  const rating = item.feedback;
  return <div className="answer-feedback" aria-label="回答反馈">
    <button type="button" className={rating === 'satisfied' ? 'feedback-selected' : ''} disabled={Boolean(rating) || pending} aria-label="满意" aria-pressed={rating === 'satisfied'} onClick={() => submit('satisfied')}>{rating === 'satisfied' ? <LikeFilled /> : <LikeOutlined />}<span>满意</span></button>
    <button type="button" className={rating === 'unsatisfied' ? 'feedback-selected' : ''} disabled={Boolean(rating) || pending} aria-label="不满意" aria-pressed={rating === 'unsatisfied'} onClick={() => submit('unsatisfied')}>{rating === 'unsatisfied' ? <DislikeFilled /> : <DislikeOutlined />}<span>不满意</span></button>
    {rating && <span className="feedback-confirmed">已反馈</span>}
    {error && <span role="alert" className="message-state-error">{error}</span>}
  </div>;
}

function safeSourceUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch { return null; }
}

function CitationContent({ citation }: { citation: Citation }) {
  const sourceUrl = safeSourceUrl(citation.sourceUrl);
  return <div className="citation-detail">
    <div className="citation-section">{citation.sectionPath || '未标注章节'}</div>
    {citation.sourcePath && <div className="citation-path">{citation.sourcePath}</div>}
    <div className="citation-original">{citation.content}</div>
    {sourceUrl ? <a href={sourceUrl} target="_blank" rel="noopener noreferrer">跳回原文 ↗</a> : <span className="citation-unavailable">暂无原文链接</span>}
  </div>;
}

function AnswerContent({ item }: { item: ChatMessageType }) {
  const content = plainMessageText(item.content);
  const citations = new Map((item.role === 'assistant' ? item.citations ?? [] : []).map((citation) => [citation.number, citation]));
  return <>{content.split(/(\[[^\]]+\]\(\/app\/service(?:\?[^)\s]*)?\)|\[\d+\])/g).map((part, index) => {
    const serviceLink = /^\[([^\]]+)\]\((\/app\/service(?:\?[^)\s]*)?)\)$/.exec(part);
    if (serviceLink) return <a key={index} href={serviceLink[2]}>{serviceLink[1]}</a>;
    const number = /^\[(\d+)\]$/.exec(part)?.[1];
    const citation = number ? citations.get(Number(number)) : undefined;
    return citation ? <Popover key={index} trigger="click" title={`来源 [${citation.number}]`} content={<CitationContent citation={citation} />}><button type="button" className="citation-marker" aria-label={`查看来源 ${citation.number}`}>{part}</button></Popover> : part;
  })}</>;
}

const suggestions = [
  '我的订单到哪了？',
  '帮我查一下退款进度',
  '订单签收了，但我还没收到',
];

const timeLabel = (date: string) => {
  const value = dayjs(date);
  return value.isSame(dayjs(), 'day') ? value.format('HH:mm') : value.format('MM-DD HH:mm');
};

function plainMessageText(content: string) {
  return content
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s*#{1,6}\s+/gm, '')
    .replace(/^\s*[-*]\s+/gm, '• ');
}

function OrderCardView({ order }: { order: OrderCard }) {
  const statusClass = order.status === '退款中' ? 'order-refund' : order.status === '已签收' ? 'order-delivered' : 'order-transit';
  return <div className="order-card">
    <div className="order-card-top"><span className="order-card-icon"><FileTextOutlined /></span><div><strong>订单信息</strong><small>订单号 {order.orderId}</small></div><span className={`order-status ${statusClass}`}>{order.status}</span></div>
    <div className="order-product"><span className="product-thumb">{order.product.slice(0, 1)}</span><div><strong>{order.product}</strong><span>商品金额 <b>¥{order.amount.toFixed(2)}</b></span></div></div>
    <div className="order-logistics"><span className="logistics-line" /><span className="logistics-label">{order.carrier || '物流进度'}</span><span>{order.logistics}</span></div>
    <div className="order-updated">{order.updatedAt ? `更新于 ${order.updatedAt}` : '物流信息以承运方更新为准'}</div>
  </div>;
}

export function MessageBubble({ item, feedbackUserId, viewerRole = 'user', userName }: { item: ChatMessageType; feedbackUserId?: string; viewerRole?: 'user' | 'staff'; userName?: string }) {
  if (item.role === 'system') return <div className="system-message"><span>{item.content}</span></div>;
  const fromUser = item.role === 'user';
  const fromStaff = item.role === 'staff';
  const fromSelf = item.role === viewerRole;
  const avatar = fromUser ? <UserOutlined /> : fromStaff ? <CustomerServiceOutlined /> : <span className="mini-brand-mark">a</span>;
  const avatarClass = `chat-avatar ${fromUser ? 'user-avatar' : fromStaff ? 'human-avatar' : ''}`;
  return <div className={`chat-row ${fromSelf ? 'chat-row-user' : 'chat-row-agent'}`}>
    {!fromSelf && <Avatar className={avatarClass} icon={avatar} />}
    <div className="chat-column">
      <div className={`message-byline ${fromSelf ? 'byline-user' : ''}`}>{fromSelf ? '我' : fromUser ? userName || '用户' : fromStaff ? '人工客服' : 'Aiden'}<time>{timeLabel(item.createdAt)}</time></div>
      <div className={`message-bubble ${fromSelf ? 'bubble-user' : 'bubble-agent'} ${item.status === 'error' ? 'bubble-error' : ''}`}>
        {item.content && <div className="message-content"><AnswerContent item={item} /></div>}
        {!item.content && item.status === 'streaming' && <div className="typing-dots"><i /><i /><i /></div>}
        {item.orderCard && <OrderCardView order={item.orderCard} />}
        {item.toolStatuses && item.toolStatuses.length > 0 && <div className="tool-trail">{item.toolStatuses.map((status, index) => <div key={`${status}-${index}`}><CheckCircleFilled />{status}</div>)}</div>}
        {item.status === 'streaming' && <span className="stream-cursor" />}
        {item.status === 'stopped' && <div className="message-state-note"><StopOutlined /> 已停止生成</div>}
        {item.status === 'error' && <div className="message-state-note message-state-error"><span /> 这条回复没有完成</div>}
      </div>
      {item.role === 'assistant' && item.status === 'complete' && feedbackUserId && <FeedbackControls key={`${feedbackUserId}:${item.conversationId}:${item.id}`} item={item} />}
    </div>
    {fromSelf && <Avatar className={avatarClass} icon={avatar} />}
  </div>;
}

function ConversationList({ conversations, selectedId, onSelect, onCreate, compact = false }: {
  conversations: Conversation[]; selectedId?: string; onSelect: (id: string) => void; onCreate: () => void; compact?: boolean;
}) {
  return <div className={`conversation-list-pane ${compact ? 'conversation-list-compact' : ''}`}>
    <div className="conversation-list-header"><div><span className="section-kicker">SUPPORT</span><h2>我的会话</h2></div><Tooltip title="新建会话"><Button type="primary" shape="circle" icon={<PlusOutlined />} onClick={onCreate} aria-label="新建会话" /></Tooltip></div>
    <div className="conversation-list-scroll">
      {conversations.length ? conversations.map((conversation) => <button key={conversation.id} className={`conversation-item ${selectedId === conversation.id ? 'conversation-item-active' : ''}`} onClick={() => onSelect(conversation.id)}>
        <span className="conversation-icon"><span className="mini-brand-mark">a</span></span>
        <span className="conversation-item-main"><span className="conversation-item-title">{conversation.subject || '订单咨询'}</span><span className="conversation-item-preview">{conversation.lastMessagePreview || '向 Aiden 咨询订单问题'}</span></span>
        <span className="conversation-item-meta"><time>{timeLabel(conversation.updatedAt)}</time><i className={`conversation-state-dot dot-${conversation.status}`} /></span>
      </button>) : <div className="conversation-empty"><span><FileTextOutlined /></span><b>还没有会话</b><small>创建一个新会话开始咨询</small><Button type="primary" icon={<PlusOutlined />} onClick={onCreate}>新建会话</Button></div>}
    </div>
    <div className="conversation-list-footer"><span className="privacy-shield">✓</span><span>仅展示你账号下的订单与对话</span></div>
  </div>;
}

function WelcomePanel({ onAsk }: { onAsk: (text: string) => void }) {
  const visibleSuggestions = apiMode === 'mock'
    ? suggestions
    : ['我的订单到哪了？', '帮我查一下售后申请进度', '退货需要满足哪些条件？'];
  return <div className="welcome-panel">
    <div className="welcome-illustration"><div className="welcome-orbit orbit-one" /><div className="welcome-orbit orbit-two" /><div className="welcome-bubble"><span className="brand-mark"><span /><span /><span /><span /></span></div><span className="welcome-spark spark-one">✦</span><span className="welcome-spark spark-two">✳</span><span className="welcome-spark spark-three">·</span></div>
    <div className="welcome-eyebrow">YOUR ORDER, IN GOOD HANDS</div>
    <h2>你好，我是 Aiden<span>。</span></h2>
    <p>{apiMode === 'mock' ? <>我可以帮你查询订单、物流和退款进度。<br />需要更多帮助时，也可以随时转接人工客服。</> : <>我可以查询本人订单、物流和售后进度，依据已入库知识解答商品与政策问题。<br />需要人工协助时，可申请站内排队，客服接单后在本会话回复。</>}</p>
    <div className="suggestions-label">试着问我</div>
    <div className="suggestion-list">{visibleSuggestions.map((item, index) => <button key={item} className="suggestion-chip" onClick={() => onAsk(item)}><span className={`suggestion-index suggestion-index-${index}`}>0{index + 1}</span>{item}<ArrowUpOutlined /></button>)}</div>
    <div className="welcome-demo-note"><span className="demo-footer-dot" />{apiMode === 'mock' ? '当前为本地演示数据，订单状态为模拟内容' : '远端模式读取当前账号的业务数据；无记录或服务故障会明确提示'}</div>
  </div>;
}

function MessageTimeline({ messages, loading, error, retry, feedbackUserId }: { messages: ChatMessageType[]; loading: boolean; error: unknown; retry: () => void; feedbackUserId: string }) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages]);
  if (loading) return <div className="chat-load-state"><Spin /><span>正在载入历史消息…</span></div>;
  if (error) return <div className="chat-load-state"><Alert type="error" showIcon message="消息暂时无法载入" description={error instanceof Error ? error.message : '请检查网络连接后重试'} action={<Button size="small" onClick={retry}>重试</Button>} /></div>;
  if (!messages.length) return null;
  return <div className="message-timeline">{messages.map((item) => <MessageBubble item={item} feedbackUserId={feedbackUserId} key={item.id} />)}<div ref={bottomRef} /></div>;
}

export function UserWorkspace() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { conversationId } = useParams();
  const [draft, setDraft] = useState('');
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [humanSending, setHumanSending] = useState(false);
  const humanSendingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const actorQuery = useQuery({ queryKey: ['me'], queryFn: api.me, staleTime: Infinity });
  const actor = actorQuery.data as Actor | undefined;
  const conversationQuery = useQuery({
    queryKey: ['conversations'], queryFn: api.listConversations,
    refetchInterval: apiMode === 'remote' ? 5_000 : false,
  });
  const conversations = conversationQuery.data ?? [];
  const conversation = conversations.find((item) => item.id === conversationId);
  const messagesQuery = useQuery({
    queryKey: ['messages', conversationId], queryFn: () => api.listMessages(conversationId!), enabled: Boolean(conversationId),
    refetchInterval: apiMode === 'remote' ? 3_000 : false,
  });
  const messages = messagesQuery.data ?? [];

  const createConversation = useMutation({
    mutationFn: api.createConversation,
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ['conversations'] });
      navigate(`/app/${created.id}`);
      setMobileListOpen(false);
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '无法新建会话'),
  });
  const requestHandoff = useMutation({
    mutationFn: () => api.requestHandoff(conversationId!),
    onSuccess: async (updated) => {
      queryClient.setQueryData<Conversation[]>(['conversations'], (items) => items?.map((item) => item.id === updated.id ? updated : item));
      await Promise.all([queryClient.invalidateQueries({ queryKey: ['conversations'] }), queryClient.invalidateQueries({ queryKey: ['messages', conversationId] })]);
      message.success(updated.status === 'staff' ? '人工客服已接入' : '已进入人工客服队列');
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '申请转人工失败'),
  });

  useEffect(() => {
    if (!conversationId && !conversationQuery.isLoading && conversations.length) navigate(`/app/${conversations[0].id}`, { replace: true });
    else if (conversationId && !conversationQuery.isLoading && conversations.length && !conversations.some((item) => item.id === conversationId)) navigate(`/app/${conversations[0].id}`, { replace: true });
  }, [conversationId, conversationQuery.isLoading, conversations, navigate]);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => { setStreamingText(''); }, [conversationId]);

  const selectedMessages = useMemo(() => messages, [messages]);
  const updateAssistant = (assistantId: string, update: (item: ChatMessageType) => ChatMessageType) => {
    queryClient.setQueryData<ChatMessageType[]>(['messages', conversationId], (items = []) => items.map((item) => item.id === assistantId ? update(item) : item));
  };
  const send = async (raw: string) => {
    const text = raw.trim();
    if (!text || !conversationId || !conversation || abortRef.current || humanSendingRef.current || conversation.status === 'closed') return;
    if (conversation.status === 'waiting' || conversation.status === 'staff') {
      humanSendingRef.current = true;
      setHumanSending(true);
      setDraft('');
      try {
        const sent = await api.sendHumanMessage(conversationId, text);
        queryClient.setQueryData<ChatMessageType[]>(['messages', conversationId], (items = []) =>
          items.some((item) => item.id === sent.id) ? items : [...items, sent]);
        await Promise.all([queryClient.invalidateQueries({ queryKey: ['messages', conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['conversations'] })]);
      } catch (error) {
        setDraft(text);
        message.error(error instanceof Error ? error.message : '发送留言失败');
        await queryClient.invalidateQueries({ queryKey: ['conversations'] });
      } finally {
        humanSendingRef.current = false;
        setHumanSending(false);
      }
      return;
    }
    setDraft('');
    setStreamingText('正在连接 Aiden…');
    const clientMessageId = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `client-${Date.now()}`;
    const assistantLocalId = `pending-${clientMessageId}`;
    const now = new Date().toISOString();
    const optimisticUser: ChatMessageType = { id: clientMessageId, conversationId, role: 'user', content: text, createdAt: now, status: 'complete' };
    const optimisticAssistant: ChatMessageType = { id: assistantLocalId, conversationId, role: 'assistant', content: '', createdAt: new Date(Date.now() + 1).toISOString(), status: 'streaming', toolStatuses: [] };
    queryClient.setQueryData<ChatMessageType[]>(['messages', conversationId], (items = []) => [...items, optimisticUser, optimisticAssistant]);
    const controller = new AbortController();
    abortRef.current = controller;
    let assistantId = assistantLocalId;
    try {
      await api.sendMessageStream(conversationId, text, clientMessageId, (event: StreamEvent) => {
        if (event.type === 'start') {
          if (event.messageId && event.messageId !== assistantId) {
            const previous = assistantId;
            assistantId = event.messageId;
            queryClient.setQueryData<ChatMessageType[]>(['messages', conversationId], (items = []) => items.map((item) => item.id === previous ? { ...item, id: assistantId } : item));
          }
          setStreamingText('Aiden 正在组织回答…');
        } else if (event.type === 'tool_status') {
          setStreamingText(event.status || '正在核对信息…');
          updateAssistant(assistantId, (item) => ({ ...item, toolStatuses: [...(item.toolStatuses ?? []), event.status || '正在处理'] }));
        } else if (event.type === 'order_card' && event.order) {
          updateAssistant(assistantId, (item) => ({ ...item, orderCard: event.order }));
        } else if (event.type === 'delta') {
          setStreamingText('Aiden 正在回复…');
          updateAssistant(assistantId, (item) => ({ ...item, content: item.content + (event.text ?? '') }));
        } else if (event.type === 'citations' && event.citations) {
          updateAssistant(assistantId, (item) => ({ ...item, citations: event.citations }));
        } else if (event.type === 'handoff') {
          setStreamingText('已进入人工客服队列');
          void queryClient.invalidateQueries({ queryKey: ['conversations'] });
        } else if (event.type === 'done') {
          updateAssistant(assistantId, (item) => ({ ...item, status: item.status === 'stopped' ? 'stopped' : 'complete', citations: event.citations ?? item.citations }));
          setStreamingText('');
        } else if (event.type === 'error') {
          updateAssistant(assistantId, (item) => ({ ...item, status: 'error', content: event.error || '生成失败，请重试。' }));
          setStreamingText('');
        }
      }, controller.signal);
    } catch (error) {
      if (controller.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        updateAssistant(assistantId, (item) => ({ ...item, status: 'stopped', content: item.content || '已停止生成。' }));
      } else {
        const detail = error instanceof Error ? error.message : '连接失败，请检查服务后重试';
        updateAssistant(assistantId, (item) => ({ ...item, status: 'error', content: detail }));
        setStreamingText('');
        message.error(detail);
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreamingText('');
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['messages', conversationId] }),
        queryClient.invalidateQueries({ queryKey: ['conversations'] }),
      ]);
    }
  };
  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send(draft);
    }
  };
  const isBusy = Boolean(abortRef.current);

  const listPane = <ConversationList conversations={conversations} selectedId={conversationId} onSelect={(id) => { navigate(`/app/${id}`); setMobileListOpen(false); }} onCreate={() => createConversation.mutate()} />;

  if (actorQuery.isLoading) return <div className="full-screen-state"><Spin /></div>;
  if (!actor) return <div className="full-screen-state"><Alert type="error" message="登录状态不可用" description="请重新登录后继续使用。" /></div>;
  return <div className="workspace-shell user-shell">
    <PageHeader actor={actor} />
    <main className="user-workspace">
      <aside className="user-sidebar">{conversationQuery.isLoading ? <div className="sidebar-loading"><Spin /></div> : conversationQuery.isError ? <div className="sidebar-error"><Alert type="error" showIcon message="会话列表加载失败" description={conversationQuery.error instanceof Error ? conversationQuery.error.message : ''} action={<Button size="small" onClick={() => void conversationQuery.refetch()}>重试</Button>} /></div> : listPane}</aside>
      <section className="user-chat-panel">
        <div className="chat-panel-header">
          <div className="chat-title-cluster"><Button className="mobile-tools" type="text" icon={<FileTextOutlined />} onClick={() => setMobileListOpen(true)} aria-label="打开会话列表" /><span className="chat-title-mark"><span className="mini-brand-mark">a</span></span><div><div className="chat-title">{conversation?.subject || '订单咨询'}</div><div className="chat-title-sub">{apiMode === 'mock' ? '专属智能客服 · 订单信息仅对本人可见' : '智能客服 · 订单与售后信息仅对本人可见'}</div></div></div>
          {conversation && <div className="chat-header-actions"><StatusPill status={conversation.status} />{conversation.status === 'bot' &&
            <Button className="handoff-button" icon={<CustomerServiceOutlined />} loading={requestHandoff.isPending} disabled={isBusy} onClick={() => requestHandoff.mutate()}>转人工</Button>}</div>}
        </div>
        {conversationQuery.isError && <Alert className="inline-alert" type="error" showIcon message="连接会话服务失败" description={conversationQuery.error instanceof Error ? conversationQuery.error.message : '请稍后重试'} action={<Button size="small" onClick={() => void conversationQuery.refetch()}>重试</Button>} />}
        {conversationQuery.isLoading ? <div className="chat-load-state"><Spin /><span>正在载入会话…</span></div> : !conversationId && !conversations.length ? <div className="chat-empty-state"><Empty description={<span>从一次订单咨询开始</span>}><Button type="primary" icon={<PlusOutlined />} onClick={() => createConversation.mutate()}>新建会话</Button></Empty></div> : conversationId && !conversation && !conversationQuery.isLoading ? <div className="chat-empty-state"><Empty description="找不到这个会话"><Button onClick={() => navigate('/app')}>返回会话列表</Button></Empty></div> : conversation && <>
          <div className="chat-scroll-area">
            {conversation.status === 'waiting' && <div className="handoff-banner"><span className="handoff-banner-icon"><ClockCircleOutlined /></span><div><b>已进入人工客服队列</b><span>客服专员接入后会在此处回复，你也可以继续留言。</span></div><span className="queue-pulse" /></div>}
            {conversation.status === 'staff' && <div className="staff-banner"><span className="staff-banner-icon"><CustomerServiceOutlined /></span><div><b>{conversation.assignedStaffName || '人工客服'}已接入</b><span>你可以在此继续与客服交流。</span></div></div>}
            {conversation.status === 'closed' && <div className="closed-banner"><span>本次人工服务已结束。</span><Button size="small" onClick={() => createConversation.mutate()}>新建咨询</Button></div>}
            <MessageTimeline messages={selectedMessages} loading={messagesQuery.isLoading} error={messagesQuery.error} retry={() => void messagesQuery.refetch()} feedbackUserId={actor.id} />
            {messages.length === 0 && !messagesQuery.isLoading && <WelcomePanel onAsk={(text) => void send(text)} />}
            {isBusy && <div className="streaming-indicator"><span className="streaming-bars"><i /><i /><i /></span>{streamingText || '正在生成回复…'}{apiMode === 'mock' && <span className="streaming-mode">本地模拟</span>}</div>}
          </div>
          <div className="composer-wrap">
            {conversation.status === 'closed' ? <div className="composer-closed">此会话已结束。<Button type="link" onClick={() => createConversation.mutate()}>开始新的咨询</Button></div> : <div className="composer-box">
              <Input.TextArea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onInputKeyDown} autoSize={{ minRows: 1, maxRows: 5 }} placeholder={conversation.status === 'bot' ? '输入消息开始对话…' : '在原会话继续留言…'} disabled={isBusy || humanSending} aria-label="输入消息" />
              <div className="composer-toolbar"><span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>{isBusy ? <Button className="stop-button" type="primary" danger icon={<StopOutlined />} onClick={() => abortRef.current?.abort()}>停止生成</Button> : <Button className="send-button" type="primary" icon={<ArrowUpOutlined />} loading={humanSending} disabled={!draft.trim()} onClick={() => void send(draft)}>发送</Button>}</div>
            </div>}
            <div className="composer-disclaimer"><span className="composer-lock">◆</span>{apiMode === 'mock' ? '演示模式使用本地模拟数据，请勿输入真实个人信息' : '回答以查询结果和知识证据为准；售后审核通过不代表退款到账，不保证人工立即接入'}</div>
          </div>
        </>}
      </section>
    </main>
    <Drawer title="我的会话" placement="left" width={320} open={mobileListOpen} onClose={() => setMobileListOpen(false)} styles={{ body: { padding: 0 } }}>{listPane}</Drawer>
  </div>;
}
