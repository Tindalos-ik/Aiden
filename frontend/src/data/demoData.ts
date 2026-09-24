import type { DemoAccount, OrderCard } from '../types';

export const demoAccounts: DemoAccount[] = [
  { id: 'user-maya', name: '林沐言', role: 'user', email: 'maya@aiden.demo', password: 'aiden123', description: '普通用户 · 有物流中与已签收订单', color: '#8172e9' },
  { id: 'user-chen', name: '陈屿', role: 'user', email: 'chen@aiden.demo', password: 'aiden123', description: '普通用户 · 有退款中与待发货订单', color: '#239b83' },
  { id: 'staff-lin', name: '林客服', role: 'staff', email: 'staff@aiden.demo', password: 'aiden123', description: '人工客服 · 可接入并回复会话', color: '#3278cb' },
];

export const demoOrdersByUser: Record<string, OrderCard[]> = {
  'user-maya': [
    { orderId: 'AD-2026-0818-0241', product: 'QuietPods Pro 降噪耳机', amount: 269, status: '物流中', logistics: '已到达杭州转运中心，预计明天送达', carrier: '顺丰速运', updatedAt: '今天 09:42' },
    { orderId: 'AD-2026-0812-1097', product: 'Studio 87 机械键盘', amount: 399, status: '已签收', logistics: '已由本人签收', carrier: '京东物流', updatedAt: '9 月 20 日 14:16' },
  ],
  'user-chen': [
    { orderId: 'AD-2026-0919-0548', product: '日光徒步双肩包', amount: 329, status: '退款中', logistics: '商家已收到退货，正在处理退款', carrier: '中通快递', updatedAt: '今天 08:25' },
    { orderId: 'AD-2026-0921-1130', product: '陶瓷手冲咖啡套装', amount: 188, status: '物流中', logistics: '包裹已揽收，等待发出', carrier: '圆通速递', updatedAt: '昨天 17:08' },
  ],
};
