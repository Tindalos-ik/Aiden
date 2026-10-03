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
  policies: Array<{ id: string; name: string; version: string; content: string; snapshot: string }>;
}
export interface ServiceProduct {
  id: string;
  name: string;
  skuName: string | null;
  specification: Record<string, unknown> | null;
  quantity: number;
  lineTotal: string;
}
export interface AfterSaleApplication {
  id: string; requestNo: string; userId: string; orderId: string; orderNo: string | null;
  orderItemId: string | null; requestType: 'refund' | 'return' | 'exchange'; reason: string;
  products: ServiceProduct[];
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
  products: ServiceProduct[]; orderNo: string | null; afterSaleRequestNo: string | null;
  acceptedAt: string | null; resolvedAt: string | null; closedAt: string | null;
  closedById: string | null; resolutionNote: string | null; createdAt: string; updatedAt: string;
}
export interface SubmitAfterSaleInput {
  orderNo: string; orderItemId: string; requestType: AfterSaleApplication['requestType'];
  reason: string; policyReference: string; policySnapshot: string; submissionKey: string; confirmed: boolean;
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

/** remote 进度仅用于瞬态界面；delta 仅承载服务端完成校验并落库后的整轮回答。 */
export type ProgressStage = 'recognition' | 'query' | 'retrieval' | 'validation';

export interface StreamEvent {
  type: 'start' | 'progress' | 'tool_status' | 'order_card' | 'delta' | 'citations' | 'handoff' | 'done' | 'error';
  messageId?: string;
  text?: string;
  status?: string;
  stage?: ProgressStage;
  message?: string;
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

export type ReviewStatus = 'pending' | 'approved' | 'rejected' | 'all';
export type ReviewSort = 'recent' | 'heat';
export type ReviewIssueType = 'knowledge_gap' | 'retrieval_miss' | 'policy_gap' | 'service_failure' | 'unclassified';
export type RejectionReason = 'existing_knowledge_not_retrieved' | 'not_reusable' | 'outdated' | 'other';

export interface ReviewQueueItem {
  id: string;
  normalized_question: string;
  example_answer: string | null;
  /** 全生命周期出现次数，不受当前日期筛选影响。 */
  occurrence_count: number;
  /** 列表按当前筛选范围统计；详情接口覆盖全部关联原话。 */
  scoped_occurrence_count: number;
  /** 不满意反馈次数；详情覆盖全部关联反馈。 */
  scoped_feedback_count: number;
  /** 按范围内原话证据分类；详情返回全部关联原话的类型。 */
  issue_types: ReviewIssueType[];
  review_status: Exclude<ReviewStatus, 'all'>;
  approved_answer: string | null;
  category: string | null;
  review_note: string | null;
  rejection_reason: RejectionReason | null;
  ingestion_status: 'not_started' | 'pending' | 'ready' | 'manual_review' | 'failed';
  ingestion_error: string | null;
  faq_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReviewQueueStatistics {
  original_question_count: number;
  merged_question_count: number;
  /** 仅 user_feedback_unresolved 不满意反馈，按 source_assistant_message_id 跨归并问题去重。 */
  feedback_count: number;
  review_status_counts: Record<Exclude<ReviewStatus, 'all'>, number>;
  ingestion_status_counts: Record<ReviewQueueItem['ingestion_status'], number>;
  issue_type_counts: Record<ReviewIssueType, number>;
}

export interface ReviewQueueResponse {
  items: ReviewQueueItem[];
  total: number;
  statistics: ReviewQueueStatistics;
  categories: Array<string | null>;
}

export interface ReviewQueueFilters {
  status: ReviewStatus;
  sort: ReviewSort;
  offset: number;
  limit?: number;
  category?: string;
  uncategorized?: boolean;
  /** 原始问题创建时间的 UTC 半开区间。 */
  start_at?: string;
  end_at?: string;
  issue_type?: ReviewIssueType;
}

export interface ReviewOriginal {
  id: string;
  raw_question: string;
  created_at: string;
  source: string;
  reason: string;
  retrieval_query: string | null;
  retrieval_status: 'searched' | 'not_searched' | 'error' | null;
  /** null = 旧记录未保存；[] = 当次无片段，必须结合 retrieval_status 判断是否检索。 */
  retrieval_snapshot: null | Array<{
    rank: number;
    chunk_id: string;
    content: string;
    source: string | null;
    section: string | null;
    score: number | null;
  }>;
}

export interface ReviewDetail extends ReviewQueueItem {
  originals: ReviewOriginal[];
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
