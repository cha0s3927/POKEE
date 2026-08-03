/**
 * WhatsApp Bot 入口 — 多用户 Baileys socket 管理器
 * 每个 web 用户维护独立的 Baileys 连接 + auth_info_baileys/{user_id} 凭证目录
 * 用法: node --env-file=.env bot.mjs
 */

import { createServer } from 'http';
import { rm, readdir } from 'fs/promises';
import { existsSync, mkdirSync } from 'fs';
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import { HttpsProxyAgent } from 'https-proxy-agent';
import { handleMessage } from './adapter.mjs';

const PUSH_PORT = parseInt(process.env.WHATSAPP_PUSH_PORT || '8767');
const PROXY_URL = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || '';
const MAX_BOTS = 50;

// ── 多用户 session 存储 ───────────────────────────────────
/** @type {Map<string, { sock: any, qr: string, pairingCode: string, connected: boolean, authDir: string, state: any, saveCreds: Function, connecting: boolean, lastActivity: number }>} */
const bots = new Map();

function getOrCreateSession(userId) {
  if (!bots.has(userId)) {
    if (!existsSync('auth_info_baileys')) {
      mkdirSync('auth_info_baileys', { recursive: true });
    }
    bots.set(userId, {
      sock: null,
      qr: '',
      pairingCode: '',
      connected: false,
      authDir: `auth_info_baileys/${userId}`,
      state: null,
      saveCreds: null,
      connecting: false,
      lastActivity: Date.now(),
    });
  }
  return bots.get(userId);
}

// ── Rabalance Baileys socket per user ───────────────────
async function connectForUser(userId) {
  const session = getOrCreateSession(userId);
  if (session.connecting) return;
  if (bots.size > MAX_BOTS && !session.connected) {
    console.log(`[wa] MAX_BOTS (${MAX_BOTS}) reached, rejecting new connection for ${userId}`);
    return;
  }
  session.connecting = true;

  const { state, saveCreds } = await useMultiFileAuthState(session.authDir);
  session.state = state;
  session.saveCreds = saveCreds;

  function makeSocketOptions() {
    const opts = {
      auth: session.state,
      browser: ['Ubuntu', 'Chrome', '22.04.4'],
      qrTimeout: 120000,
      defaultQueryTimeoutMs: 120000,
    };
    if (PROXY_URL) {
      opts.agent = new HttpsProxyAgent(PROXY_URL);
      console.log(`[wa] ${userId}: using proxy ${PROXY_URL}`);
    }
    return opts;
  }

  async function startSocket() {
    session.sock = makeWASocket(makeSocketOptions());

    session.sock.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        session.qr = qr;
        session.pairingCode = '';
        console.log(`[wa] ${userId}: QR generated`);
      }

      if (connection === 'close') {
        session.connected = false;
        session.qr = '';
        session.pairingCode = '';
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        const loggedOut = statusCode === DisconnectReason.loggedOut;
        console.log(`[wa] ${userId}: connection closed (code=${statusCode}), loggedOut=${loggedOut}`);

        if (loggedOut) {
          console.log(`[wa] ${userId}: logged out, clearing auth and restarting...`);
          await rm(session.authDir, { recursive: true, force: true });
          session.connecting = false;
          await new Promise(r => setTimeout(r, 5000));
          connectForUser(userId);
        } else {
          startSocket();
        }
      } else if (connection === 'open') {
        session.connected = true;
        session.qr = '';
        session.pairingCode = '';
        session.connecting = false;
        session.lastActivity = Date.now();
        console.log(`[wa] ${userId}: connected to WhatsApp`);
      }
    });

    session.sock.ev.on('messages.upsert', async ({ messages, type }) => {
      console.log(`[wa] ${userId}: messages.upsert type=${type} count=${messages.length}`);
      for (const msg of messages) {
        console.log(`[wa] ${userId}: msg key=${JSON.stringify(msg.key)}, hasMessage=${!!msg.message}, fromMe=${msg.key?.fromMe}`);
        if (!msg.message) continue;
        const botJid = session.sock.user?.id?.replace(/:\d+/, '');
        await handleMessage(session.sock, msg, botJid);
        session.lastActivity = Date.now();
      }
    });

    session.sock.ev.on('creds.update', session.saveCreds);
  }

  await startSocket();
}

// ── HTTP server ──
function jsonReply(res, code, data) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

function parseUrl(req) {
  return new URL(req.url, 'http://localhost');
}

const pushServer = createServer(async (req, res) => {
  const url = parseUrl(req);

  // GET /qr?user_id=xxx — 返回当前 QR / pairing code
  if (req.method === 'GET' && url.pathname === '/qr') {
    const userId = url.searchParams.get('user_id') || '';
    if (!userId) {
      return jsonReply(res, 400, { error: 'user_id query parameter is required' });
    }
    const session = getOrCreateSession(userId);
    if (!session.sock && !session.connecting) {
      connectForUser(userId); // fire-and-forget，不阻塞
    }
    return jsonReply(res, 200, {
      qr: session.qr,
      pairing_code: session.pairingCode,
      connected: session.connected,
    });
  }

  // GET /status?user_id=xxx — 连接状态
  if (req.method === 'GET' && url.pathname === '/status') {
    const userId = url.searchParams.get('user_id') || '';
    const session = bots.get(userId);
    return jsonReply(res, 200, { connected: session?.connected || false });
  }

  // POST /pairing — 请求配对码
  if (req.method === 'POST' && url.pathname === '/pairing') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const { phone, user_id } = JSON.parse(body);
        if (!phone || !user_id) {
          return jsonReply(res, 400, { error: 'phone and user_id required' });
        }
        const session = bots.get(user_id);
        if (!session || !session.sock) {
          return jsonReply(res, 400, { error: 'socket not ready for this user' });
        }
        const code = await session.sock.requestPairingCode(phone);
        session.pairingCode = code;
        session.qr = '';
        console.log(`[pairing] ${user_id}: code=${code} phone=${phone}`);
        jsonReply(res, 200, { pairing_code: code });
      } catch (e) {
        console.error('[pairing] error:', e.message);
        jsonReply(res, 500, { error: e.message });
      }
    });
    return;
  }

  // POST /push — Python scheduler 推送通知
  if (req.method === 'POST' && url.pathname === '/push') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', async () => {
      try {
        const data = JSON.parse(body);
        const jid = data.user_id.replace('whatsapp:', '');
        const text = `叮咚！你的提醒到啦 \u{1F514}\n\n${data.task}\n时间：${data.run_at}`;

        // 优先用 web_user_id 找对应 socket
        let sock = null;
        const webUserId = data.web_user_id;
        if (webUserId) {
          const session = bots.get(webUserId);
          if (session?.connected && session?.sock) {
            sock = session.sock;
          }
        }
        // Fallback: 遍历所有已连接 socket
        if (!sock) {
          for (const [, s] of bots) {
            if (s.connected && s.sock) {
              sock = s.sock;
              break;
            }
          }
        }
        if (!sock) {
          return jsonReply(res, 503, { error: 'no connected WhatsApp socket available' });
        }
        await sock.sendMessage(jid, { text });
        console.log(`[push] sent to ${jid}`);
        jsonReply(res, 200, { ok: true });
      } catch (e) {
        console.error('[push] error:', e.message);
        jsonReply(res, 500, { error: e.message });
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

// ── 启动: 恢复所有已有 per-user bot ─────────────────────
async function restoreAllBots() {
  try {
    const entries = await readdir('auth_info_baileys', { withFileTypes: true });
    let count = 0;
    for (const entry of entries) {
      if (entry.isDirectory()) {
        const userId = entry.name;
        console.log(`[wa] restoring bot for user: ${userId}`);
        connectForUser(userId);
        count++;
      }
    }
    console.log(`[wa] restored ${count} bot(s)`);
  } catch (e) {
    console.log('[wa] no existing bots to restore');
  }
}

await restoreAllBots();

// ── 定时清理过期 session ──────────────────────────────
setInterval(() => {
  const now = Date.now();
  for (const [userId, session] of bots) {
    if (!session.connected && !session.connecting &&
        (now - session.lastActivity) > 3600000) {
      try { session.sock?.end(); } catch (e) { /* ignore */ }
      bots.delete(userId);
      console.log(`[wa] cleaned up stale session: ${userId}`);
    }
  }
}, 900000); // 每 15 分钟

// ── 优雅退出 ──
process.on('SIGINT', () => {
  console.log('[wa] shutting down...');
  for (const [, session] of bots) {
    try { session.sock?.end(); } catch (e) { /* ignore */ }
  }
  pushServer.close();
  process.exit(0);
});
