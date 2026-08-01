/**
 * WeChat Bot 入口 — 扫码登录 + 长轮询 + 推送 HTTP server
 * 用法: node bot.mjs
 */

import { createServer } from 'http';
import { login, start } from 'weixin-agent-sdk';
import { agent } from './adapter.mjs';

const PUSH_PORT = parseInt(process.env.WECHAT_PUSH_PORT || '8765');

const accountId = await login();
console.log(`[bot] 已登录: ${accountId}`);

const bot = start(agent);
console.log('[bot] 正在监听微信消息...');

// ── Push HTTP server: Python scheduler 回调此端口推送通知 ──
const server = createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/push') {
    res.writeHead(404);
    res.end();
    return;
  }

  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', async () => {
    try {
      const data = JSON.parse(body);
      const text = `叮咚！你的提醒到啦 🔔\n\n${data.task}\n时间：${data.run_at}`;
      await bot.sendMessage(text);
      console.log(`[push] sent: ${data.task}`);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    } catch (e) {
      console.error('[push] error:', e.message);
      res.writeHead(500);
      res.end(JSON.stringify({ error: e.message }));
    }
  });
});

server.listen(PUSH_PORT, '127.0.0.1', () => {
  console.log(`[push-server] listening on :${PUSH_PORT}`);
});

// ── 优雅退出 ──
process.on('SIGINT', () => {
  console.log('[bot] 正在关闭...');
  server.close();
  process.exit(0);
});
