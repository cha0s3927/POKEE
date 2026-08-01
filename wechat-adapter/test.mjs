/**
 * 离线测试 — 绕过微信 iLink，直接测 adapter → Python agent 链路
 * 用法: node test.mjs
 */

import { agent } from './adapter.mjs';

const tests = [
  { text: '帮我创建一个2分钟后的提醒，内容是"测试微信提醒"', label: '创建提醒' },
  { text: '查看我所有的提醒', label: '查询列表' },
  { text: '现在几点', label: '获取时间' },
];

for (const { text, label } of tests) {
  console.log(`\n=== ${label} ===`);
  console.log(`> ${text}`);

  const response = await agent.chat({
    conversationId: 'test-user-001',
    text,
  });

  console.log(`< ${response.text}`);
}

// 清空会话
await agent.clearSession('test-user-001');
console.log('\n[test] 会话已重置');
