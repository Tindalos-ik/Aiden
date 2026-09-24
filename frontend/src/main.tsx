import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{
      token: {
        colorPrimary: '#5865d8',
        colorInfo: '#5865d8',
        colorSuccess: '#1c9a79',
        colorWarning: '#dc9c38',
        colorError: '#d75e5e',
        borderRadius: 10,
        fontFamily: 'Inter, "SF Pro Display", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        controlHeight: 40,
      },
      components: {
        Button: { fontWeight: 600, primaryShadow: '0 3px 10px rgba(79, 94, 207, 0.18)' },
        Input: { activeShadow: '0 0 0 3px rgba(88, 101, 216, 0.10)' },
        Card: { borderRadiusLG: 14 },
      },
    }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
);
