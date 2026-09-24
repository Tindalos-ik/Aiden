import type { Actor, Conversation, LoginInput, Message, StreamEvent } from '../types';

export interface AidenApi {
  login(input: LoginInput): Promise<Actor>;
  me(): Promise<Actor | null>;
  logout(): Promise<void>;
  listConversations(): Promise<Conversation[]>;
  createConversation(): Promise<Conversation>;
  listMessages(conversationId: string): Promise<Message[]>;
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
