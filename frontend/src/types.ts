export type Role = 'user' | 'staff';
export type ConversationStatus = 'bot' | 'waiting' | 'staff' | 'closed';
export type MessageRole = 'user' | 'assistant' | 'staff' | 'system';
export type MessageStatus = 'complete' | 'streaming' | 'error' | 'stopped';

export interface Actor {
  id: string;
  name: string;
  role: Role;
  email?: string;
  avatarColor?: string;
}

export interface Conversation {
  id: string;
  userId: string;
  userName?: string;
  subject: string;
  status: ConversationStatus;
  createdAt: string;
  updatedAt: string;
  assignedStaffId?: string;
  assignedStaffName?: string;
  lastMessagePreview?: string;
}

export interface OrderCard {
  orderId: string;
  product: string;
  amount: number;
  status: '物流中' | '已签收' | '退款中';
  logistics: string;
  carrier?: string;
  updatedAt?: string;
}

export interface Message {
  id: string;
  conversationId: string;
  role: MessageRole;
  content: string;
  createdAt: string;
  status: MessageStatus;
  orderCard?: OrderCard;
  toolStatuses?: string[];
}

export interface StreamEvent {
  type: 'start' | 'tool_status' | 'order_card' | 'delta' | 'handoff' | 'done' | 'error';
  messageId?: string;
  text?: string;
  status?: string;
  order?: OrderCard;
  error?: string;
  conversation?: Conversation;
}

export interface DemoAccount {
  id: string;
  name: string;
  role: Role;
  email: string;
  password: string;
  description: string;
  color: string;
}

export interface LoginInput {
  account: string;
  password: string;
  role?: Role;
}
