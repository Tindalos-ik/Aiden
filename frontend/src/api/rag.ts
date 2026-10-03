import type { RagChunk, RagEvalReport, RagJob, RagMilvusSnapshot, RagMiningSnapshot, RagOverview, RejectionReason, ReviewDetail, ReviewQueueFilters, ReviewQueueResponse } from '../types';
import { apiMode } from './index';

const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');

/** 建库调用始终走真实远端；mock 页面会明确禁用，绝不生成模拟入库结果。 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (apiMode !== 'remote') throw new Error('演示模式不提供真实建库操作，请切换 remote 模式。');
  const response = await fetch(`${baseUrl}/rag${path}`, {
    ...init,
    credentials: 'include',
    headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  });
  if (!response.ok) {
    const body = await response.text().catch(() => '');
    let detail = '';
    try { detail = (JSON.parse(body) as { detail?: string }).detail || ''; } catch { /* plain response */ }
    throw new Error(detail || `建库 API 请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export const ragApi = {
  evalReport: () => request<RagEvalReport>('/evals/customer-rag-v1', { cache: 'no-store' }),
  workflowEvalReport: () => request<RagEvalReport>('/evals/customer-workflow-v2', { cache: 'no-store' }),
  overview: () => request<RagOverview>('/overview'),
  milvus: (offset = 0) => request<RagMilvusSnapshot>(`/milvus?limit=20&offset=${offset}`),
  preview: (file: string) => request<{ file: string; title: string; chunks: RagChunk[] }>(`/preview?file=${encodeURIComponent(file)}`),
  chunks: (offset = 0) => request<{ items: RagChunk[] }>(`/chunks?limit=20&offset=${offset}`),
  mining: () => request<RagMiningSnapshot>('/mining'),
  jobs: () => request<{ items: RagJob[] }>('/jobs', { cache: 'no-store' }),
  startJob: (kind: string, file?: string) => request<RagJob>(`/jobs/${encodeURIComponent(kind)}`, {
    method: 'POST', body: JSON.stringify(file ? { file } : {}),
  }),
  startEmbedding: () => request<{ state: string; managed: boolean }>('/embedding/start', { method: 'POST' }),
  stopEmbedding: () => request<{ state: string; managed: boolean }>('/embedding/stop', { method: 'POST' }),
  reviewQueue: (filters: ReviewQueueFilters) => {
    const params = new URLSearchParams({ status: filters.status, sort: filters.sort, limit: String(filters.limit ?? 20), offset: String(filters.offset) });
    if (filters.category !== undefined) params.set('category', filters.category);
    if (filters.uncategorized) params.set('uncategorized', 'true');
    if (filters.start_at) params.set('start_at', filters.start_at);
    if (filters.end_at) params.set('end_at', filters.end_at);
    if (filters.issue_type) params.set('issue_type', filters.issue_type);
    return request<ReviewQueueResponse>(`/review-queue?${params}`, { cache: 'no-store' });
  },
  reviewDetail: (id: string) => request<ReviewDetail>(`/review-queue/${encodeURIComponent(id)}`, { cache: 'no-store' }),
  approveReview: (id: string, body: { approved_answer: string; category: string; review_note: string }) =>
    request<ReviewDetail>(`/review-queue/${encodeURIComponent(id)}/approve`, { method: 'POST', body: JSON.stringify(body) }),
  rejectReview: (id: string, body: { rejection_reason: RejectionReason; review_note: string }) =>
    request<ReviewDetail>(`/review-queue/${encodeURIComponent(id)}/reject`, { method: 'POST', body: JSON.stringify(body) }),
  retryReviewIngestion: (id: string) => request<ReviewDetail>(`/review-queue/${encodeURIComponent(id)}/retry-ingestion`, { method: 'POST' }),
};
