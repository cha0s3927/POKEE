/**
 * WhatsApp → Reminder Agent 适配器
 * 将 Baileys 收到的消息转发到 Python FastAPI
 */

const API_BASE = process.env.API_BASE || 'http://localhost:8000';
const WHATSAPP_SECRET = process.env.WHATSAPP_SECRET || 'whatsapp-secret-change-me';

export async function handleMessage(sock, msg, botJid) {
  const text = msg.message?.conversation
    || msg.message?.extendedTextMessage?.text
    || msg.message?.imageMessage?.caption;
  if (!text || !text.trim()) return;

  // 跳过 @lid 内部设备同步消息（非真实用户消息）
  if (msg.key.remoteJid?.endsWith('@lid')) return;
  // 跳过所有 bot 自己发出的消息回显
  if (msg.key.fromMe) return;

  const jid = msg.key.remoteJid;

  try {
    const resp = await fetch(`${API_BASE}/api/whatsapp/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        conversation_id: jid,
        secret: WHATSAPP_SECRET,
      }),
    });

    if (!resp.ok) {
      console.error(`[adapter] API error ${resp.status}: ${await resp.text()}`);
      await sock.sendMessage(jid, { text: '抱歉，服务暂时不可用，请稍后再试。' });
      return;
    }

    const data = await resp.json();
    await sock.sendMessage(jid, { text: data.reply });
    console.log(`[adapter] replied to ${jid}: ${data.reply.substring(0, 40)}...`);
  } catch (e) {
    console.error('[adapter] error:', e.message);
  }
}

export async function clearSession(conversationId) {
  try {
    await fetch(`${API_BASE}/api/whatsapp/reset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId,
        secret: WHATSAPP_SECRET,
      }),
    });
  } catch (e) {
    console.error('[adapter] clearSession error:', e.message);
  }
}
