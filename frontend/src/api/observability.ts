import { apiMode } from './index';
import type { ObservabilityFilters, ObservabilityStatus, ObservabilityOverview, ObservabilityTracePage, ObservabilityDetail } from '../types';

const baseUrl = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');
async function request<T>(path: string): Promise<T> {
  if (apiMode !== 'remote') throw new Error('演示模式不提供真实观测数据。');
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/observability${path}`, { credentials: 'include', cache: 'no-store' });
  } catch {
    throw new Error('无法连接观测服务，请确认服务运行后手动刷新。');
  }
  if (!response.ok) {
    const message = response.status === 401
      ? '员工登录已失效，请重新登录后再试。'
      : response.status === 403
        ? '当前员工账号无权查看观测数据，请联系管理员。'
        : response.status === 404
        ? '观测接口不可用，请确认后端已部署观测功能。'
        : response.status >= 500
          ? '观测服务暂时无法读取数据，请稍后手动刷新。'
          : '观测查询未完成，请检查筛选条件后重试。';
    throw new Error(message);
  }
  try {
    return await response.json() as T;
  } catch {
    throw new Error('观测服务返回的数据格式无法识别，请检查服务版本后手动刷新。');
  }
}
function params(filters: ObservabilityFilters): string {
  const query = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)); });
  return query.toString();
}
export const observabilityApi = {
  status: () => request<ObservabilityStatus>('/status'),
  overview: async (filters: ObservabilityFilters) => {
    const result = await request<ObservabilityOverview>(`/overview?${params(filters)}`);
    // 字段切换后旧后端可能仍返回费用趋势；拒绝不兼容响应，不补造空 Token 趋势。
    if (result.data != null && !Array.isArray(result.data.token_trend)) {
      throw new Error('观测接口版本与页面不一致，请管理员更新并重启后端服务后刷新。');
    }
    return result;
  },
  traces: (filters: ObservabilityFilters, page: number, page_size = 20) => request<ObservabilityTracePage>(`/traces?${params(filters)}&page=${page}&page_size=${page_size}`),
  detail: (id: string, filters: ObservabilityFilters) => request<ObservabilityDetail>(`/traces/${encodeURIComponent(id)}?${params(filters)}`),
};
