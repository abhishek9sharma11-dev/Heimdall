# Heimdall — Self-Hosted Webinar Co-host Bot

A self-hosted AI bot that joins your Zoom webinars as a panelist named **"Heimdall AI"** with your photo. Auto-greets attendees, answers questions in your voice using your own content, runs scheduled messages on slash commands. No managed bot services. You own the whole stack.

---

## The two-piece architecture

This is two processes that run on the same Linux box and talk over `localhost`:

1. **`bridge/`** — a C++ daemon that uses the official Zoom Linux Meeting SDK to join webinars and send/receive chat. Exposes a tiny HTTP API on `:8765`.
2. **`src/`** — a Python orchestrator that handles the brain (LLM, RAG), greeter, slash commands, and scheduler. Talks to the bridge over HTTP.

Each process is small. The C++ side is ~500 lines and only knows about Zoom. The Python side is ~800 lines and only knows about your business logic. You can rewrite either independently.

```
[Zoom Webinar]
       ↑
   SDK protocol
       ↑
[bridge/  C++ daemon] ← localhost:8765 → [src/  Python orchestrator] → [Claude API]
```

---

## What you have to do (honest list)

| Step | Effort | One-time? |
|---|---|---|
| Register a Zoom Marketplace app, get SDK credentials | 30 min | yes |
| Download Zoom Linux Meeting SDK from the marketplace | 5 min | yes (per SDK release) |
| Build the C++ bridge | 2–6 hours first time, then 5 min per change | mostly |
| Set up Python orchestrator | 30 min | yes |
| Build your knowledge base + persona | 2–4 hours of curating content | iterative |
| Submit the Zoom app for production review | weeks of waiting | yes |

You can run against your own meetings during development without review. Submission is only required if other Zoom accounts will use the bot.

---

## Setup

### 1. System requirements

- **Linux** (Ubuntu 22.04 or 24.04 recommended; the SDK is Linux-only for headless use)
- C++ toolchain: `gcc`, `cmake`, `pkg-config`, `libssl-dev`, `libxcb-*` (the SDK lists ~20 deps)
- Python 3.10+
- A box with at least 2GB RAM (the SDK is hungry)

A $10/mo VPS works fine for one bot. Don't run multiple bots on the same box without resource isolation.

### 2. Get the Zoom SDK

1. Go to [marketplace.zoom.us](https://marketplace.zoom.us) → Develop → Build App → General App
2. App credentials → copy Client ID + Secret to `.env`
3. Embed → Meeting SDK toggle → ON
4. Embed → Download Linux SDK → drop the unzipped folder into `bridge/zoomsdk/`

### 3. Build the bridge

```bash
cd bridge
mkdir build && cd build
cmake ..
make -j
./zoom-bridge   # listens on 127.0.0.1:8765
```

See `bridge/README.md` for the SDK file layout the build expects.

### 4. Set up Python orchestrator

```bash
cd ..  # back to repo root
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then edit it
```

### 5. Build your knowledge base

Drop your content into `./knowledge/` — markdown, txt, transcripts. One file per logical chunk works best.

```bash
python scripts/ingest_knowledge.py
```

### 6. Customize your voice

Edit `src/brain.py` → `PERSONA_CONFIG`. Fill in:
- Voice notes (how you write — lowercase? em-dashes?)
- Topics you're confident on
- Topics to defer to you
- 2–3 writing samples

Bot quality is bounded by how well you do this. Spend an hour.

### 7. Dry-run before going live

```bash
ZOOM_BACKEND=dry_run python -m src.main
```

This skips the bridge entirely and runs an interactive terminal where you type as an attendee. Iterate on the persona.

### 8. Go live

In one terminal:
```bash
./bridge/build/zoom-bridge
```

In another:
```bash
ZOOM_BACKEND=bridge MEETING_ID=123456789 MEETING_PASSWORD=xxx python -m src.main
```

---

## Slash commands (host-only)

Sent in public chat by the registered HOST_EMAIL:

| Command | Effect |
|---|---|
| `/schedule 5m <text>` | Send `<text>` to chat in 5 minutes |
| `/schedule 14:30 <text>` | Send at 14:30 local time |
| `/list` | DM you the queue |
| `/cancel <id>` | Cancel a scheduled message |
| `/quiet` / `/resume` | Pause/resume auto-replies |
| `/dm <name>: <text>` | Privately DM a participant |

---

## Project structure

```
heimdall/
├── README.md                  ← you are here
├── requirements.txt
├── .env.example
├── bridge/                    ← C++ daemon (Zoom SDK wrapper)
│   ├── README.md              ← build instructions
│   ├── CMakeLists.txt
│   ├── src/
│   │   ├── main.cpp           ← entry point + HTTP server
│   │   ├── meeting.cpp        ← SDK init, join, leave
│   │   ├── meeting.h
│   │   ├── chat.cpp           ← send/receive chat events
│   │   ├── chat.h
│   │   ├── http_server.cpp    ← tiny HTTP server (httplib.h)
│   │   └── events.cpp         ← SSE event stream to Python
│   └── zoomsdk/               ← drop the official SDK here (gitignored)
├── src/                       ← Python orchestrator
│   ├── main.py                ← entry point
│   ├── config.py              ← settings
│   ├── zoom_client.py         ← talks to bridge OR runs dry-run
│   ├── brain.py               ← persona + Claude + RAG
│   └── handlers.py            ← greeter, chat router, slash, scheduler
├── scripts/
│   └── ingest_knowledge.py
├── knowledge/                 ← drop your content here
│   └── sample-faq.md
└── vector_store/              ← gitignored, built from knowledge/
```

---

## A note on the "AI" suffix

You picked "Heimdall AI" as the display name. Two reasons that matters:

1. **Trust.** Attendees who know they're talking to an AI assistant phrase questions usefully. Attendees who *think* they're talking to you and find out otherwise lose trust in everything.
2. **Compliance.** Zoom's AI Companion policies and several jurisdictions (EU AI Act, California's SB-1001) require disclosure when AI participates in real-time interactions in commercial contexts. The "AI" suffix + a one-line disclosure on first contact covers you.

Both are configured in `.env` and `src/handlers.py`.

---

## What's not built yet (left as exercises)

- Persistent per-attendee conversation memory across follow-up questions
- Profanity / abuse filter on outgoing replies
- Q&A panel support (separate from chat in webinars)
- Admin dashboard to review responses post-session
- Multi-bot orchestration (running > 1 bot on the same box)

The codebase is structured so each is a localized addition.
