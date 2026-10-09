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
  /** 仅当前 remote 流使用：final 已核验，但仍在等待 done。 */
  verified?: boolean;
}

/** remote delta 是未核验的追加草稿；final 是核验并落库后的权威正文与引用。 */
export type ProgressStage = 'recognition' | 'query' | 'retrieval' | 'validation';

export interface StreamEvent {
  type: 'start' | 'progress' | 'tool_status' | 'order_card' | 'delta' | 'final' | 'citations' | 'handoff' | 'done' | 'error';
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
  /** 一轮检索/生成/评审完成后推进；在线包含所有阈值试跑次数。 */
  evaluationProgress?: { completed: number; total: number };
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
  completeness?: number | null;
  false_refusal?: number | null;
  unsafe_answer?: number | null;
  latency_ms?: number | null;
  intent_correct?: number | null;
  tool_correct?: number | null;
  refund_correct?: number | null;
  handoff_correct?: number | null;
  blocked_tool_correct?: number | null;
  unanswerable_refusal_rate: number | null;
  scored_count?: number;
  preconditions_unmet_count?: number;
  tokens?: { input: number | null; output: number | null; total: number | null };
  judge_tokens?: { input: number | null; output: number | null; total: number | null };
  judge_latency_ms?: number | null;
}
 

export interface RagEvalCase {
  strategy: string;
  id: string;
  query: string;
  type: string;
  difficulty: string;
  dataset_version?: string;
  split?: 'validation' | 'holdout';
  semantic_cluster?: string;
  ground_truth: Array<{ source_path: string; section: string; authority_note?: string }>;
  answer_facts: string[];
  acceptable_answers?: string[];
  refusal_conditions?: string[];
  evidence_requirements?: string[];
  expected_refusal?: boolean;
  retrieved: Array<{ chunk_id?: string; source_path: string | null; section_path: string; score: number | null; content?: string }>;
  'recall@1': number | null;
  'recall@5': number | null;
  'recall@10': number | null;
  mrr: number | null;
  answer: string | null;
  faithfulness: number | null;
  completeness?: number | null;
  false_refusal?: number | null;
  unsafe_answer?: number | null;
  latency_ms?: number | null;
  tokens?: { input: number | null; output: number | null; total: number | null };
  judge_tokens?: { input: number | null; output: number | null; total: number | null };
  judge_reason: string | null;
  refused: 0 | 1 | null;
  manual_review?: { status: 'pending' | 'accepted' | 'disagreed'; reviewer?: string; notes?: string };
  actual_intents?: string[];
  actual_tools?: string[];
  refund_stage?: string | null;
  handoff?: boolean;
  handoff_committed?: boolean;
  node_trace?: string[];
  precondition_source?: string;
  preconditions?: {
    status: 'not_required' | 'missing_account' | 'account_not_found' | 'order_not_found' | 'satisfied';
    account_exists?: boolean;
  };
  persistence_disabled?: boolean;
  intent_correct?: number | null;
  tool_correct?: number | null;
  refund_correct?: number | null;
  blocked_tool_correct?: number | null;
  judge_latency_ms?: number | null;
  judge_evidence?: Array<Record<string, unknown>>;
  handoff_correct?: number | null;
  blocked_tools?: string[];
  workflow?: { expected_intents?: string[]; expected_tools?: string[]; expected_refund_stage?: string | null; expected_handoff?: boolean };

}
export interface RagEvalReport {
  schema_version?: 1 | 2;
  legacy?: boolean;
  evaluation_mode?: 'offline_retrieval' | 'online_workflow';
  execution_environment?: 'real' | 'fixture' | 'historical_unverified';
  dataset: string;
  collection: string;
  generated_at?: string;
  faithfulness_judge: string | null;
  metadata?: {
    effective_collection_sources?: string[];
    effective_collection_chunks?: number;
    dataset_version?: string;
    dataset_sha256?: string;
    corpus_version?: string | null;
    corpus_version_label?: string | null;
    corpus_manifest?: { sha256: string; count: number; chunks?: Array<{ id: string; source_path: string; section_path: string; content_hash: string; embedding_fingerprint: string; actual_content_sha256: string }>; sources: string[]; source: string };
    collection?: string;
    collection_version?: string | null;
    collection_manifest?: { sha256: string; count: number; source: string; chunk_ids?: string[] };
    embedding_model?: string;
    embedding_dimension?: number;
    generation_model?: string | null;
    judge_model?: string | null;
    judge_independent?: boolean | null;
    reranker_model?: string | null;
    online_threshold?: number | null;
    handoff_boundary?: string;
    model_metadata_source?: string;
    retrieved_sources?: string[];
    expected_sources?: string[];
    unobserved_sources?: string[];
    corpus_coverage_basis?: string;
    persistence_disabled?: boolean;
    allow_writes?: boolean;
    isolated_environment?: string | null;
  };
  strategies: Record<string, {
    overall: RagEvalMetrics;
    by_type: Record<string, RagEvalMetrics>;
    by_difficulty: Record<string, RagEvalMetrics>;
    by_split?: Record<string, RagEvalMetrics>;
  }>;
  workflows?: RagEvalMetrics | null;
  threshold_selection?: {
    validation_count: number;
    holdout_count: number;
    candidates: Array<{ threshold: number; loss: number; summary: RagEvalMetrics }>;
    selected_threshold: number;
    validation: RagEvalMetrics;
    holdout: RagEvalMetrics;
  } | null;
  cases: RagEvalCase[];
}

// 员工专用真实编码器主题分类；mock 适配器不得使用。
export interface TopicCheck { name: string; ready: boolean; code: string; message: string; missing_tables?: string[]; missing_columns?: Record<string, string[]> }
export interface TopicCursor { created_at: string; source_id: string }
export interface TopicBatchInput { start_at?: string; end_at?: string; limit: number; batch_size: number; after?: TopicCursor }
export interface TopicJob {
  id: string; kind: 'batch' | 'evaluation'; state: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted';
  created_at: string; started_at: string | null; finished_at: string | null;
  model_version: string | null; taxonomy_version: string; parameters: Record<string, unknown>;
  result: { processed?: number; persisted?: number; stale_source?: number; next_cursor?: TopicCursor | null; report_id?: string } | null;
  error: { code: string; message: string; stage?: string } | null; report_id: string | null;
}
export interface TopicAvailability {
  taxonomy: { version: string | null; labels: Array<{ id: string; display: string; include: string; exclude: string }> };
  selected_model_version: string | null; model_versions: string[]; checks: TopicCheck[];
  can_classify: boolean; can_evaluate: boolean; active_job: TopicJob | null;
}
export interface TopicPrediction {
  id: string; input_hash: string; scores: Record<string, number>; predicted_labels: string[]; status: string;
  model_version: string; taxonomy_version: string; created_at: string; updated_at: string; current: boolean;
}
export interface TopicHuman {
  id: string; input_hash: string; labels: string[]; status: string; reviewed_by_id: string;
  created_at: string; updated_at: string; current: boolean;
}
export interface TopicResult {
  source_id: string; question: string; input_hash: string; created_at: string;
  conversation_id: string | null; user_message_id: string | null; matched_review_id: string | null;
  current_prediction: TopicPrediction | null; latest_prediction: TopicPrediction | null;
  human_current: TopicHuman | null; human_latest: TopicHuman | null;
}
export interface TopicFilters {
  start_at?: string; end_at?: string; model_version?: string; topic?: string;
  status?: 'predicted' | 'uncertain' | 'unclassified'; current_prediction?: boolean; has_human?: boolean;
}
export interface TopicConclusions { quality: string; coverage: string; performance: string; threshold: 'unconfirmed' }
export interface TopicReportSummary {
  id: string; created_at: string | null; model_version: string | null; taxonomy_version: string | null;
  dataset_hash: string | null; test_split_hash: string | null; available: boolean; reason: string | null;
  conclusions: TopicConclusions | null;
}
export interface TopicStats {
  model_version: string; taxonomy_version: string; source_unique_count: number; review_unique_count: number;
  linked_reviews_lifetime_occurrence_count: number;
  model: { source_count: number; scope_source_count: number; unclassified_count: number; status_counts: Record<string, number>; label_counts: Record<string, number> };
  human: { source_count: number; scope_source_count: number; unreviewed_count: number; status_counts: Record<string, number>; label_counts: Record<string, number> };
  merged_review_label_counts: { model: Record<string, number>; human: Record<string, number> };
}
export interface TopicEvaluation {
  model_version: string; taxonomy_version: string; test_split_hash: string;
  dataset_hash: string; split_hashes: { train: string; validation: string; test: string };
  metrics: { micro_f1: number | null; macro_f1_supported_classes: number | null; supported_class_count: number; unevaluable_classes: string[];
    per_class: Record<string, { support: number; evaluable: boolean; precision: number | null; recall: number | null; f1: number | null; tp: number; fp: number; fn: number }> };
  coverage: { samples: number; groups: number; multilabel_samples: number; label_support: Record<string, number> };
  status_metrics: { confusion: Record<string, number>; label_confusion_pairs: Record<string, number>; revision_needed_count: number; revision_needed_rate: number; completed_human_revisions: number; insufficient_context_is_uncertain_for_comparison: boolean };
  errors: Array<{ id: string; redacted_text: string; human_truth_labels: string[]; expected_status: string; raw_prediction: unknown; missing: string[]; extra: string[]; human_revision: unknown }>;
  elapsed_seconds: number; samples_per_second: number; baseline_configuration?: unknown; resource_usage: unknown; usage: unknown; cost: number | null;
}
export type ObservationStatus = 'success' | 'error' | 'unknown';
export interface ObservabilityTokens { input: number | null; output: number | null; total: number | null; known_generations: number; total_generations: number; field_coverage?: Record<string, { known: number; missing: number }>; source?: string }
export interface ObservabilityTokenTrendPoint { date: string; model: string; bucket: string; generations: number; tokens: ObservabilityTokens }
export interface ObservabilityCoverage { observations_read: number; generations_read: number; matched_generations: number; matched_traces: number; matched_observations: number; source_matched_traces: number; excluded_by_source: number; excluded_by_filters: number; empty_reason: 'no_observations' | 'source_excluded' | 'filters_excluded' | null; truncated: boolean; limit: number; scope: string; start_at: string | null; end_at: string | null }
export interface ObservabilityAvailability { configured: boolean; query_verified: boolean; upstream: string; async_incomplete: boolean }
export interface ObservabilityEnvelope<T> { state: 'unconfigured' | 'configured_unverified' | 'ready' | 'empty' | 'auth_failed' | 'connection_failed' | 'unsupported'; message: string; queried_at: string; latest_record_at: string | null; coverage: ObservabilityCoverage; availability?: ObservabilityAvailability; data: T | null }
export type ObservabilityFilters = { start_at: string; end_at: string; source: string; max_observations: number; model?: string; intent?: string; status?: ObservationStatus; conversation_id?: string; message_id?: string };
export interface ObservabilityTrace { id: string; start_at: string | null; name: string; source: 'aiden_support' | 'other'; source_detail?: string; intents: string[]; intents_recorded?: boolean; models: string[]; status: ObservationStatus; duration_ms: number | null; tokens: ObservabilityTokens; conversation_id: string | null; message_id: string | null; external_url: string | null }
export interface ObservabilityTrendPoint { date: string; roots: number; generations: number; duration: { p95_ms: number | null }; tokens: ObservabilityTokens }
export interface ObservabilityInsights { facts: string[]; limitations: string[] }
export interface ObservabilitySlowItems { requests: Array<{ trace_id: string; start_at: string | null; duration_ms: number; intents: string[]; models: string[] }>; steps: Array<{ trace_id: string; observation_id: string; name: string; type: string; duration_ms: number; model: string | null }> }
export interface ObservabilityDataQuality { generations: number; provider_verified: number; usage_unrecorded: number; usage_unverified: number }
export interface ObservabilityOverviewData { counts: { roots: number; generations: number; success: number; error: number; unknown: number }; duration: { samples: number; mean_ms: number | null; p50_ms: number | null; p95_ms: number | null }; tokens: ObservabilityTokens; trend: ObservabilityTrendPoint[]; token_trend: ObservabilityTokenTrendPoint[]; models: Array<{ model: string; generations: number; tokens: ObservabilityTokens }>; attribution: Array<{ bucket: string; generations: number; tokens: ObservabilityTokens }>; insights: ObservabilityInsights; slow_items: ObservabilitySlowItems; data_quality: ObservabilityDataQuality }
export interface ObservabilityObservation { id: string; parent_id: string | null; name: string; type: string; start_at: string | null; end_at: string | null; status: ObservationStatus; duration_ms: number | null; model: string | null; tokens: ObservabilityTokens; usage_details: Record<string, number> | null; usage_details_source?: string; error_summary: string | null }
export interface ObservabilityStatusData { limits: { default_hours: number; max_days: number; default_observations: number; max_observations: number }; read_api: string; privacy: string; async_incomplete: boolean }
export type ObservabilityStatus = ObservabilityEnvelope<ObservabilityStatusData>;
export type ObservabilityOverview = ObservabilityEnvelope<ObservabilityOverviewData>;
export type ObservabilityTracePage = ObservabilityEnvelope<{ items: ObservabilityTrace[]; page: number; page_size: number; total: number }>;
export type ObservabilityDetail = ObservabilityEnvelope<{ trace: ObservabilityTrace; observations: ObservabilityObservation[]; structure: { observations: number; generations: number; roots: number; orphaned: number; missing_parents?: number; cross_boundary: boolean; truncated?: boolean }; notes: string[] }>;
