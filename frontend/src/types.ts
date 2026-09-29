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
  assignedStaffId?: string | null;
  assignedStaffName?: string | null;
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

export type AfterSaleStatus = 'pending' | 'approved' | 'rejected' | 'processing' | 'awaiting_external_refund' | 'completed' | 'cancelled';
export type TicketStatus = 'open' | 'in_progress' | 'resolved' | 'closed';
export interface ServiceOrder { orderNo: string; status: string; items: Array<{ id: string; name: string; quantity: number }> }
export interface AfterSalePreview extends ServiceOrder {
  orderId: string;
  orderStatus: string;
  items: Array<{ id: string; name: string; quantity: number; lineTotal: string }>;
  policies: Array<{ id: string; name: string; version: string; content: string }>;
}
export interface AfterSaleApplication {
  id: string; requestNo: string; userId: string; orderId: string; orderNo: string | null;
  orderItemId: string | null; requestType: 'refund' | 'return' | 'exchange'; reason: string;
  status: AfterSaleStatus; requestedAmount: string | null; policyReference: string | null;
  reviewedById: string | null; reviewedAt: string | null; processingAt: string | null;
  processingById: string | null; resolvedById: string | null;
  resolvedAt: string | null; decisionNote: string | null; createdAt: string; updatedAt: string;
  alreadyExists?: boolean;
  submissionKey?: string;
  itemName?: string | null;
  policyName?: string | null;
  policyEvidence?: string | null;
}
export interface ServiceTicket {
  id: string; ticketNo: string; userId: string; conversationId: string;
  issueType: string; description: string; status: TicketStatus;
  assignedStaffId: string | null; afterSaleRequestId: string | null;
  acceptedAt: string | null; resolvedAt: string | null; closedAt: string | null;
  closedById: string | null; resolutionNote: string | null; createdAt: string; updatedAt: string;
}
export interface SubmitAfterSaleInput {
  orderNo: string; orderItemId: string; requestType: AfterSaleApplication['requestType'];
  reason: string; policyReference: string; submissionKey: string; confirmed: boolean;
  sourceTicketNo?: string;
}

/** [number] 对应回答中的引用编号；content 是被引用 chunk 的原文。 */
export interface Citation {
  number: number;
  chunkId: string;
  content: string;
  sectionPath: string;
  sourcePath?: string | null;
  sourceUrl?: string | null;
}

export interface Message {
  id: string;
  conversationId: string;
  role: MessageRole;
  content: string;
  createdAt: string;
  status: MessageStatus;
  feedback?: 'satisfied' | 'unsatisfied' | null;
  orderCard?: OrderCard;
  toolStatuses?: string[];
  citations?: Citation[];
}

export interface StreamEvent {
  type: 'start' | 'tool_status' | 'order_card' | 'delta' | 'citations' | 'handoff' | 'done' | 'error';
  messageId?: string;
  text?: string;
  status?: string;
  order?: OrderCard;
  error?: string;
  conversation?: Conversation;
  citations?: Citation[];
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
    dimensionPending: boolean;
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

export interface RagMilvusSnapshot {
  collection: string;
  exists: boolean | null;
  dimension: number | null;
  count: number | null;
  items: Array<{
    chunkId: string;
    sourceType: string;
    sourcePath: string;
    category: string;
    sectionPath: string;
    contentType: string;
    mysqlStatus: string;
  }>;
  error: string | null;
  mysqlError: string | null;
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

export interface RagEvalMetrics {
  count: number;
  answerable: number;
  'recall@1': number | null;
  'recall@5': number | null;
  'recall@10': number | null;
  mrr: number | null;
  faithfulness: number | null;
  unanswerable_refusal_rate: number | null;
}

export interface RagEvalCase {
  strategy: string;
  id: string;
  query: string;
  type: string;
  difficulty: string;
  ground_truth: Array<{ source_path: string; section: string }>;
  answer_facts: string[];
  retrieved: Array<{ chunk_id: string; source_path: string | null; section_path: string; score: number }>;
  'recall@1': number | null;
  'recall@5': number | null;
  'recall@10': number | null;
  mrr: number | null;
  answer: string | null;
  faithfulness: number | null;
  judge_reason: string | null;
  refused: 0 | 1 | null;
}

export interface RagEvalReport {
  dataset: string;
  collection: string;
  generated_at?: string;
  faithfulness_judge: string | null;
  strategies: Record<string, {
    overall: RagEvalMetrics;
    by_type: Record<string, RagEvalMetrics>;
    by_difficulty: Record<string, RagEvalMetrics>;
  }>;
  cases: RagEvalCase[];
}
