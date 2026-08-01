/**
 * WhatsApp Bot 入口 — 扫码登录 + WebSocket + push HTTP server
 * 用法: node --env-file=.env bot.mjs
 */

import { createServer } from 'http';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { HttpsProxyAgent } from 'https-proxy-agent';
import { handleMessage } from './adapter.mjs';

const PUSH_PORT = parseInt(process.env.WHATSAPP_PUSH_PORT || '8767');
const PROXY_URL = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || '';

// ── Push HTTP server: Python scheduler 回调此端口推送通知 ──
const pushServer = createServer(async (req, res) => {
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
      const jid = data.user_id.replace('whatsapp:', '');
      const text = `叮咚！你的提醒到啦 \u{1F514}\n\n${data.task}\n时间：${data.run_at}`;
      await sock.sendMessage(jid, { text });
      console.log(`[push] sent to ${jid}: ${data.task}`);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    } catch (e) {
      console.error('[push] error:', e.message);
      res.writeHead(500);
      res.end(JSON.stringify({ error: e.message }));
    }
  });
});

pushServer.listen(PUSH_PORT, '127.0.0.1', () => {
  console.log(`[push-server] listening on :${PUSH_PORT}`);
});

// ── Baileys WebSocket ──
const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

let sock;

async function startSocket() {
  const sockOpts = { auth: state };
  if (PROXY_URL) {
    sockOpts.agent = new HttpsProxyAgent(PROXY_URL);
    console.log(`[wa] using proxy: ${PROXY_URL}`);
  }
  sock = makeWASocket(sockOpts);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      const { default: qrcode } = await import('qrcode-terminal');
      console.log('\n📱 请用手机 WhatsApp 扫码（设置 → 已关联设备）：\n');
      qrcode.generate(qr, { small: true });
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(`[wa] connection closed (code: ${statusCode}), reconnect: ${shouldReconnect}`);
      if (shouldReconnect) {
        startSocket();
      }
    } else if (connection === 'open') {
      console.log('[wa] connected to WhatsApp');
    }
  });

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages) {
      console.log('[wa] msg:', JSON.stringify({ fromMe: msg.key.fromMe, remoteJid: msg.key.remoteJid, type: Object.keys(msg.message||{})[0] }));
      if (!msg.message) continue;
      const botJid = sock.user?.id?.replace(/:\d+/, '');
      await handleMessage(sock, msg, botJid);
    }
  });

  sock.ev.on('creds.update', saveCreds);
}

await startSocket();

// ── 优雅退出 ──
process.on('SIGINT', () => {
  console.log('[wa] shutting down...');
  pushServer.close();
  process.exit(0);
});
