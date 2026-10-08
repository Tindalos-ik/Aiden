import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Avatar, Button, Dropdown, Modal, Tag, Typography, message } from 'antd';
import { CustomerServiceOutlined, DatabaseOutlined, DownOutlined, LogoutOutlined, ReloadOutlined, RobotOutlined } from '@ant-design/icons';
import { api, apiMode } from '../api';
import type { Actor } from '../types';

export function Brand({ compact = false }: { compact?: boolean }) {
  return <div className={`brand ${compact ? 'brand-compact' : ''}`}><span className="brand-mark"><span /><span /><span /><span /></span><span className="brand-name">aiden<span>support</span></span></div>;
}

export function ModePill() {
  return apiMode === 'mock'
    ? <Tag className="mode-tag mode-mock"><span className="mode-dot" />演示模式 · 模拟流式输出</Tag>
    : <Tag className="mode-tag mode-remote"><span className="mode-dot" />真实 API · SSE 流式连接</Tag>;
}

export function StatusPill({ status }: { status: 'bot' | 'waiting' | 'staff' | 'closed' }) {
  const map = {
    bot: { text: 'Aiden 服务中', cls: 'status-bot' },
    waiting: { text: '等待人工接入', cls: 'status-waiting' },
    staff: { text: '人工服务中', cls: 'status-staff' },
    closed: { text: '会话已结束', cls: 'status-closed' },
  } as const;
  return <Tag className={`status-tag ${map[status].cls}`}><span className="status-dot" />{map[status].text}</Tag>;
}

export function PageHeader({ actor }: { actor: Actor }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [busy, setBusy] = useState(false);
  const reset = () => Modal.confirm({
    title: '重置演示数据？',
    content: '将恢复初始示例会话和订单，当前演示产生的对话会被清除。',
    okText: '重置数据',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      setBusy(true);
      try { await api.resetDemoData(); message.success('演示数据已恢复'); }
      catch (error) { message.error(error instanceof Error ? error.message : '重置失败'); }
      finally { setBusy(false); }
    },
  });
  const logout = async () => {
    try { await api.logout(); }
    catch (error) { message.error(error instanceof Error ? error.message : '退出失败'); return; }
    navigate('/login', { replace: true });
  };
  return <header className="app-topbar">
    <div className="topbar-left"><Brand compact /><span className="topbar-divider" /><span className="workspace-label">{location.pathname.includes('/service') ? '售后与工单' : actor.role === 'staff' ? location.pathname.startsWith('/staff/topics') ? '旁路主题分类' : location.pathname.startsWith('/staff/reviews') ? '问题审核队列' : location.pathname.startsWith('/staff/evals') ? 'RAG 评估' : location.pathname.startsWith('/staff/rag') ? 'RAG 建库控制台' : '人工客服工作台' : '智能订单客服'}</span></div>
    <div className="topbar-right"><ModePill />
      <Button type="text" onClick={() => navigate(actor.role === 'staff' ? '/staff/service' : '/app/service')}>售后与工单</Button>
      {actor.role !== 'staff' && location.pathname.includes('/service') && <Button type="text" onClick={() => navigate('/app')}>返回会话</Button>}
      {actor.role === 'staff' && <Button type="text" icon={<DatabaseOutlined />} onClick={() => navigate('/staff/rag')}>RAG 建库</Button>}
      {actor.role === 'staff' && <Button type="text" onClick={() => navigate('/staff/reviews')}>问题审核</Button>}
      {actor.role === 'staff' && <Button type="text" onClick={() => navigate('/staff/topics')}>主题分类器</Button>}
      {actor.role === 'staff' && <Button type="text" icon={<CustomerServiceOutlined />} onClick={() => navigate('/staff')}>客服工作台</Button>}
      {apiMode === 'mock' && <Button className="reset-button" type="text" icon={<ReloadOutlined />} loading={busy} onClick={reset}>重置演示</Button>}
      <Dropdown menu={{ items: [
        { key: 'identity', label: <span>{actor.name} · {actor.role === 'staff' ? '客服专员' : '普通用户'}</span>, disabled: true },
        ...(apiMode === 'mock' ? [{ key: 'reset', label: '重置演示数据', icon: <ReloadOutlined />, onClick: reset }] : []),
        ...(actor.role === 'staff' ? [{ key: 'rag', label: 'RAG 建库控制台', icon: <DatabaseOutlined />, onClick: () => navigate('/staff/rag') }] : []),
        ...(actor.role === 'staff' ? [{ key: 'reviews', label: '低置信度问题审核', onClick: () => navigate('/staff/reviews') }] : []),
        ...(actor.role === 'staff' ? [{ key: 'topics', label: '主题分类器', onClick: () => navigate('/staff/topics') }] : []),
        ...(actor.role === 'staff' ? [{ key: 'service', label: '人工客服工作台', icon: <CustomerServiceOutlined />, onClick: () => navigate('/staff') }] : []),
        { key: 'logout', label: '退出登录', icon: <LogoutOutlined />, onClick: logout },
      ] }} trigger={['click']}>
        <button className="account-trigger"><Avatar size={32} style={{ backgroundColor: actor.avatarColor || '#5b68c8' }}>{actor.name.slice(0, 1)}</Avatar><span className="account-name">{actor.name}</span><DownOutlined className="account-chevron" /></button>
      </Dropdown>
    </div>
  </header>;
}

export function SpinnerPage({ label = '正在连接工作台…' }: { label?: string }) {
  return <div className="full-screen-state"><div className="loading-orb"><RobotOutlined /></div><Typography.Text type="secondary">{label}</Typography.Text></div>;
}

export function RoleIcon({ staff = false }: { staff?: boolean }) {
  return staff ? <CustomerServiceOutlined /> : <RobotOutlined />;
}
