import type { AidenApi } from './contracts';
import type { Actor, Conversation, Message, OrderCard, StreamEvent, AfterSaleApplication, ServiceTicket } from '../types';
import { demoAccounts, demoOrdersByUser } from '../data/demoData';

const DB_KEY = 'aiden-demo-db-v1';
const ACTOR_KEY = 'aiden-demo-actor-v1';
const CHANNEL_NAME = 'aiden-demo-sync-v1';

interface DemoDatabase {
  version: number;
  conversations: Conversation[];
  messages: Message[];
  ordersByUser: Record<string, OrderCard[]>;
  afterSales?: AfterSaleApplication[];
  tickets?: ServiceTicket[];
}

const makeId = (prefix: string) => `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

function seedDatabase(): DemoDatabase {
  const conversations: Conversation[] = [];
  const messages: Message[] = [];
  for (const userId of ['user-maya', 'user-chen']) {
    const historyId = `${userId}-history`;
    const currentId = `${userId}-current`;
    const userOrders = demoOrdersByUser[userId];
    const old = userOrders[userId === 'user-maya' ? 1 : 0];
    conversations.push(
      { id: historyId, userId, subject: '订单状态咨询', status: 'bot', createdAt: ago(1380), updatedAt: ago(1350), lastMessagePreview: `已为你查询到订单 ${old.orderId}` },
      { id: currentId, userId, subject: '新的订单咨询', status: 'bot', createdAt: ago(9), updatedAt: ago(9) },
    );
    messages.push(
      { id: `${historyId}-u`, conversationId: historyId, role: 'user', content: '你好，帮我看看最近的订单到哪了？', createdAt: ago(1360), status: 'complete' },
      { id: `${historyId}-a`, conversationId: historyId, role: 'assistant', content: `查到了，订单 ${old.orderId} 当前状态是${old.status}。${old.logistics}。`, createdAt: ago(1359), status: 'complete', orderCard: old },
    );
  }
  return {
    version: 1,
    conversations,
    messages,
    ordersByUser: demoOrdersByUser,
    afterSales: [],
    tickets: [{
      id: 'demo-ticket-maya', ticketNo: 'TK-DEMO-001', userId: 'user-maya',
      conversationId: 'user-maya-history', issueType: 'complaint',
      description: '演示工单：配送延迟，需要客服核实。', status: 'open',
      assignedStaffId: null, afterSaleRequestId: null, acceptedAt: null,
      resolvedAt: null, closedAt: null, closedById: null, resolutionNote: null,
      createdAt: ago(120), updatedAt: ago(120),
    }],
  };
}

function database(): DemoDatabase {
  const raw = localStorage.getItem(DB_KEY);
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as DemoDatabase;
      if (parsed.version === 1) return parsed;
    } catch {
      // A damaged local demo store is replaced with the deterministic seed below.
    }
  }
  const seeded = seedDatabase();
  localStorage.setItem(DB_KEY, JSON.stringify(seeded));
  return seeded;
}

function writeDatabase(db: DemoDatabase): void {
  localStorage.setItem(DB_KEY, JSON.stringify(db));
  if ('BroadcastChannel' in window) {
    const channel = new BroadcastChannel(CHANNEL_NAME);
    channel.postMessage({ type: 'changed', at: Date.now() });
    channel.close();
  }
}

function currentActor(): Actor {
  const value = sessionStorage.getItem(ACTOR_KEY);
  if (!value) throw new Error('演示登录已失效，请重新登录');
  const actor = JSON.parse(value) as Actor;
  return actor;
}

function requireUser(): Actor {
  const actor = currentActor();
  if (actor.role !== 'user') throw new Error('当前账号没有用户会话权限');
  return actor;
}

function requireStaff(): Actor {
  const actor = currentActor();
  if (actor.role !== 'staff') throw new Error('当前账号没有客服工作台权限');
  return actor;
}

function storeConversation(db: DemoDatabase, conversation: Conversation): void {
  const i = db.conversations.findIndex((item) => item.id === conversation.id);
  if (i < 0) db.conversations.push(conversation);
  else db.conversations[i] = conversation;
}

function updateMessage(db: DemoDatabase, message: Message): void {
  const i = db.messages.findIndex((item) => item.id === message.id);
  if (i < 0) db.messages.push(message);
  else db.messages[i] = message;
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException('Aborted', 'AbortError'));
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

function chooseOrder(text: string, orders: OrderCard[]): OrderCard {
  const matched = orders.find((order) => text.includes(order.orderId));
  if (matched) return matched;
  if (/退款|退钱|退货/.test(text)) return orders.find((order) => order.status === '退款中') ?? orders[0];
  if (/签收|送到|收到/.test(text)) return orders.find((order) => order.status === '已签收') ?? orders[0];
  return orders[0];
}

function answerFor(text: string, order: OrderCard): { answer: string; handoff: boolean; query: boolean } {
  if (/转人工|找真人|找人工|联系人工客服|接入人工/.test(text)) {
    return { answer: '已进入人工客服队列，客服接单后会在本会话回复。你可以继续补充问题。', handoff: true, query: false };
  }
  if (/退款|退钱|退货/.test(text)) {
    const urgent = /一直|超过|没到|争议|投诉|不同意/.test(text);
    return {
      answer: urgent
        ? `我查到订单 ${order.orderId} 的退款当前显示为“${order.status}”。退款由商家处理，如果你对处理进度有争议，我可以为你转接人工专员继续核实。`
        : `我查到订单 ${order.orderId} 当前为“${order.status}”，${order.logistics}。退款到账时间会受支付渠道影响；如果状态长时间没有变化，可以申请人工专员帮你跟进。`,
      handoff: false,
      query: true,
    };
  }
  if (/到哪|物流|发货|订单|快递|签收/.test(text)) {
    return { answer: `你的订单 ${order.orderId} 当前状态是“${order.status}”。${order.logistics}。我会继续留意后续状态，有其他订单也可以把订单号发给我。`, handoff: false, query: true };
  }
  return { answer: '我可以帮你查询物流、订单状态或退款进度。告诉我你遇到的问题，或者点下方的“转人工”由客服专员协助。', handoff: false, query: false };
}

function sortedMessages(conversationId: string, db = database()): Message[] {
  return db.messages.filter((message) => message.conversationId === conversationId).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}

export const mockApi: AidenApi = {
  async login(input) {
    const account = demoAccounts.find((item) => (item.id === input.account || item.email === input.account) && item.password === input.password && (!input.role || item.role === input.role));
    if (!account) throw new Error('账号或密码不正确。可使用页面提供的演示账号快捷登录。');
    const actor: Actor = { id: account.id, name: account.name, role: account.role, email: account.email, avatarColor: account.color };
    sessionStorage.setItem(ACTOR_KEY, JSON.stringify(actor));
    database();
    return actor;
  },
  async me() {
    const value = sessionStorage.getItem(ACTOR_KEY);
    if (!value) return null;
    try { return JSON.parse(value) as Actor; } catch { return null; }
  },
  async logout() {
    sessionStorage.removeItem(ACTOR_KEY);
  },
  async listConversations() {
    const actor = requireUser();
    return database().conversations.filter((item) => item.userId === actor.id).sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  },
  async createConversation() {
    const actor = requireUser();
    const db = database();
    const now = new Date().toISOString();
    const conversation: Conversation = { id: makeId('conversation'), userId: actor.id, subject: '新的订单咨询', status: 'bot', createdAt: now, updatedAt: now };
    storeConversation(db, conversation);
    writeDatabase(db);
    return conversation;
  },
  async listMessages(conversationId) {
    const actor = currentActor();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId);
    if (!conversation || (actor.role === 'user' && conversation.userId !== actor.id)) throw new Error('无权查看此会话');
    return sortedMessages(conversationId, db);
  },
  async listStaffMessages(conversationId) {
    return this.listMessages(conversationId);
  },
  async sendHumanMessage(conversationId, text) {
    const actor = requireUser();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId && item.userId === actor.id);
    if (!conversation) throw new Error('找不到当前会话');
    if (conversation.status !== 'waiting' && conversation.status !== 'staff') throw new Error('此会话当前不能发送消息');
    const now = new Date().toISOString();
    const sent: Message = { id: makeId('user-message'), conversationId, role: 'user', content: text, createdAt: now, status: 'complete' };
    updateMessage(db, sent);
    conversation.updatedAt = now;
    conversation.lastMessagePreview = text;
    storeConversation(db, conversation);
    writeDatabase(db);
    return sent;
  },
  async sendMessageStream(conversationId, text, clientMessageId, onEvent, signal) {
    const actor = requireUser();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId && item.userId === actor.id);
    if (!conversation) throw new Error('找不到当前会话');
    if (conversation.status === 'closed') throw new Error('此会话已结束，请新建会话继续咨询');
    if (conversation.status === 'waiting' || conversation.status === 'staff') throw new Error('人工客服已接入，请在此会话中直接留言');

    const userMessage: Message = { id: clientMessageId, conversationId, role: 'user', content: text, createdAt: new Date().toISOString(), status: 'complete' };
    const assistantId = makeId('assistant');
    // 演示回答来自本地模拟订单，不附会知识库 chunk 或原文链接。
    const assistant: Message = { id: assistantId, conversationId, role: 'assistant', content: '', createdAt: new Date().toISOString(), status: 'streaming', toolStatuses: [] };
    updateMessage(db, userMessage);
    updateMessage(db, assistant);
    if (conversation.subject === '新的订单咨询') conversation.subject = text.slice(0, 22);
    conversation.updatedAt = new Date().toISOString();
    conversation.lastMessagePreview = text;
    storeConversation(db, conversation);
    writeDatabase(db);
    onEvent({ type: 'start', messageId: assistantId });

    const orders = db.ordersByUser[actor.id] ?? [];
    const order = orders.length ? chooseOrder(text, orders) : undefined;
    const result = answerFor(text, order ?? { orderId: '', product: '', amount: 0, status: '物流中', logistics: '' });
    const persist = () => {
      const latest = database();
      updateMessage(latest, assistant);
      const current = latest.conversations.find((item) => item.id === conversationId);
      if (current) {
        current.updatedAt = new Date().toISOString();
        current.lastMessagePreview = assistant.content.slice(-70) || text;
        storeConversation(latest, current);
      }
      writeDatabase(latest);
    };
    try {
      if (/模拟错误|测试错误/.test(text)) throw new Error('演示连接中断，请重试或转接人工客服。');
      if (result.query) {
        const status = '正在查询订单…';
        assistant.toolStatuses = [...(assistant.toolStatuses ?? []), status];
        onEvent({ type: 'tool_status', status });
        persist();
        await delay(600, signal);
        const resolved = '已核对订单与物流信息';
        assistant.toolStatuses = [...(assistant.toolStatuses ?? []), resolved];
        onEvent({ type: 'tool_status', status: resolved });
        onEvent({ type: 'order_card', order });
        assistant.orderCard = order;
      }
      const parts = result.answer.match(/.{1,5}/gu) ?? [result.answer];
      for (const part of parts) {
        await delay(34, signal);
        assistant.content += part;
        onEvent({ type: 'delta', text: part });
        persist();
      }
      if (result.handoff) {
        assistant.content = result.answer;
        const latest = database();
        const current = latest.conversations.find((item) => item.id === conversationId);
        if (current) { current.status = 'waiting'; current.updatedAt = new Date().toISOString(); storeConversation(latest, current); }
        updateMessage(latest, assistant);
        writeDatabase(latest);
        onEvent({ type: 'handoff' });
      }
      assistant.status = 'complete';
      persist();
      onEvent({ type: 'done' });
    } catch (error) {
      if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
        assistant.status = 'stopped';
        if (!assistant.content) assistant.content = '已停止生成。';
        persist();
        onEvent({ type: 'done' });
        return;
      }
      assistant.status = 'error';
      assistant.content = error instanceof Error ? error.message : '生成失败，请重试';
      persist();
      onEvent({ type: 'error', error: assistant.content });
    }
  },
  async requestHandoff(conversationId) {
    const actor = requireUser();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId && item.userId === actor.id);
    if (!conversation) throw new Error('找不到当前会话');
    if (conversation.status === 'closed') throw new Error('会话已结束，请新建会话');
    if (conversation.status === 'staff' || conversation.status === 'waiting') return conversation;
    conversation.status = 'waiting';
    conversation.updatedAt = new Date().toISOString();
    conversation.lastMessagePreview = '用户申请转人工';
    const systemMessage: Message = { id: makeId('message'), conversationId, role: 'system', content: '已进入人工客服队列，请稍候。', createdAt: new Date().toISOString(), status: 'complete' };
    updateMessage(db, systemMessage);
    storeConversation(db, conversation);
    writeDatabase(db);
    return conversation;
  },
  async listQueue() {
    requireStaff();
    return database().conversations.filter((item) => item.status === 'waiting' || item.status === 'staff' || item.status === 'closed').map((item) => ({ ...item, userName: demoAccounts.find((account) => account.id === item.userId)?.name })).sort((a, b) => (a.status === b.status ? a.updatedAt.localeCompare(b.updatedAt) : a.status === 'waiting' ? -1 : 1));
  },
  async acceptConversation(conversationId) {
    const staff = requireStaff();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId);
    if (!conversation) throw new Error('会话不存在');
    if (conversation.status === 'closed') throw new Error('此会话已结束');
    conversation.status = 'staff';
    conversation.assignedStaffId = staff.id;
    conversation.assignedStaffName = staff.name;
    conversation.updatedAt = new Date().toISOString();
    updateMessage(db, { id: makeId('message'), conversationId, role: 'system', content: `${staff.name}已接入会话`, createdAt: new Date().toISOString(), status: 'complete' });
    storeConversation(db, conversation);
    writeDatabase(db);
    return conversation;
  },
  async sendStaffMessage(conversationId, text) {
    const staff = requireStaff();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId);
    if (!conversation || conversation.status !== 'staff') throw new Error('请先接入一个等待中的会话');
    if (conversation.assignedStaffId && conversation.assignedStaffId !== staff.id) throw new Error('该会话已由其他客服接入');
    const message: Message = { id: makeId('staff-message'), conversationId, role: 'staff', content: text, createdAt: new Date().toISOString(), status: 'complete' };
    updateMessage(db, message);
    conversation.updatedAt = message.createdAt;
    conversation.lastMessagePreview = text;
    storeConversation(db, conversation);
    writeDatabase(db);
    return message;
  },
  async closeConversation(conversationId) {
    requireStaff();
    const db = database();
    const conversation = db.conversations.find((item) => item.id === conversationId);
    if (!conversation) throw new Error('会话不存在');
    conversation.status = 'closed';
    conversation.updatedAt = new Date().toISOString();
    updateMessage(db, { id: makeId('message'), conversationId, role: 'system', content: '本次人工服务已结束，感谢你的耐心等待。', createdAt: new Date().toISOString(), status: 'complete' });
    storeConversation(db, conversation);
    writeDatabase(db);
    return conversation;
  },
  async listServiceOrders() {
    const actor = requireUser();
    return (database().ordersByUser[actor.id] ?? []).map((order) => ({
      orderNo: order.orderId, status: order.status,
      items: [{ id: `${order.orderId}-item`, name: order.product, quantity: 1 }],
    }));
  },
  async previewAfterSale(orderNo, kind) {
    const actor = requireUser();
    const order = (database().ordersByUser[actor.id] ?? []).find((item) => item.orderId === orderNo);
    if (!order) throw new Error('订单不存在');
    const name = { refund: '退款', return: '退货', exchange: '换货' }[kind];
    return {
      orderId: orderNo, orderNo, orderStatus: order.status, status: order.status,
      items: [{ id: `${orderNo}-item`, name: order.product, quantity: 1, lineTotal: String(order.amount) }],
      policies: [{ id: `mock-${kind}`, name: `${name}演示政策`, version: 'demo', content: '仅供本地流程演示；具体条件以真实商城政策为准。' }],
    };
  },
  async submitAfterSale(input) {
    const actor = requireUser();
    if (!input.confirmed || !input.reason.trim()) throw new Error('请填写原因并确认提交');
    const preview = await this.previewAfterSale(input.orderNo, input.requestType);
    if (!preview.items.some((item) => item.id === input.orderItemId) || !preview.policies.some((item) => item.id === input.policyReference)) throw new Error('订单商品或政策无效');
    const db = database();
    const rows = db.afterSales ?? [];
    const linkTicket = (requestId: string) => {
      if (!input.sourceTicketNo) return;
      const ticket = (db.tickets ?? []).find((item) => item.ticketNo === input.sourceTicketNo && item.userId === actor.id);
      if (!ticket || (ticket.afterSaleRequestId && ticket.afterSaleRequestId !== requestId)) throw new Error('关联工单不存在或已关联其他申请');
      ticket.afterSaleRequestId = requestId;
      writeDatabase(db);
    };
    const sameKey = rows.find((item) => item.userId === actor.id && item.submissionKey === input.submissionKey);
    if (sameKey) {
      if (sameKey.orderNo !== input.orderNo || sameKey.orderItemId !== input.orderItemId || sameKey.requestType !== input.requestType || sameKey.reason !== input.reason.trim()) throw new Error('幂等键已用于另一项申请');
      linkTicket(sameKey.id);
      return { ...sameKey, alreadyExists: true };
    }
    const duplicate = rows.find((item) => item.userId === actor.id && item.orderNo === input.orderNo && item.orderItemId === input.orderItemId && !['cancelled', 'rejected', 'completed'].includes(item.status));
    if (duplicate) {
      if (duplicate.requestType !== input.requestType) throw new Error('该商品已有其他类型的未结束售后申请');
      linkTicket(duplicate.id); return { ...duplicate, alreadyExists: true };
    }
    const now = new Date().toISOString();
    const row: AfterSaleApplication = { id: makeId('as'), requestNo: makeId('AS'), userId: actor.id,
      orderId: input.orderNo, orderNo: input.orderNo, orderItemId: input.orderItemId,
      requestType: input.requestType, reason: input.reason.trim(), status: 'pending',
      requestedAmount: null, policyReference: input.policyReference, submissionKey: input.submissionKey, reviewedById: null,
      reviewedAt: null, processingAt: null, processingById: null, resolvedById: null,
      resolvedAt: null, decisionNote: null, createdAt: now, updatedAt: now };
    db.afterSales = [...rows, row];
    linkTicket(row.id);
    writeDatabase(db);
    return row;
  },
  async listAfterSales() { const actor = requireUser(); return (database().afterSales ?? []).filter((item) => item.userId === actor.id); },
  async cancelAfterSale(id) {
    const actor = requireUser(); const db = database();
    const row = (db.afterSales ?? []).find((item) => item.id === id && item.userId === actor.id);
    if (!row) throw new Error('申请不存在');
    if (!['pending', 'approved'].includes(row.status)) throw new Error('非法的申请状态转换');
    row.status = 'cancelled'; row.resolvedAt = row.updatedAt = new Date().toISOString(); row.resolvedById = actor.id; writeDatabase(db); return row;
  },
  async listTickets() { const actor = requireUser(); return (database().tickets ?? []).filter((item) => item.userId === actor.id); },
  async listStaffAfterSales() { requireStaff(); return database().afterSales ?? []; },
  async transitionAfterSale(id, status, note) {
    const actor = requireStaff(); const db = database();
    const row = (db.afterSales ?? []).find((item) => item.id === id);
    if (!row) throw new Error('申请不存在');
    const next: Record<string, string[]> = { pending: ['approved', 'rejected'], approved: [row.requestType === 'refund' ? 'awaiting_external_refund' : 'processing'], processing: ['completed'] };
    if (!next[row.status]?.includes(status)) throw new Error('非法的申请状态转换');
    if (['rejected', 'processing', 'completed'].includes(status) && !note.trim()) throw new Error('请填写处理说明');
    const now = new Date().toISOString(); row.status = status as AfterSaleApplication['status']; row.updatedAt = now; row.decisionNote = note.trim() || row.decisionNote;
    if (['approved', 'rejected'].includes(status)) { row.reviewedAt = now; row.reviewedById = actor.id; }
    if (['processing', 'awaiting_external_refund'].includes(status)) { row.processingAt = now; row.processingById = actor.id; }
    if (['rejected', 'completed'].includes(status)) { row.resolvedAt = now; row.resolvedById = actor.id; }
    writeDatabase(db); return row;
  },
  async listStaffTickets() { requireStaff(); return database().tickets ?? []; },
  async transitionTicket(id, status, note, afterSaleRequestId) {
    const actor = requireStaff(); const db = database();
    const row = (db.tickets ?? []).find((item) => item.id === id);
    if (!row) throw new Error('工单不存在');
    const next: Record<string, string[]> = { open: ['in_progress'], in_progress: ['resolved', 'closed'], resolved: ['closed'] };
    if (!next[row.status]?.includes(status)) throw new Error('非法的工单状态转换');
    if (row.assignedStaffId && row.assignedStaffId !== actor.id) throw new Error('仅接手员工可处理工单');
    if (afterSaleRequestId && !(db.afterSales ?? []).some((item) => item.id === afterSaleRequestId && item.userId === row.userId)) throw new Error('关联申请不存在');
    const now = new Date().toISOString(); row.status = status as ServiceTicket['status']; row.updatedAt = now;
    if (status === 'in_progress') { row.assignedStaffId = actor.id; row.acceptedAt = now; }
    if (note.trim()) row.resolutionNote = note.trim();
    if (status === 'resolved') row.resolvedAt = now;
    if (status === 'closed') { row.closedAt = now; row.closedById = actor.id; }
    if (afterSaleRequestId) row.afterSaleRequestId = afterSaleRequestId;
    writeDatabase(db); return row;
  },
  async resetDemoData() {
    localStorage.removeItem(DB_KEY);
    localStorage.setItem(DB_KEY, JSON.stringify(seedDatabase()));
    if ('BroadcastChannel' in window) {
      const channel = new BroadcastChannel(CHANNEL_NAME);
      channel.postMessage({ type: 'changed', at: Date.now() });
      channel.close();
    }
  },
};

export const mockStorageKey = DB_KEY;
export const mockChannelName = CHANNEL_NAME;
export type { StreamEvent };
