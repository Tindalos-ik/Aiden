import type { AidenApi } from './contracts';
import type { Actor, Citation, Conversation, Message, OrderCard, StreamEvent, ServiceOrder, AfterSalePreview, AfterSaleApplication, ServiceTicket } from '../types';

const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    credentials: 'include',
    headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  });
  if (!response.ok) {
    const body = await response.text().catch(() => '');
    let message = body;
    try {
      const parsed = JSON.parse(body) as { detail?: string; message?: string };
      message = parsed.detail || parsed.message || body;
    } catch { /* Keep the server's plain text error. */ }
    throw new ApiError(message || `请求失败（${response.status}）`, response.status);
  }
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

function listOf<T>(value: T[] | { items?: T[]; conversations?: T[]; messages?: T[] }): T[] {
  if (Array.isArray(value)) return value;
  return value.items ?? value.conversations ?? value.messages ?? [];
}

interface RawSseEvent { event: string; data: string; id?: string; }

/** Incremental SSE parser: it retains partial lines and handles CRLF, multiline data and EOF. */
export async function readSseStream(
  stream: ReadableStream<Uint8Array>,
  onEvent: (event: RawSseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let eventName = '';
  let eventId: string | undefined;
  let dataLines: string[] = [];
  let atStreamStart = true;

  const dispatch = () => {
    if (dataLines.length) onEvent({ event: eventName || 'message', data: dataLines.join('\n'), id: eventId });
    eventName = '';
    eventId = undefined;
    dataLines = [];
  };
  const consumeLine = (rawLine: string) => {
    const line = rawLine;
    if (!line) { dispatch(); return; }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') eventName = value;
    else if (field === 'data') dataLines.push(value);
    else if (field === 'id' && !value.includes('\0')) eventId = value;
  };
  const consumeAvailableLines = (final: boolean) => {
    while (true) {
      const lf = buffer.indexOf('\n');
      const cr = buffer.indexOf('\r');
      let end: number;
      if (lf < 0) end = cr;
      else if (cr < 0) end = lf;
      else end = Math.min(lf, cr);
      if (end < 0 || (!final && buffer[end] === '\r' && end === buffer.length - 1)) return;
      const delimiterSize = buffer[end] === '\r' && buffer[end + 1] === '\n' ? 2 : 1;
      consumeLine(buffer.slice(0, end));
      buffer = buffer.slice(end + delimiterSize);
    }
  };

  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (atStreamStart && buffer.length) {
        if (buffer.charCodeAt(0) === 0xfeff) buffer = buffer.slice(1);
        atStreamStart = false;
      }
      consumeAvailableLines(false);
    }
    buffer += decoder.decode();
    consumeAvailableLines(true);
    if (buffer.length) consumeLine(buffer);
    dispatch();
  } finally {
    if (signal?.aborted) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function citationsFrom(value: unknown): Citation[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is Citation => typeof item === 'object' && item !== null
    && Number.isInteger(item.number) && item.number > 0
    && typeof item.chunkId === 'string' && typeof item.content === 'string'
    && typeof item.sectionPath === 'string');
}

function normalizedEvent(raw: RawSseEvent): StreamEvent | null {
  const known = new Set<StreamEvent['type']>(['start', 'tool_status', 'order_card', 'delta', 'citations', 'handoff', 'done', 'error']);
  let payload: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(raw.data);
    if (typeof parsed === 'object' && parsed !== null) payload = parsed as Record<string, unknown>;
    else if (typeof parsed === 'string') payload = { text: parsed };
  } catch {
    payload = { text: raw.data };
  }
  const candidate = typeof payload.type === 'string' ? payload.type : raw.event;
  if (!known.has(candidate as StreamEvent['type'])) return null;
  const type = candidate as StreamEvent['type'];
  const order = (payload.order ?? payload.orderCard ?? payload.card) as OrderCard | undefined;
  const conversation = (payload.conversation ?? payload.data) as Conversation | undefined;
  return {
    type,
    messageId: String(payload.messageId ?? payload.assistantMessageId ?? '') || undefined,
    text: typeof payload.text === 'string' ? payload.text : typeof payload.delta === 'string' ? payload.delta : undefined,
    status: typeof payload.status === 'string' ? payload.status : typeof payload.message === 'string' && type === 'tool_status' ? payload.message : undefined,
    order,
    error: typeof payload.error === 'string' ? payload.error : typeof payload.message === 'string' && type === 'error' ? payload.message : undefined,
    conversation,
    citations: citationsFrom(payload.citations),
  };
}

export const remoteApi: AidenApi = {
  async login(input) {
    return request<Actor>('/auth/login', { method: 'POST', body: JSON.stringify(input) });
  },
  async me() {
    try { return await request<Actor>('/auth/me'); }
    catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  },
  async logout() {
    await request<void>('/auth/logout', { method: 'POST' });
  },
  async listConversations() {
    return listOf(await request<Conversation[] | { items?: Conversation[]; conversations?: Conversation[] }>('/conversations'));
  },
  async createConversation() {
    return request<Conversation>('/conversations', { method: 'POST', body: JSON.stringify({}) });
  },
  async listMessages(conversationId) {
    const path = `/conversations/${encodeURIComponent(conversationId)}/messages`;
    return listOf(await request<Message[] | { items?: Message[]; messages?: Message[] }>(path));
  },
  async listStaffMessages(conversationId) {
    return listOf(await request<Message[]>(`/staff/conversations/${encodeURIComponent(conversationId)}/messages`));
  },
  async sendHumanMessage(conversationId, text) {
    return request<Message>(`/conversations/${encodeURIComponent(conversationId)}/messages`, { method: 'POST', body: JSON.stringify({ text }) });
  },
  async sendMessageStream(conversationId, text, clientMessageId, onEvent, signal) {
    const response = await fetch(`${baseUrl}/conversations/${encodeURIComponent(conversationId)}/messages/stream`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ text, clientMessageId }),
      signal,
    });
    if (!response.ok) {
      const body = await response.text().catch(() => '');
      let message = body;
      try { message = (JSON.parse(body) as { detail?: string; message?: string }).detail || (JSON.parse(body) as { message?: string }).message || body; } catch { /* plain response body */ }
      throw new ApiError(message || `流式请求失败（${response.status}）`, response.status);
    }
    if (!response.body) throw new Error('服务端没有返回可读取的流');
    await readSseStream(response.body, (raw) => {
      const event = normalizedEvent(raw);
      if (event) onEvent(event);
    }, signal);
  },
  async requestHandoff(conversationId) {
    return request<Conversation>(`/conversations/${encodeURIComponent(conversationId)}/handoff`, { method: 'POST', body: JSON.stringify({}) });
  },
  async listQueue() {
    return listOf(await request<Conversation[] | { items?: Conversation[]; conversations?: Conversation[] }>('/staff/queue'));
  },
  async acceptConversation(conversationId) {
    return request<Conversation>(`/staff/conversations/${encodeURIComponent(conversationId)}/accept`, { method: 'POST', body: JSON.stringify({}) });
  },
  async sendStaffMessage(conversationId, text) {
    return request<Message>(`/staff/conversations/${encodeURIComponent(conversationId)}/messages`, { method: 'POST', body: JSON.stringify({ text }) });
  },
  async closeConversation(conversationId) {
    return request<Conversation>(`/staff/conversations/${encodeURIComponent(conversationId)}/close`, { method: 'POST', body: JSON.stringify({}) });
  },
  async listServiceOrders() { return request<ServiceOrder[]>('/service/orders'); },
  async previewAfterSale(orderNo, kind) {
    return request<AfterSalePreview>(`/service/orders/${encodeURIComponent(orderNo)}/after-sale-preview?requestType=${kind}`);
  },
  async submitAfterSale(input) {
    return request<AfterSaleApplication>('/service/after-sales', { method: 'POST', body: JSON.stringify(input) });
  },
  async listAfterSales() { return request<AfterSaleApplication[]>('/service/after-sales'); },
  async cancelAfterSale(id) {
    return request<AfterSaleApplication>(`/service/after-sales/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  },
  async listTickets() { return request<ServiceTicket[]>('/service/tickets'); },
  async listStaffAfterSales() { return request<AfterSaleApplication[]>('/staff/service/after-sales'); },
  async transitionAfterSale(id, status, note) {
    return request<AfterSaleApplication>(`/staff/service/after-sales/${encodeURIComponent(id)}/transition`, { method: 'POST', body: JSON.stringify({ status, note }) });
  },
  async listStaffTickets() { return request<ServiceTicket[]>('/staff/service/tickets'); },
  async transitionTicket(id, status, note, afterSaleRequestId) {
    return request<ServiceTicket>(`/staff/service/tickets/${encodeURIComponent(id)}/transition`, { method: 'POST', body: JSON.stringify({ status, note, afterSaleRequestId }) });
  },
  async resetDemoData() {
    throw new Error('演示数据仅存在于 mock 模式，当前远端模式不会修改服务端数据。');
  },
};
