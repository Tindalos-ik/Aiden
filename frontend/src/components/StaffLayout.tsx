import { useState, type ReactNode } from 'react';
import { Button, Drawer, Tooltip } from 'antd';
import { BarChartOutlined, CustomerServiceOutlined, DatabaseOutlined, ExperimentOutlined, FileSearchOutlined, MenuFoldOutlined, MenuOutlined, MenuUnfoldOutlined, RadarChartOutlined, SolutionOutlined } from '@ant-design/icons';
import { Link, useLocation } from 'react-router-dom';
import { PageHeader } from './Common';
import type { Actor } from '../types';

const navigation = [
  { title: '客户服务', items: [
    { path: '/staff', label: '客服工作台', icon: <CustomerServiceOutlined /> },
    { path: '/staff/service', label: '售后与工单', icon: <SolutionOutlined /> },
  ] },
  { title: '知识运营', items: [
    { path: '/staff/rag', label: 'RAG 建库', icon: <DatabaseOutlined /> },
    { path: '/staff/reviews', label: '问题审核', icon: <FileSearchOutlined /> },
    { path: '/staff/evals', label: 'RAG 评估', icon: <ExperimentOutlined /> },
  ] },
  { title: '分析观测', items: [
    { path: '/staff/topics', label: '主题分类器', icon: <RadarChartOutlined /> },
    { path: '/staff/observability', label: '观测与用量', icon: <BarChartOutlined /> },
  ] },
];

/** 仅承载员工导航与页面外壳，鉴权、请求和页面状态仍由现有调用方管理。 */
export function StaffLayout({ actor, children, className = '' }: { actor: Actor; children: ReactNode; className?: string }) {
  const { pathname } = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [navigationOpen, setNavigationOpen] = useState(false);
  const selectedPath = navigation.flatMap((group) => group.items).find((item) => item.path !== '/staff' && (pathname === item.path || pathname.startsWith(`${item.path}/`)))?.path ?? '/staff';
  const navigationPanels = [collapsed, false].map((compact, index) => <nav key={index} className={`staff-navigation ${compact ? 'staff-navigation-compact' : ''}`} aria-label="员工功能导航">
    {navigation.map((group) => <div className="staff-navigation-group" key={group.title}>
      <div className="staff-navigation-heading">{group.title}</div>
      {group.items.map((item) => <Tooltip key={item.path} title={compact ? item.label : undefined} placement="right">
        <Link to={item.path} className={`staff-navigation-link ${selectedPath === item.path ? 'is-selected' : ''}`} aria-label={item.label} aria-current={selectedPath === item.path ? 'page' : undefined} onClick={() => setNavigationOpen(false)}>
          {item.icon}<span>{item.label}</span>
        </Link>
      </Tooltip>)}
    </div>)}
  </nav>);
  return <div className={`staff-layout ${collapsed ? 'staff-layout-collapsed' : ''} ${className}`}>
    <div className="staff-layout-header">
      <Button className="staff-navigation-trigger" type="text" icon={<MenuOutlined />} aria-label="打开员工导航" onClick={() => setNavigationOpen(true)} />
      <PageHeader actor={actor} />
    </div>
    <div className="staff-layout-body">
      <aside className="staff-layout-sidebar">
        <div className="staff-sidebar-control"><span>员工中心</span><Tooltip title={collapsed ? '展开导航' : '折叠导航'} placement="right"><Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} aria-label={collapsed ? '展开导航' : '折叠导航'} onClick={() => setCollapsed(!collapsed)} /></Tooltip></div>
        {navigationPanels[0]}
      </aside>
      <div className="staff-content">{children}</div>
    </div>
    <Drawer rootClassName="staff-navigation-drawer" title="员工导航" placement="left" width={280} open={navigationOpen} onClose={() => setNavigationOpen(false)}>{navigationPanels[1]}</Drawer>
  </div>;
}
