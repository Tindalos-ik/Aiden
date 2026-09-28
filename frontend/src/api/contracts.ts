import type { Actor, Conversation, LoginInput, Message, StreamEvent } from '../types';

export interface AidenApi {
  login(input: LoginInput): Promise<Actor>;
  me(): Promise<Actor | null>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  createConversation(): Promise<Conversation>;
  /** 历史 assistant 消息可携带 citations；mock 模式不生成虚构来源。 */
  listMessages(conversationId: string): Promise<Message[]>;
  listStaffMessages(conversationId: string): Promise<Message[]>;
  sendHumanMessage(conversationId: string, text: string): Promise<Message>;
  /** citations SSE 事件的 payload 为 { citations: Citation[] }，编号与回答中的 [n] 一致。 */
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
  resetDemoData(): Promise<void>;
}
