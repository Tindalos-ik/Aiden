import type { Actor, Conversation, LoginInput, Message, StreamEvent, ServiceOrder, AfterSalePreview, AfterSaleApplication, ServiceTicket, SubmitAfterSaleInput } from '../types';

export interface AidenApi {
  login(input: LoginInput): Promise<Actor>;
  me(): Promise<Actor | null>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  createConversation(): Promise<Conversation>;
  /** 历史 assistant 消息可携带 citations；mock 模式不生成虚构来源。 */
  listMessages(conversationId: string): Promise<Message[]>;
  submitFeedback(conversationId: string, messageId: string, rating: 'satisfied' | 'unsatisfied'): Promise<{ rating: 'satisfied' | 'unsatisfied' }>;
  listStaffMessages(conversationId: string): Promise<Message[]>;
  sendHumanMessage(conversationId: string, text: string): Promise<Message>;
  /** remote 进度仅用于瞬态界面；delta 是服务端完成校验并落库后的整轮回答，引用编号与正文中的 [n] 对应。 */
  sendMessageStream(
    conversationId: string,
    text: string,
    clientMessageId: string,
    onEvent: (event: StreamEvent) => void,
    signal: AbortSignal,
  ): Promise<void>;
  requestHandoff(conversationId: string): Promise<Conversation>;
  listQueue(): Promise<Conversation[]>;
  acceptConversation(conversationId: string): Promise<Conversation>;
  sendStaffMessage(conversationId: string, text: string): Promise<Message>;
  closeConversation(conversationId: string): Promise<Conversation>;
  listServiceOrders(): Promise<ServiceOrder[]>;
  previewAfterSale(orderNo: string, kind: SubmitAfterSaleInput['requestType']): Promise<AfterSalePreview>;
  submitAfterSale(input: SubmitAfterSaleInput): Promise<AfterSaleApplication>;
  listAfterSales(): Promise<AfterSaleApplication[]>;
  cancelAfterSale(id: string): Promise<AfterSaleApplication>;
  listTickets(): Promise<ServiceTicket[]>;
  listStaffAfterSales(): Promise<AfterSaleApplication[]>;
  transitionAfterSale(id: string, status: string, note: string): Promise<AfterSaleApplication>;
  listStaffTickets(): Promise<ServiceTicket[]>;
  transitionTicket(id: string, status: string, note: string, afterSaleRequestId?: string): Promise<ServiceTicket>;
  resetDemoData(): Promise<void>;
}
