import { apiMode } from './index';
import type { ObservabilityFilters, ObservabilityStatus, ObservabilityOverview, ObservabilityTracePage, ObservabilityDetail } from '../types';

const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');
async function request<T>(path: string): Promise<T> {
  if (apiMode !== 'remote') throw new Error('演示模式不提供真实观测数据。');
  const response = await fetch(`${baseUrl}/observability${path}`, { credentials: 'include', cache: 'no-store' });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${body || '观测 API 请求失败'}（HTTP ${response.status}）`);
  }
  return response.json() as Promise<T>;
}
function params(filters: ObservabilityFilters): string {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)); });
  return query.toString();
}
export const observabilityApi = {
  status: () => request<ObservabilityStatus>('/status'),
  overview: (filters: ObservabilityFilters) => request<ObservabilityOverview>(`/overview?${params(filters)}`),
  traces: (filters: ObservabilityFilters, page: number, page_size = 20) => request<ObservabilityTracePage>(`/traces?${params(filters)}&page=${page}&page_size=${page_size}`),
  detail: (id: string, filters: ObservabilityFilters) => request<ObservabilityDetail>(`/traces/${encodeURIComponent(id)}?${params(filters)}`),
};
