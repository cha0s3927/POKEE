/**
 * WhatsApp Bot 入口 — QR 扫码 / 配对码登录 + WebSocket + push HTTP server
 * 用法: node --env-file=.env bot.mjs
 */

import { createServer } from 'http';
import { rm } from 'fs/promises';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { HttpsProxyAgent } from 'https-proxy-agent';
import { handleMessage } from './adapter.mjs';

const PUSH_PORT = parseInt(process.env.WHATSAPP_PUSH_PORT || '8767');
const PROXY_URL = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || '';

let currentQr = '';
let currentPairingCode = '';
let waConnected = false;
let sock = null;

// ── HTTP server ──
const pushServer = createServer(async (req, res) => {
  // GET /qr — 返回当前 QR / pairing code
  if (req.method === 'GET' && req.url === '/qr') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ qr: currentQr, pairing_code: currentPairingCode, connected: waConnected }));
    return;
  }

  // GET /status — 连接状态
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ connected: waConnected }));
    return;
  }

  // POST /pairing — 请求配对码
  if (req.method === 'POST' && req.url === '/pairing') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const { phone } = JSON.parse(body);
        if (!phone || !sock) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'phone required and socket must be ready' }));
          return;
        }
        const code = await sock.requestPairingCode(phone);
        currentPairingCode = code;
        currentQr = ''; // hide QR when pairing code is available
        console.log(`[pairing] code: ${code} for phone: ${phone}`);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ pairing_code: code }));
      } catch (e) {
        console.error('[pairing] error:', e.message);
        res.writeHead(500);
        res.end(JSON.stringify({ error: e.message }));
      }
    });
    return;
  }

  // POST /push — Python scheduler 回调推送通知
  if (req.method === 'POST' && req.url === '/push') {
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
    return;
  }

  res.writeHead(404);
  res.end();
});

pushServer.listen(PUSH_PORT, '127.0.0.1', () => {
  console.log(`[push-server] listening on :${PUSH_PORT}`);
});

// ── Baileys WebSocket ──
async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

  async function startSocket() {
    const sockOpts = {
      auth: state,
      browser: ['Ubuntu', 'Chrome', '22.04.4'],
      qrTimeout: 120000,
      defaultQueryTimeoutMs: 120000,
    };
    if (PROXY_URL) {
      sockOpts.agent = new HttpsProxyAgent(PROXY_URL);
      console.log(`[wa] using proxy: ${PROXY_URL}`);
    }
    sock = makeWASocket(sockOpts);

    sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        currentQr = qr;
        currentPairingCode = '';
        console.log('\n📱 QR 码已生成，可通过 Web 界面扫码连接');
      }

      if (connection === 'close') {
        waConnected = false;
        currentQr = '';
        currentPairingCode = '';
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const loggedOut = statusCode === DisconnectReason.loggedOut;
        console.log(`[wa] connection closed (code: ${statusCode}), loggedOut: ${loggedOut}`);

        if (loggedOut) {
          console.log('[wa] logged out, clearing auth state and restarting in 5s...');
          await rm('auth_info_baileys', { recursive: true, force: true });
          await new Promise(r => setTimeout(r, 5000));
          connect();
        } else {
          startSocket();
        }
      } else if (connection === 'open') {
        waConnected = true;
        currentQr = '';
        currentPairingCode = '';
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
}

await connect();

// ── 优雅退出 ──
process.on('SIGINT', () => {
  console.log('[wa] shutting down...');
  pushServer.close();
  process.exit(0);
});
