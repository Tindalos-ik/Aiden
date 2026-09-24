import { useState } from 'react';
import { Alert, Button, Card, Form, Input, Radio, Tag, Typography, message } from 'antd';
import { ArrowRightOutlined, CustomerServiceOutlined, LockOutlined, MailOutlined, RobotOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { api, apiMode } from '../api';
import { demoAccounts } from '../data/demoData';
import { Brand, ModePill } from '../components/Common';
import type { LoginInput, Role } from '../types';

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [role, setRole] = useState<Role>('user');
  const login = useMutation({
    mutationFn: (input: LoginInput) => api.login(input),
    onSuccess: (actor) => {
      queryClient.setQueryData(['me'], actor);
      navigate(actor.role === 'staff' ? '/staff' : '/app', { replace: true });
    },
    onError: (error) => message.error(error instanceof Error ? error.message : '登录失败'),
  });
  const remoteLogin = (values: { account: string; password: string }) => login.mutate({ ...values, role });
  const signInDemo = (accountId: string, password: string) => login.mutate({ account: accountId, password });

  return <main className="login-page">
    <section className="login-aside">
      <div className="login-aside-inner">
        <Brand />
        <div className="login-aside-copy">
          <div className="eyebrow"><span className="eyebrow-line" />订单服务 · 更懂你的每一步</div>
          <h1>让每一次<br /><span>服务都恰到好处。</span></h1>
          <p>{apiMode === 'mock' ? 'Aiden 帮你看订单、追物流，也随时为你连接真人客服。' : '通过智能客服进行对话；订单数据与人工客服暂未接入。'}</p>
          <div className="login-benefits">
            <div><span className="benefit-icon"><RobotOutlined /></span><span><b>{apiMode === 'mock' ? '智能订单助手' : '智能对话客服'}</b><small>{apiMode === 'mock' ? '订单信息快速核对，进度一目了然' : '回答一般咨询，具体订单数据暂未接入'}</small></span></div>
            <div><span className="benefit-icon benefit-human"><CustomerServiceOutlined /></span><span><b>{apiMode === 'mock' ? '人工随时接力' : '人工客服暂未接入'}</b><small>{apiMode === 'mock' ? '复杂问题由专员接入，服务不断线' : '当前版本提供智能对话闭环'}</small></span></div>
          </div>
        </div>
        <div className="login-aside-footer"><SafetyCertificateOutlined /> 订单隐私受保护 <span>·</span> {apiMode === 'mock' ? '服务记录可追溯' : '会话历史持久保存'}</div>
      </div>
      <div className="aside-decoration aside-decoration-one" /><div className="aside-decoration aside-decoration-two" />
    </section>
    <section className="login-main">
      <div className="login-panel">
        <div className="login-heading"><div className="login-mode"><ModePill /></div><Typography.Title level={2}>{apiMode === 'mock' ? '欢迎来到演示工作台' : '欢迎回来'}</Typography.Title><Typography.Paragraph type="secondary">{apiMode === 'mock' ? '选择一个演示身份，完整体验智能客服与人工协作流程。' : '使用本地开发演示账号登录智能客服。'}</Typography.Paragraph></div>
        {apiMode === 'mock' ? <>
          <Alert className="demo-alert" type="info" showIcon message="演示模式" description="此工作台使用本地模拟数据，不会连接真实订单系统。演示数据保存在此浏览器中，可随时重置。" />
          <div className="demo-account-heading"><span>选择演示身份</span><span className="demo-account-hint">点击即可进入</span></div>
          <div className="demo-accounts">
            {demoAccounts.map((account) => <Card key={account.id} className={`demo-account ${account.role === 'staff' ? 'demo-account-staff' : ''}`} bordered={false}>
              <div className="demo-account-main">
                <span className="demo-avatar" style={{ background: `${account.color}15`, color: account.color }}>{account.role === 'staff' ? <CustomerServiceOutlined /> : <RobotOutlined />}</span>
                <span className="demo-account-info"><span className="demo-name">{account.name}<Tag className={account.role === 'staff' ? 'role-tag staff-role' : 'role-tag'}>{account.role === 'staff' ? '人工客服' : '普通用户'}</Tag></span><span className="demo-desc">{account.description}</span><span className="demo-credentials">{account.email} <i>·</i> {account.password}</span></span>
                <Button type="text" className="demo-enter" icon={<ArrowRightOutlined />} aria-label={`以${account.name}身份进入`} loading={login.isPending} onClick={() => signInDemo(account.id, account.password)} />
              </div>
            </Card>)}
          </div>
          <div className="demo-footer"><span className="demo-footer-dot" /> 同一浏览器开两个标签页，可使用用户和客服身份演示转人工</div>
        </> : <Card className="remote-login-card" bordered={false}>
          <Alert className="demo-alert" type="info" showIcon message="本地开发演示账号" description={<>普通用户：maya@aiden.demo / aiden123，或 chen@aiden.demo / aiden123。第一版尚未接入人工客服工作台。</>} />
          <Radio.Group value={role} onChange={(event) => setRole(event.target.value as Role)} optionType="button" buttonStyle="solid" className="role-selector">
            <Radio.Button value="user">普通用户</Radio.Button><Radio.Button value="staff" disabled>人工客服（未接入）</Radio.Button>
          </Radio.Group>
          <Form layout="vertical" onFinish={remoteLogin} requiredMark={false}>
            <Form.Item label="账号" name="account" rules={[{ required: true, message: '请输入账号' }]}><Input size="large" prefix={<MailOutlined />} placeholder="邮箱或账号" autoComplete="username" /></Form.Item>
            <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}><Input.Password size="large" prefix={<LockOutlined />} placeholder="请输入密码" autoComplete="current-password" /></Form.Item>
            <Button type="primary" htmlType="submit" size="large" block loading={login.isPending}>登录并继续</Button>
          </Form>
          <div className="remote-note"><SafetyCertificateOutlined /> 登录凭证由服务端校验，身份由 HttpOnly Cookie 维护</div>
        </Card>}
        <div className="login-legal">Aiden 智能订单客服 <span>·</span> 为安心服务而设计</div>
      </div>
    </section>
  </main>;
}
