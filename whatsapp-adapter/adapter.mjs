/**
 * WhatsApp → Reminder Agent 适配器
 * 将 Baileys 收到的消息转发到 Python FastAPI
 */

const API_BASE = process.env.API_BASE || 'http://localhost:8000';
const WHATSAPP_SECRET = process.env.WHATSAPP_SECRET || 'whatsapp-secret-change-me';

// 防回环：bot 发送后 WhatsApp 会回显到 messages.upsert，用此 set 过滤
const pendingEchoes = new Set();

export async function handleMessage(sock, msg, botJid) {
  const text = msg.message?.conversation
    || msg.message?.extendedTextMessage?.text
    || msg.message?.imageMessage?.caption;
  if (!text || !text.trim()) return;

  // 跳过 bot 回复其他人的回显（self-chat 的 JID 是 @lid 格式，放行）
  if (msg.key.fromMe && msg.key.remoteJid !== botJid && !msg.key.remoteJid?.endsWith('@lid')) return;
  // 跳过 bot 回复自己的回显
  if (pendingEchoes.has(text.trim())) {
    pendingEchoes.delete(text.trim());
    return;
  }

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
    // 记录刚发的回复，防止回显触发回环
    if (msg.key.fromMe) pendingEchoes.add(data.reply);
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
