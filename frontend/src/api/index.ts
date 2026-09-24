import type { AidenApi } from './contracts';
import { mockApi } from './mock';
import { remoteApi } from './remote';

export const apiMode = import.meta.env.VITE_API_MODE === 'remote' ? 'remote' : 'mock';
export const api: AidenApi = apiMode === 'remote' ? remoteApi : mockApi;
