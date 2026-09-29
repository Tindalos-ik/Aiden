import { useEffect } from 'react';
import { Alert, Button } from 'antd';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { api, apiMode } from './api';
import { LoginPage } from './pages/LoginPage';
import { UserWorkspace } from './pages/UserWorkspace';
import { StaffWorkspace } from './pages/StaffWorkspace';
import { UserServicePage, StaffServicePage } from './pages/ServicePages';
import { RagConsole } from './pages/RagConsole';
import { RagEvals } from './pages/RagEvals';
import { ReviewQueue } from './pages/ReviewQueue';
import { mockChannelName, mockStorageKey } from './api/mock';
import type { Role } from './types';
import { Brand, SpinnerPage } from './components/Common';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 2_000, retry: 1, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});

function CrossTabSync() {
  useEffect(() => {
    if (apiMode !== 'mock') return;
    let timer: number | undefined;
    const refresh = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: ['conversations'] });
        void queryClient.invalidateQueries({ queryKey: ['messages'] });
        void queryClient.invalidateQueries({ queryKey: ['staff-queue'] });
      }, 120);
    };
    const storage = (event: StorageEvent) => { if (!event.key || event.key === mockStorageKey) refresh(); };
    const channel = 'BroadcastChannel' in window ? new BroadcastChannel(mockChannelName) : undefined;
    channel?.addEventListener('message', refresh);
    window.addEventListener('storage', storage);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('storage', storage);
      channel?.close();
    };
  }, []);
  return null;
}

function Landing() {
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['me'], queryFn: api.me, retry: 0 });
  if (isLoading) return <SpinnerPage />;
  if (isError) return <ConnectionError detail={error instanceof Error ? error.message : '无法连接服务'} onRetry={() => void refetch()} />;
  if (!data) return <Navigate to="/login" replace />;
  return <Navigate to={data.role === 'staff' ? '/staff' : '/app'} replace />;
}

function ConnectionError({ detail, onRetry }: { detail: string; onRetry: () => void }) {
  return <div className="full-screen-state"><div className="connection-card"><Brand /><Alert type="error" showIcon message="无法恢复登录状态" description={detail} /><Button type="primary" onClick={onRetry}>重试连接</Button></div></div>;
}

function RoleGate({ role, children }: { role: Role; children: React.ReactNode }) {
  const { data, isLoading, isError, error, refetch } = useQuery({ queryKey: ['me'], queryFn: api.me, retry: 0 });
  if (isLoading) return <SpinnerPage />;
  if (isError) return <ConnectionError detail={error instanceof Error ? error.message : '无法连接服务'} onRetry={() => void refetch()} />;
  if (!data) return <Navigate to="/login" replace />;
  if (data.role !== role) return <Navigate to={data.role === 'staff' ? '/staff' : '/app'} replace />;
  return <>{children}</>;
}

function AppRoutes() {
  return <>
    <CrossTabSync />
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<Landing />} />
      <Route path="/app" element={<RoleGate role="user"><UserWorkspace /></RoleGate>} />
      <Route path="/app/service" element={<RoleGate role="user"><UserServicePage /></RoleGate>} />
      <Route path="/app/:conversationId" element={<RoleGate role="user"><UserWorkspace /></RoleGate>} />
      <Route path="/staff" element={<RoleGate role="staff"><StaffWorkspace /></RoleGate>} />
      <Route path="/staff/service" element={<RoleGate role="staff"><StaffServicePage /></RoleGate>} />
      <Route path="/staff/rag" element={<RoleGate role="staff"><RagConsole /></RoleGate>} />
      <Route path="/staff/reviews" element={<RoleGate role="staff"><ReviewQueue /></RoleGate>} />
      <Route path="/staff/evals" element={<RoleGate role="staff"><RagEvals /></RoleGate>} />
      <Route path="/staff/:conversationId" element={<RoleGate role="staff"><StaffWorkspace /></RoleGate>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  </>;
}

export default function App() {
  return <QueryClientProvider client={queryClient}><BrowserRouter><AppRoutes /></BrowserRouter></QueryClientProvider>;
}
