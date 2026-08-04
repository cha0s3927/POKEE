# POKEE — Personal Reminder Agent

A multi-platform reminder agent powered by LLM. Chat naturally via WeChat, WhatsApp, Feishu, LinkedIn, or Web UI — the agent understands your intent and schedules reminders. No rigid command syntax needed.

## Features

- **Natural language reminders** — "Remind me to submit the report at 3pm tomorrow" just works
- **Multi-platform** — Web UI + WeChat + WhatsApp + Feishu + LinkedIn, one agent behind all
- **Points system** — Daily login bonus, reminder deduction, audit trail
- **Persona system** — Customizable agent personality per user
- **Persistent scheduling** — APScheduler with SQLite job store, survives restarts
- **Real-time push** — SSE for Web UI, adapter push for IM platforms

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Web UI (static)                 │
└──────────────────────┬──────────────────────────┘
                       │ SSE / HTTP
┌──────────────────────▼──────────────────────────┐
│              FastAPI (main.py)                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │  routes/ │ │  agent   │ │    adapters/     │ │
│  │  auth    │ │  (LLM)   │ │  wechat/whatsapp │ │
│  │  chat    │ │          │ │  feishu/linkedin │ │
│  │  sse     │ │ tools ◄──┼── scheduler        │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
└──────────────────────┬──────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
    ┌─────────┐ ┌──────────┐ ┌──────────┐
    │ WeChat  │ │ WhatsApp │ │  Feishu  │  LinkedIn
    │ (iLink) │ │ (Baileys)│ │  (SDK)   │  (Cookie)
    │ Node.js │ │ Node.js  │ │  Python  │  Python
    └─────────┘ └──────────┘ └──────────┘
```

| Layer | Tech | Role |
|-------|------|------|
| **API** | FastAPI + Uvicorn | HTTP/SSE endpoints, static file serving |
| **Agent** | OpenAI-compatible LLM (DeepSeek) | Tool-calling loop: classify intent → execute tool → reply |
| **Scheduler** | APScheduler + SQLite | Cron-like job store, fires reminders at due time |
| **Adapters** | Python + Node.js | Per-platform IM protocol handling, credential isolation |
| **Auth** | Token-based | Web login, IM bindings via `user_im_bindings` |

## Supported Platforms

| Platform | Protocol | Technology | QR Login |
|----------|----------|------------|----------|
| **Web UI** | HTTP + SSE | FastAPI + vanilla JS | Email/password |
| **WeChat** | iLink Bot API | Node.js adapter (`weixin-agent-sdk`) | QR scan |
| **WhatsApp** | Baileys WebSocket | Node.js adapter (`@whiskeysockets/baileys`) | QR scan |
| **Feishu** | Lark WebSocket | Python (`lark-oapi`) | QR scan |
| **LinkedIn** | HTTP polling | Python (`linkedin-api`) | Cookie |

## Quick Start

### Prerequisites

- Python 3.9+
- Node.js 22+ (for WeChat + WhatsApp adapters)
- DeepSeek API key (or any OpenAI-compatible endpoint)

### Local Development

```bash
# 1. Clone
git clone <repo-url>
cd reminder-agent

# 2. Configure
cp .env.example .env
# Edit .env — fill in DEEPSEEK_API_KEY at minimum

# 3. Install Python deps
cd scheduler
pip install -r requirements.txt

# 4. Install Node deps (optional, for WeChat/WhatsApp)
cd ../wechat-adapter && npm install && cd ..
cd ../whatsapp-adapter && npm install && cd ..

# 5. Run
cd scheduler
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` — register an account and start chatting.

### Docker

```bash
cp .env.example .env
# Edit .env with your API keys

docker compose up -d --build
```

The container runs 3 processes via supervisord: FastAPI (port 8000), WeChat adapter, WhatsApp adapter.

## Configuration

All configuration is via environment variables (or `.env` file). See `.env.example` for the full template.

### Required

| Variable | Description |
|----------|-------------|
| `DEEPSEEK_API_KEY` | LLM API key (DeepSeek or OpenAI-compatible) |
| `DEEPSEEK_BASE_URL` | LLM API endpoint (default: `https://api.deepseek.com/v1`) |

### Platform-specific

| Variable | Platform | Description |
|----------|----------|-------------|
| `WECHAT_SECRET` | WeChat | Shared secret between scheduler and Node adapter |
| `WHATSAPP_SECRET` | WhatsApp | Shared secret between scheduler and Node adapter |
| `FEISHU_APP_ID` | Feishu | Lark application ID |
| `FEISHU_APP_SECRET` | Feishu | Lark application secret |
| `LINKEDIN_EMAIL` | LinkedIn | Account email |
| `LINKEDIN_PASSWORD` | LinkedIn | Account password |
| `LINKEDIN_LI_AT` | LinkedIn | Cookie `li_at` (alternative to email/password) |
| `LINKEDIN_JSESSIONID` | LinkedIn | Cookie `JSESSIONID` |

### Proxy (optional)

Set `PROXY_URL` for container outbound traffic (e.g., `http://host.docker.internal:7897`). Set `BUILD_PROXY` if the Docker build itself needs a proxy.

## Project Structure

```
reminder-agent/
├── scheduler/                # Python backend
│   ├── main.py               # FastAPI app + startup/shutdown
│   ├── agent.py              # LLM agent with tool-calling loop
│   ├── auth.py               # Token-based auth helpers
│   ├── config.py             # Settings loader (config.yaml + .env)
│   ├── config.yaml           # Config template with env-var substitution
│   ├── database.py           # SQLAlchemy engine + schema + points system
│   ├── scheduler.py          # APScheduler + fire_reminder + SSE push
│   ├── tools.py              # Tool executor (create/list/cancel reminders, etc.)
│   ├── requirements.txt
│   ├── adapters/             # IM adapter implementations
│   │   ├── base.py           # BaseIMAdapter abstract class
│   │   ├── wechat.py         # WeChat — HTTP bridge to Node.js process
│   │   ├── whatsapp.py       # WhatsApp — HTTP bridge to Node.js process
│   │   ├── feishu.py         # Feishu — in-process FeishuBot wrapper
│   │   └── linkedin.py       # LinkedIn — in-process LinkedInBot wrapper
│   ├── channels/             # Low-level IM protocol
│   │   ├── feishu.py         # Lark WebSocket client
│   │   └── linkedin.py       # LinkedIn HTTP polling client
│   ├── routes/               # FastAPI route modules
│   │   ├── auth.py           # /api/register, /api/login
│   │   ├── chat.py           # /api/chat, /api/reset
│   │   ├── reminders.py      # /reminders
│   │   ├── platforms.py      # /api/platforms/*, IM webhook endpoints
│   │   └── sse.py            # /api/sse (Server-Sent Events)
│   └── static/
│       └── index.html        # Single-page web UI
├── wechat-adapter/           # Node.js WeChat adapter
│   ├── bot.mjs               # iLink WebSocket bot
│   ├── adapter.mjs           # HTTP bridge (status/qr/push endpoints)
│   └── package.json
├── whatsapp-adapter/         # Node.js WhatsApp adapter
│   ├── bot.mjs               # Baileys WebSocket bot
│   ├── adapter.mjs           # HTTP bridge
│   └── package.json
├── docker-compose.yml
├── Dockerfile
├── supervisord.conf          # Multi-process manager for container
├── .env.example              # Environment variable template
└── .gitignore
```

## API Overview

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/register` | POST | — | Register new account |
| `/api/login` | POST | — | Login, returns token |
| `/api/chat` | POST | Token | Send message to agent |
| `/api/sse` | GET | Token | SSE stream for real-time notifications |
| `/api/reset` | POST | Token | Reset conversation history |
| `/api/me/points` | GET | Token | Query points balance |
| `/reminders` | GET | Token | List reminders |
| `/reminders/{id}` | DELETE | Token | Cancel a reminder |
| `/health` | GET | — | Health check |

Full API docs at `/docs` (Swagger UI) when the server is running.

## Tools Available to Agent

The LLM agent can call these tools autonomously:

| Tool | Description |
|------|-------------|
| `get_current_time` | Get current time with timezone |
| `create_reminder` | Schedule a new reminder (-0.1 pts) |
| `list_reminders` | List user's reminders, optionally filtered by status |
| `get_reminder` | Get a specific reminder by ID |
| `cancel_reminder` | Cancel a pending reminder |
| `get_notifications` | Get unread fired reminders |
| `ack_notifications` | Mark notifications as seen |
| `set_persona` | Change agent personality |
| `get_balance` | Query points balance |

## License

MIT
