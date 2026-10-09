import { apiMode } from './index';
import type { TopicAvailability, TopicBatchInput, TopicConclusions, TopicEvaluation, TopicFilters, TopicJob, TopicReportSummary, TopicResult, TopicStats } from '../types';

const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');
export class TopicApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = 'TopicApiError'; }
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (apiMode !== 'remote') throw new Error('演示模式不提供真实主题分类或评测操作。');
  const response = await fetch(`${baseUrl}/topics${path}`, { ...init, credentials: 'include', cache: 'no-store', headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers } });
  if (!response.ok) {
    let text = await response.text();
    try {
      const parsed: unknown = JSON.parse(text);
      const detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? parsed.detail : undefined;
      if (typeof detail === 'string') text = detail;
      else if (detail && typeof detail === 'object') {
        const lines: string[] = [];
        if ('code' in detail && typeof detail.code === 'string') lines.push(detail.code);
        if ('message' in detail && typeof detail.message === 'string') lines.push(detail.message);
        for (const key of ['missing_tables', 'missing_columns']) {
          if (key in detail) {
            const missing = Reflect.get(detail, key);
            if (Array.isArray(missing)) lines.push(`${key}: ${missing.filter((item) => typeof item === 'string').join('、')}`);
          }
        }
        if ('checks' in detail && Array.isArray(detail.checks)) {
          detail.checks.forEach((check: unknown) => {
            if (check && typeof check === 'object' && 'ready' in check && check.ready === false && 'name' in check && 'message' in check) lines.push(`${String(check.name)}: ${String(check.message)}`);
          });
        }
        text = lines.join('\n') || JSON.stringify(detail);
      }
    } catch { /* 保留服务端纯文本错误。 */ }
    throw new TopicApiError(`${response.status === 409 ? '已有活动任务，不能重复启动。' : ''}${text || '主题 API 请求失败'}（HTTP ${response.status}）`, response.status);
  }
  return response.json() as Promise<T>;
}
function params(filters: TopicFilters): URLSearchParams {
  const result = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '') result.set(key, String(value)); });
  return result;
}
export const topicApi = {
  availability: (model_key?: string) => request<TopicAvailability>(`/availability${model_key ? `?model_key=${encodeURIComponent(model_key)}` : ''}`),
  jobs: () => request<{ items: TopicJob[] }>('/jobs'),
  job: (id: string) => request<TopicJob>(`/jobs/${encodeURIComponent(id)}`),
  startBatch: (body: TopicBatchInput) => request<TopicJob>('/jobs/batch', { method: 'POST', body: JSON.stringify(body) }),
  startEvaluation: (model_key: string, batch_size: number) => request<TopicJob>('/jobs/evaluation', { method: 'POST', body: JSON.stringify({ model_key, batch_size }) }),
  results: (filters: TopicFilters, offset: number) => request<{ items: TopicResult[]; total: number; limit: number; offset: number }>(`/results?${params(filters)}&limit=20&offset=${offset}`),
  result: (id: string, model_version?: string) => request<TopicResult>(`/results/${encodeURIComponent(id)}?${params({ model_version })}`),
  stats: (filters: Pick<TopicFilters, 'model_version' | 'start_at' | 'end_at'>) => request<TopicStats>(`/stats?${params(filters)}`),
  reports: (model_version: string) => request<{ items: TopicReportSummary[] }>(`/reports?${params({ model_version })}`),
  report: (id: string) => request<{ id: string; created_at: string; model_key: string | null; data_mode: TopicEvaluation['data_mode']; evaluation_source: string | null; report: TopicEvaluation; conclusions: TopicConclusions }>(`/reports/${encodeURIComponent(id)}`),
};
