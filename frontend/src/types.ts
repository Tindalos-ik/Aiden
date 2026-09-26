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

export interface RagChunk {
  id?: string;
  index?: number;
  sourceType?: string;
  sourcePath?: string | null;
  sectionPath: string;
  category: string;
  questions: string[];
  content: string;
  contentType: string;
  isKeyClause: boolean;
  needsManualReview?: boolean;
  reviewReason?: string | null;
  vectorStatus?: string;
  vectorId?: string | null;
}

export interface RagJob {
  id: string;
  kind: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  progress: string | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  result: unknown;
}

export interface RagOverview {
  embedding: {
    healthy: boolean;
    managed: boolean;
    processState: string;
    exitCode: number | null;
    model: string | null;
    dimension: number | null;
    healthError: string | null;
    canStart: boolean;
  };
  chunksByStatus: Record<string, number>;
  batchesByStatus: Record<string, number>;
  candidatesByStatus: Record<string, number>;
  milvus: { collection: string; dimension: number; metric: string; index: string };
  documents: string[];
  databaseError: string | null;
  documentsError: string | null;
}

export interface RagCandidate {
  candidate_id: string;
  question: string;
  answer: string;
  category: string;
  status: string;
  rejection_reason: string | null;
}

export interface RagMiningSnapshot {
  batches: Array<{ id: string; runId: string | null; status: string; turnCount: number; candidateCount: number; error: string | null; updatedAt: string | null }>;
  candidates: Record<string, RagCandidate[]>;
}
