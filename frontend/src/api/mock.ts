import type { AidenApi } from './contracts';
import type { Actor, Conversation, Message, OrderCard, StreamEvent } from '../types';
import { demoAccounts, demoOrdersByUser } from '../data/demoData';

const DB_KEY = 'aiden-demo-db-v1';
const ACTOR_KEY = 'aiden-demo-actor-v1';
const CHANNEL_NAME = 'aiden-demo-sync-v1';

interface DemoDatabase {
  version: number;
  conversations: Conversation[];
  messages: Message[];
  ordersByUser: Record<string, OrderCard[]>;
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
  if (/投诉|转人工|人工客服|联系人工/.test(text)) {
    return { answer: '我来为你联系人工客服，稍后会有专员接入当前会话。你可以继续在这里补充订单情况。', handoff: true, query: false };
  }
  if (/退款|退钱|退货/.test(text)) {
    const urgent = /一直|超过|没到|争议|投诉|不同意/.test(text);
    return {
      answer: urgent
        ? `我查到订单 ${order.orderId} 的退款当前显示为“${order.status}”。退款由商家处理，如果你对处理进度有争议，我可以为你转接人工专员继续核实。`
        : `我查到订单 ${order.orderId} 当前为“${order.status}”，${order.logistics}。退款到账时间会受支付渠道影响；如果状态长时间没有变化，可以申请人工专员帮你跟进。`,
      handoff: urgent,
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
        if (result.handoff) current.status = 'waiting';
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
    const systemMessage: Message = { id: makeId('message'), conversationId, role: 'system', content: '已为你接入人工客服，请稍候。', createdAt: new Date().toISOString(), status: 'complete' };
    updateMessage(db, systemMessage);
    storeConversation(db, conversation);
    writeDatabase(db);
    return conversation;
  },
  async listQueue() {
    requireStaff();
    return database().conversations.filter((item) => item.status === 'waiting' || item.status === 'staff').map((item) => ({ ...item, userName: demoAccounts.find((account) => account.id === item.userId)?.name })).sort((a, b) => (a.status === b.status ? a.updatedAt.localeCompare(b.updatedAt) : a.status === 'waiting' ? -1 : 1));
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
