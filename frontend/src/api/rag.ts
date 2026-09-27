import type { RagChunk, RagEvalReport, RagJob, RagMilvusSnapshot, RagMiningSnapshot, RagOverview } from '../types';
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
  evalReport: () => request<RagEvalReport>('/evals/customer-rag-v1'),
  overview: () => request<RagOverview>('/overview'),
  milvus: (offset = 0) => request<RagMilvusSnapshot>(`/milvus?limit=20&offset=${offset}`),
  preview: (file: string) => request<{ file: string; title: string; chunks: RagChunk[] }>(`/preview?file=${encodeURIComponent(file)}`),
  chunks: (offset = 0) => request<{ items: RagChunk[] }>(`/chunks?limit=20&offset=${offset}`),
  mining: () => request<RagMiningSnapshot>('/mining'),
  jobs: () => request<{ items: RagJob[] }>('/jobs'),
  startJob: (kind: string, file?: string) => request<RagJob>(`/jobs/${encodeURIComponent(kind)}`, {
    method: 'POST', body: JSON.stringify(file ? { file } : {}),
  }),
  startEmbedding: () => request<{ state: string; managed: boolean }>('/embedding/start', { method: 'POST' }),
  stopEmbedding: () => request<{ state: string; managed: boolean }>('/embedding/stop', { method: 'POST' }),
};
