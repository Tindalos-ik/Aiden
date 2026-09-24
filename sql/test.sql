-- Aiden local demo data for MySQL 8.4, after Alembic upgrade head.
-- Run against the disposable local aiden database. Re-running leaves these fixed rows unchanged.
-- This covers all 15 application tables; alembic_version is migration metadata and is untouched.
SET NAMES utf8mb4;
USE aiden;
START TRANSACTION;

-- Login: sql.customer@aiden.test / aiden123. The staff row is for relationships only.
INSERT INTO users (id, name, email, role, password_salt, password_hash, created_at, updated_at)
VALUES ('11111111-1111-4111-8111-000000000001', 'SQL测试用户', 'sql.customer@aiden.test', 'user',
        '0123456789abcdef0123456789abcdef',
        'ea16cb32ac721dac140257b6f218e9fd1a24691fbd48f7aae2e697178dfe4e68',
        '2026-09-18 08:00:00', '2026-09-18 08:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO users (id, name, email, role, created_at, updated_at)
VALUES ('11111111-1111-4111-8111-000000000002', 'SQL测试客服', 'sql.staff@aiden.test', 'staff',
        '2026-09-18 08:00:00', '2026-09-18 08:00:00')
ON DUPLICATE KEY UPDATE id = id;

-- Expired on purpose: it exercises login_sessions without creating a reusable live login token.
INSERT INTO login_sessions (token_hash, user_id, expires_at, created_at)
VALUES (SHA2('aiden-expired-sql-seed-session', 256), '11111111-1111-4111-8111-000000000001',
        '2020-01-01 00:00:00', '2019-12-31 00:00:00')
ON DUPLICATE KEY UPDATE token_hash = token_hash;

INSERT INTO products (id, name, category, description, is_active, created_at, updated_at)
VALUES ('22222222-2222-4222-8222-000000000001', 'QuietPods Pro 降噪耳机', '数码',
        '【测试商品】无线降噪耳机。', 1, '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO products (id, name, category, description, is_active, created_at, updated_at)
VALUES ('22222222-2222-4222-8222-000000000002', '日光徒步双肩包', '户外',
        '【测试商品】20L 轻量双肩包。', 1, '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO product_skus (id, product_id, sku_code, name, specification, price, stock_quantity, is_active, created_at, updated_at)
VALUES ('22222222-2222-4222-8222-000000000011', '22222222-2222-4222-8222-000000000001',
        'TEST-QP-BLACK', '曜石黑', '{"color":"black","version":"pro"}', 269.00, 20, 1,
        '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO product_skus (id, product_id, sku_code, name, specification, price, stock_quantity, is_active, created_at, updated_at)
VALUES ('22222222-2222-4222-8222-000000000012', '22222222-2222-4222-8222-000000000002',
        'TEST-BAG-GREEN', '森林绿 20L', '{"color":"green","capacity":"20L"}', 329.00, 12, 1,
        '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO orders (id, order_no, user_id, total_amount, currency, status, ordered_at, created_at, updated_at)
VALUES ('33333333-3333-4333-8333-000000000001', 'TEST-20260922-001',
        '11111111-1111-4111-8111-000000000001', 269.00, 'CNY', 'shipped',
        '2026-09-22 10:00:00', '2026-09-22 10:00:00', '2026-09-24 08:15:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO orders (id, order_no, user_id, total_amount, currency, status, ordered_at, created_at, updated_at)
VALUES ('33333333-3333-4333-8333-000000000002', 'TEST-20260918-002',
        '11111111-1111-4111-8111-000000000001', 329.00, 'CNY', 'delivered',
        '2026-09-18 11:30:00', '2026-09-18 11:30:00', '2026-09-20 14:30:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO order_items (id, order_id, sku_id, product_name_snapshot, sku_code_snapshot,
                         sku_name_snapshot, specification_snapshot, quantity, unit_price, line_total)
VALUES ('33333333-3333-4333-8333-000000000011', '33333333-3333-4333-8333-000000000001',
        '22222222-2222-4222-8222-000000000011', 'QuietPods Pro 降噪耳机', 'TEST-QP-BLACK',
        '曜石黑', '{"color":"black","version":"pro"}', 1, 269.00, 269.00)
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO order_items (id, order_id, sku_id, product_name_snapshot, sku_code_snapshot,
                         sku_name_snapshot, specification_snapshot, quantity, unit_price, line_total)
VALUES ('33333333-3333-4333-8333-000000000012', '33333333-3333-4333-8333-000000000002',
        '22222222-2222-4222-8222-000000000012', '日光徒步双肩包', 'TEST-BAG-GREEN',
        '森林绿 20L', '{"color":"green","capacity":"20L"}', 1, 329.00, 329.00)
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO shipments (id, order_id, shipment_no, carrier_code, carrier_name, tracking_no,
                       status, shipped_at, created_at, updated_at)
VALUES ('44444444-4444-4444-8444-000000000001', '33333333-3333-4333-8333-000000000001',
        'TEST-PKG-001', 'SF', '顺丰速运', 'SFTEST20260922001', 'in_transit',
        '2026-09-23 09:00:00', '2026-09-23 09:00:00', '2026-09-24 08:15:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO shipments (id, order_id, shipment_no, carrier_code, carrier_name, tracking_no,
                       status, shipped_at, delivered_at, created_at, updated_at)
VALUES ('44444444-4444-4444-8444-000000000002', '33333333-3333-4333-8333-000000000002',
        'TEST-PKG-002', 'JD', '京东物流', 'JDTEST20260918002', 'delivered',
        '2026-09-19 09:00:00', '2026-09-20 14:30:00', '2026-09-19 09:00:00', '2026-09-20 14:30:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO tracking_events (id, shipment_id, event_code, status, description, location, occurred_at)
VALUES ('44444444-4444-4444-8444-000000000011', '44444444-4444-4444-8444-000000000001',
        'PICKED_UP', 'picked_up', '快件已揽收', '上海', '2026-09-23 09:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO tracking_events (id, shipment_id, event_code, status, description, location, occurred_at)
VALUES ('44444444-4444-4444-8444-000000000012', '44444444-4444-4444-8444-000000000001',
        'ARRIVED', 'in_transit', '已到达杭州转运中心', '杭州', '2026-09-24 08:15:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO tracking_events (id, shipment_id, event_code, status, description, location, occurred_at)
VALUES ('44444444-4444-4444-8444-000000000013', '44444444-4444-4444-8444-000000000002',
        'SHIPPED', 'in_transit', '包裹已发出', '杭州', '2026-09-19 09:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO tracking_events (id, shipment_id, event_code, status, description, location, occurred_at)
VALUES ('44444444-4444-4444-8444-000000000014', '44444444-4444-4444-8444-000000000002',
        'DELIVERED', 'delivered', '已由本人签收', '杭州', '2026-09-20 14:30:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO after_sale_requests (id, request_no, user_id, order_id, order_item_id, request_type,
                                 reason, status, requested_amount, created_at, updated_at)
VALUES ('55555555-5555-4555-8555-000000000001', 'TEST-AS-20260921-001',
        '11111111-1111-4111-8111-000000000001', '33333333-3333-4333-8333-000000000002',
        '33333333-3333-4333-8333-000000000012', 'return',
        '【测试申请】双肩包不合适，申请退货', 'pending', 329.00,
        '2026-09-21 10:00:00', '2026-09-21 10:00:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO policies (id, policy_key, name, version, content, effective_from, is_active, created_at, updated_at)
VALUES ('66666666-6666-4666-8666-000000000001', 'test_return_policy', '测试退货政策', '1.0',
        '【测试政策】签收后 7 天内可以提出退货申请；是否符合条件需审核订单与商品状态。',
        '2026-01-01 00:00:00', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO policies (id, policy_key, name, version, content, effective_from, is_active, created_at, updated_at)
VALUES ('66666666-6666-4666-8666-000000000002', 'test_shipping_insurance', '测试运费险说明', '1.0',
        '【测试政策】本条仅用于数据库联调，不代表真实商城的运费险承诺。',
        '2026-01-01 00:00:00', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO faq (id, category, question, answer, is_active, created_at, updated_at)
VALUES ('77777777-7777-4777-8777-000000000001', '物流', '发货后在哪里查看物流？',
        '【测试 FAQ】可通过订单详情查看包裹和物流节点。', 1,
        '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO faq (id, category, question, answer, is_active, created_at, updated_at)
VALUES ('77777777-7777-4777-8777-000000000002', '售后', '退货申请提交后在哪里看进度？',
        '【测试 FAQ】可通过售后申请编号查看处理状态。', 1,
        '2026-09-01 00:00:00', '2026-09-01 00:00:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO conversations (id, user_id, subject, status, started_at, last_message_preview, created_at, updated_at)
VALUES ('88888888-8888-4888-8888-000000000001', '11111111-1111-4111-8111-000000000001',
        'SQL 测试会话', 'bot', '2026-09-24 09:00:00', '你好，这是预置的测试会话。',
        '2026-09-24 09:00:00', '2026-09-24 09:01:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO conversations (id, user_id, subject, status, assigned_staff_id, started_at,
                           last_message_preview, created_at, updated_at)
VALUES ('88888888-8888-4888-8888-000000000002', '11111111-1111-4111-8111-000000000001',
        '签收异常记录', 'staff', '11111111-1111-4111-8111-000000000002',
        '2026-09-21 09:00:00', '【预置测试消息】工单已记录。',
        '2026-09-21 09:00:00', '2026-09-21 09:10:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO messages (id, conversation_id, sender_role, content, status, client_message_id, created_at)
VALUES ('99999999-9999-4999-8999-000000000001', '88888888-8888-4888-8888-000000000001',
        'user', '你好', 'complete', 'sql-seed-hello', '2026-09-24 09:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO messages (id, conversation_id, sender_role, content, status, created_at)
VALUES ('99999999-9999-4999-8999-000000000002', '88888888-8888-4888-8888-000000000001',
        'assistant', '【预置测试消息】你好，这是用于检查消息表的示例。', 'complete', '2026-09-24 09:01:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO messages (id, conversation_id, sender_role, content, status, client_message_id, created_at)
VALUES ('99999999-9999-4999-8999-000000000003', '88888888-8888-4888-8888-000000000002',
        'user', '包裹显示签收，但我没收到。', 'complete', 'sql-seed-complaint', '2026-09-21 09:00:00')
ON DUPLICATE KEY UPDATE id = id;
INSERT INTO messages (id, conversation_id, sender_role, content, status, created_at)
VALUES ('99999999-9999-4999-8999-000000000004', '88888888-8888-4888-8888-000000000002',
        'staff', '【预置测试消息】工单已记录，等待进一步核实。', 'complete', '2026-09-21 09:10:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO tickets (id, ticket_no, conversation_id, user_id, issue_type, description, status,
                     assigned_staff_id, created_at, updated_at)
VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-000000000001', 'TEST-TICKET-001',
        '88888888-8888-4888-8888-000000000002', '11111111-1111-4111-8111-000000000001',
        'delivery_dispute', '【测试工单】物流显示签收，但用户反馈未收到。', 'in_progress',
        '11111111-1111-4111-8111-000000000002', '2026-09-21 09:05:00', '2026-09-21 09:05:00')
ON DUPLICATE KEY UPDATE id = id;

INSERT INTO unanswered_questions (id, conversation_id, question, confidence, review_status, created_at)
VALUES ('bbbbbbbb-bbbb-4bbb-8bbb-000000000001', '88888888-8888-4888-8888-000000000001',
        '退款到账能否加急？', 0.1500, 'pending', '2026-09-24 09:02:00')
ON DUPLICATE KEY UPDATE id = id;

COMMIT;
