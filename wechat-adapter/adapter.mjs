/**
 * WeChat → Reminder Agent 适配器
 * 实现 weixin-agent-sdk 的 Agent 接口，将请求转发到 Python FastAPI
 */

const API_BASE = process.env.API_BASE || 'http://localhost:8000';
const WECHAT_SECRET = process.env.WECHAT_SECRET || 'wechat-secret-change-me';

export const agent = {
  async chat(request) {
    const resp = await fetch(`${API_BASE}/api/wechat/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: request.text,
        conversation_id: request.conversationId,
        secret: WECHAT_SECRET,
      }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      console.error(`[adapter] API error ${resp.status}: ${err}`);
      return { text: '抱歉，服务暂时不可用，请稍后再试。' };
    }

    const data = await resp.json();
    return { text: data.reply };
  },

  async clearSession(conversationId) {
    try {
      await fetch(`${API_BASE}/api/wechat/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          secret: WECHAT_SECRET,
        }),
      });
    } catch (e) {
      console.error('[adapter] clearSession error:', e.message);
    }
  },
};
