/**
 * WeChat Bot 入口 — 扫码登录 + 长轮询 + 推送 HTTP server
 * 用法: node bot.mjs
 */

import { createServer } from 'http';
import { login, start } from 'weixin-agent-sdk';
import { agent } from './adapter.mjs';

const PUSH_PORT = parseInt(process.env.WECHAT_PUSH_PORT || '8765');

let wechatQrUrl = '';
let wechatQrUpdatedAt = 0;  // 二维码生成时间戳 (ms)
let wechatReady = false;
let bot = null;

// ── HTTP server: QR 码 + push 通知 ──
const server = createServer(async (req, res) => {
  // GET /qr — 返回当前 QR 数据
  if (req.method === 'GET' && req.url === '/qr') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      qr_url: wechatQrUrl,
      connected: wechatReady,
      qr_updated_at: wechatQrUpdatedAt,  // 二维码生成时间，用于判断新鲜度
    }));
    return;
  }

  // GET /status — 连接状态
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ connected: wechatReady }));
    return;
  }

  // POST /push — Python scheduler 回调推送通知
  if (req.method === 'POST' && req.url === '/push') {
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
    return;
  }

  res.writeHead(404);
  res.end();
});

// 先启动 HTTP server，这样 login() 阻塞等待扫码期间 Web 界面能拿到 QR
server.listen(PUSH_PORT, '127.0.0.1', () => {
  console.log(`[push-server] listening on :${PUSH_PORT}`);
});

// ── 扫码登录（失败自动重试）──
let accountId;
while (true) {
  try {
    accountId = await login({
      log: (msg) => {
        console.log(msg);
        // 拦截所有 QR 链接（初始格式 + 刷新后的格式）
        let m = msg.match(/二维码链接:\s*(https:\/\/\S+)/);
        if (!m) m = msg.match(/(https:\/\/liteapp\.weixin\.qq\.com\/\S+)/);
        if (!m) m = msg.match(/(https:\/\/\S*qrcode\S*)/i);
        if (m) {
          wechatQrUrl = m[1];
          wechatQrUpdatedAt = Date.now();
          console.log('[bot] QR URL captured');
        }
      },
    });
    break; // 登录成功，跳出重试循环
  } catch (e) {
    console.error('[bot] 登录失败:', e.message);
    console.error('[bot] 5 秒后重试...');
    wechatQrUrl = '';
    wechatQrUpdatedAt = 0;
    await new Promise(r => setTimeout(r, 5000));
  }
}

console.log(`[bot] 已登录: ${accountId}`);

bot = start(agent);
wechatReady = true;
wechatQrUrl = '';
console.log('[bot] 正在监听微信消息...');

// ── 优雅退出 ──
process.on('SIGINT', () => {
  console.log('[bot] 正在关闭...');
  server.close();
  process.exit(0);
});
