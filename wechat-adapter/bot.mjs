/**
 * WeChat Bot 入口 — 扫码登录 + 启动长轮询
 * 用法: node bot.mjs
 */

import { login, start } from 'weixin-agent-sdk';
import { agent } from './adapter.mjs';

const accountId = await login();
console.log(`[bot] 已登录: ${accountId}`);

const bot = start(agent);
console.log('[bot] 正在监听微信消息...');

process.on('SIGINT', () => {
  console.log('[bot] 正在关闭...');
  bot.stop();
  process.exit(0);
});
