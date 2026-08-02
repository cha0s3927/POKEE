# ── Stage 0: 构建参数 ──
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# ── Stage 1: Node.js 依赖 ──
FROM node:22-slim AS node-deps
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

WORKDIR /build/wechat
COPY wechat-adapter/package.json wechat-adapter/package-lock.json* ./
RUN npm ci --omit=dev 2>/dev/null || npm install --omit=dev

WORKDIR /build/whatsapp
COPY whatsapp-adapter/package.json whatsapp-adapter/package-lock.json* ./
RUN npm ci --omit=dev 2>/dev/null || npm install --omit=dev

# ── Stage 2: 最终镜像 ──
FROM python:3.12-slim
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} HTTPS_PROXY=${HTTPS_PROXY} NO_PROXY=${NO_PROXY}

# 安装 Node.js 22 + supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates supervisor && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python 依赖 ──
COPY scheduler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Node.js 依赖（从 stage 1 复制） ──
COPY --from=node-deps /build/wechat/node_modules wechat-adapter/node_modules/
COPY --from=node-deps /build/whatsapp/node_modules whatsapp-adapter/node_modules/

# 删除 qrcode-terminal，强制 SDK 打印 QR URL（以便 Web 界面拦截）
RUN rm -rf /app/wechat-adapter/node_modules/qrcode-terminal 2>/dev/null || true

# ── Python 源码 ──
COPY scheduler/main.py .
COPY scheduler/agent.py .
COPY scheduler/auth.py .
COPY scheduler/config.yaml .
COPY scheduler/channels/ channels/
COPY scheduler/static/ static/

# ── Node.js 适配器源码 ──
COPY wechat-adapter/bot.mjs wechat-adapter/
COPY wechat-adapter/adapter.mjs wechat-adapter/
COPY whatsapp-adapter/bot.mjs whatsapp-adapter/
COPY whatsapp-adapter/adapter.mjs whatsapp-adapter/

# ── 数据目录 ──
RUN mkdir -p /app/data \
    /app/whatsapp-adapter/auth_info_baileys \
    /root/.openclaw/openclaw-weixin/accounts

# ── Supervisor 配置 ──
COPY supervisord.conf /etc/supervisord.conf

EXPOSE 8000

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
