# POKEE — 个人提醒助手

基于 LLM 的多平台提醒助手。通过微信、WhatsApp、飞书、LinkedIn 或 Web 界面用自然语言聊天，Agent 自动理解意图并设置提醒。无需记忆命令格式。

## 功能特性

- **自然语言交互** — 说"明天下午三点提醒我交报告"即可，不用学命令
- **多平台接入** — Web 界面 + 微信 + WhatsApp + 飞书 + LinkedIn，背后同一个 Agent
- **积分系统** — 每日登录奖励 +5 分，创建提醒 -0.1 分，有审计流水
- **人设系统** — 每个用户可自定义 Agent 说话风格
- **持久化调度** — APScheduler + SQLite，重启不丢
- **实时推送** — Web 端 SSE 推送，IM 平台通过适配器推送

## 架构

```
┌─────────────────────────────────────────────────┐
│                   Web 前端 (static)               │
└──────────────────────┬──────────────────────────┘
                       │ SSE / HTTP
┌──────────────────────▼──────────────────────────┐
│               FastAPI (main.py)                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │  routes/ │ │  agent   │ │    adapters/     │ │
│  │  认证     │ │  (LLM)   │ │  微信/WhatsApp   │ │
│  │  对话     │ │          │ │  飞书/LinkedIn   │ │
│  │  SSE     │ │ 工具调用◄─┼── 调度器           │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
└──────────────────────┬──────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
    ┌─────────┐ ┌──────────┐ ┌──────────┐
    │  微信   │ │ WhatsApp │ │   飞书   │  LinkedIn
    │ (iLink) │ │ (Baileys)│ │  (SDK)   │  (Cookie)
    │ Node.js │ │ Node.js  │ │  Python  │  Python
    └─────────┘ └──────────┘ └──────────┘
```

| 层 | 技术栈 | 职责 |
|-------|------|------|
| **API** | FastAPI + Uvicorn | HTTP/SSE 端点，静态文件服务 |
| **Agent** | OpenAI 兼容 LLM (DeepSeek) | 工具调用循环：意图识别 → 执行工具 → 回复 |
| **调度器** | APScheduler + SQLite | 定时任务存储，到期触发提醒 |
| **适配器** | Python + Node.js | 各平台 IM 协议处理，凭证隔离 |
| **认证** | Token 机制 | Web 登录，IM 绑定通过 `user_im_bindings` |

## 支持平台

| 平台 | 协议 | 技术 | 登录方式 |
|----------|----------|------------|----------|
| **Web UI** | HTTP + SSE | FastAPI + 原生 JS | 邮箱/密码 |
| **微信** | iLink Bot API | Node.js 适配器 (`weixin-agent-sdk`) | 扫码 |
| **WhatsApp** | Baileys WebSocket | Node.js 适配器 (`@whiskeysockets/baileys`) | 扫码 |
| **飞书** | Lark WebSocket | Python (`lark-oapi`) | 扫码 |
| **LinkedIn** | HTTP 轮询 | Python (`linkedin-api`) | Cookie |

## 快速开始

### 环境要求

- Python 3.9+
- Node.js 22+（微信和 WhatsApp 适配器需要）
- DeepSeek API Key（或其他 OpenAI 兼容接口）

### 本地开发

```bash
# 1. 克隆仓库
git clone <repo-url>
cd reminder-agent

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入 DEEPSEEK_API_KEY

# 3. 安装 Python 依赖
cd scheduler
pip install -r requirements.txt

# 4. 安装 Node 依赖（可选，仅微信/WhatsApp 需要）
cd ../wechat-adapter && npm install && cd ..
cd ../whatsapp-adapter && npm install && cd ..

# 5. 启动
cd scheduler
uvicorn main:app --reload --port 8000
```

打开 `http://localhost:8000`，注册账号即可开始使用。

### Docker 部署

```bash
cp .env.example .env
# 编辑 .env 填入 API Key

docker compose up -d --build
```

容器通过 supervisord 管理 3 个进程：FastAPI（端口 8000）、微信适配器、WhatsApp 适配器。

## 配置说明

所有配置通过环境变量（或 `.env` 文件）设置，完整模板见 `.env.example`。

### 必填

| 变量 | 说明 |
|----------|-------------|
| `DEEPSEEK_API_KEY` | LLM API Key（DeepSeek 或其他 OpenAI 兼容接口） |
| `DEEPSEEK_BASE_URL` | LLM API 地址（默认 `https://api.deepseek.com/v1`） |

### 各平台配置

| 变量 | 平台 | 说明 |
|----------|----------|-------------|
| `WECHAT_SECRET` | 微信 | 调度器与 Node 适配器之间的共享密钥 |
| `WHATSAPP_SECRET` | WhatsApp | 调度器与 Node 适配器之间的共享密钥 |
| `FEISHU_APP_ID` | 飞书 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 飞书 | 飞书应用密钥 |
| `LINKEDIN_EMAIL` | LinkedIn | 账号邮箱 |
| `LINKEDIN_PASSWORD` | LinkedIn | 账号密码 |
| `LINKEDIN_LI_AT` | LinkedIn | Cookie `li_at`（可替代邮箱密码） |
| `LINKEDIN_JSESSIONID` | LinkedIn | Cookie `JSESSIONID` |

### 代理（可选）

容器内需要访问外网时设置 `PROXY_URL`（如 `http://host.docker.internal:7897`）。Docker 构建时需要代理则设置 `BUILD_PROXY`。

## 项目结构

```
reminder-agent/
├── scheduler/                # Python 后端
│   ├── main.py               # FastAPI 入口 + 启动/关闭
│   ├── agent.py              # LLM Agent 工具调用循环
│   ├── auth.py               # Token 认证工具
│   ├── config.py             # 配置加载（config.yaml + .env）
│   ├── config.yaml           # 配置模板，支持 ${VAR:-default} 占位
│   ├── database.py           # 数据库引擎 + 建表 + 积分系统
│   ├── scheduler.py          # APScheduler + 提醒触发 + SSE 推送
│   ├── tools.py              # 工具执行器（增删查提醒等）
│   ├── requirements.txt
│   ├── adapters/             # IM 适配器实现
│   │   ├── base.py           # BaseIMAdapter 抽象基类
│   │   ├── wechat.py         # 微信 — HTTP 桥接 Node.js 进程
│   │   ├── whatsapp.py       # WhatsApp — HTTP 桥接 Node.js 进程
│   │   ├── feishu.py         # 飞书 — 进程内 FeishuBot 封装
│   │   └── linkedin.py       # LinkedIn — 进程内 LinkedInBot 封装
│   ├── channels/             # IM 底层协议实现
│   │   ├── feishu.py         # 飞书 WebSocket 客户端
│   │   └── linkedin.py       # LinkedIn HTTP 轮询客户端
│   ├── routes/               # FastAPI 路由模块
│   │   ├── auth.py           # /api/register, /api/login
│   │   ├── chat.py           # /api/chat, /api/reset
│   │   ├── reminders.py      # /reminders
│   │   ├── platforms.py      # /api/platforms/*, IM 回调端点
│   │   └── sse.py            # /api/sse（Server-Sent Events）
│   └── static/
│       └── index.html        # 单页 Web 前端
├── wechat-adapter/           # Node.js 微信适配器
│   ├── bot.mjs               # iLink WebSocket 机器人
│   ├── adapter.mjs           # HTTP 桥接（状态/QR/推送端点）
│   └── package.json
├── whatsapp-adapter/         # Node.js WhatsApp 适配器
│   ├── bot.mjs               # Baileys WebSocket 机器人
│   ├── adapter.mjs           # HTTP 桥接
│   └── package.json
├── docker-compose.yml
├── Dockerfile
├── supervisord.conf          # 容器内多进程管理
├── .env.example              # 环境变量模板
└── .gitignore
```

## API 概览

| 端点 | 方法 | 认证 | 说明 |
|----------|--------|------|-------------|
| `/api/register` | POST | — | 注册新账号 |
| `/api/login` | POST | — | 登录，返回 Token |
| `/api/chat` | POST | Token | 发送消息给 Agent |
| `/api/sse` | GET | Token | SSE 实时通知流 |
| `/api/reset` | POST | Token | 重置对话历史 |
| `/api/me/points` | GET | Token | 查询积分余额 |
| `/reminders` | GET | Token | 查看提醒列表 |
| `/reminders/{id}` | DELETE | Token | 取消提醒 |
| `/health` | GET | — | 健康检查 |

启动后可访问 `/docs` 查看完整 Swagger API 文档。

## Agent 可用工具

LLM Agent 可自主调用以下工具：

| 工具 | 说明 |
|------|-------------|
| `get_current_time` | 获取当前时间（含时区） |
| `create_reminder` | 创建提醒（扣 0.1 分） |
| `list_reminders` | 查看提醒列表，可按状态筛选 |
| `get_reminder` | 按 ID 查看特定提醒 |
| `cancel_reminder` | 取消待执行提醒 |
| `get_notifications` | 获取未读已触发提醒 |
| `ack_notifications` | 标记通知为已读 |
| `set_persona` | 切换 Agent 说话风格 |
| `get_balance` | 查询积分余额 |

## License

MIT
