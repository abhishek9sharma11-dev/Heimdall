# SYSTEM CONTEXT — Self-Hosted Zoom Webinar AI Co-host Bot

> **Purpose of this file:** Give this entire document to an LLM (or engineering team) and instruct it to rebuild the same product from scratch. It must produce a system with equivalent architecture, features, APIs, constraints, and operational model — not a vague chatbot sketch.

**Product name (example):** Heimdall AI webinar co-host  
**Hard constraint:** Self-hosted. No Recall.ai, MeetStream, or other managed “bot joins your Zoom” platforms. Third-party LLM APIs (OpenRouter, Anthropic, local Ollama) are allowed. You own the Zoom session automation.

**Last validated against live ops:** Playwright Mac bridge + Python orchestrator, multi-slot webinars on ports 8765–8767 (Aug 2026).

---

## 1. What the product does

A bot joins a **Zoom webinar as a panelist** (display name must contain `AI` or `Assistant`). While the human host presents (often simulive recording), the bot:

1. Monitors **webinar chat**
2. Optionally **answers attendee questions** in the host’s voice (LLM + optional RAG)
3. Can **draft** a private/DM-style reply to a person (select name + type text) and optionally **wait for a human to click Submit** (live-safe mode)
4. Fires a **timed schedule** of public chat messages and **poll launches** (clock times in a timezone)
5. Accepts **host-only slash commands** in chat
6. Can run **N isolated stacks** on one Mac (one Chrome + one bridge + one Python per webinar)

It does **not** currently automate Zoom’s separate Q&A panel (chat only). It does **not** keep per-attendee multi-turn memory across questions.

---

## 2. Architecture (non-negotiable split)

Two processes on the same machine. **Do not merge them.**

```
[Zoom Webinar Web Client / SDK]
            ↑
   Playwright Chromium  OR  Zoom Linux Meeting SDK
            ↑
[Bridge process]  ←── HTTP + SSE on 127.0.0.1:PORT ──→  [Python orchestrator]
   Zoom I/O only                                              Brain + business logic
```

| Layer | Responsibility | Must NOT do |
|-------|----------------|-------------|
| **Bridge** | Join/leave meeting, read chat, send chat, open polls, emit SSE events | Call LLMs, load RAG, know persona, own schedules |
| **Python** | Persona, LLM, RAG, greeter, slash commands, scheduler, rate limits, draft-vs-send policy | Import Zoom SDK / drive Chromium directly |

**Contract:** Python is a client of a small HTTP/SSE API. Either bridge implementation (Playwright or C++) can sit behind the same contract. Production multi-webinar Mac ops use **Playwright** (`bridge/node-bridge/`). Docker / Linux headless path uses **C++ Zoom Meeting SDK** (`bridge/src/`).

---

## 3. Tech stack

### Python orchestrator (`src/`)
- Python 3.10+
- asyncio everywhere for I/O
- `pydantic-settings` for config
- `httpx` for bridge HTTP + SSE
- `openai` AsyncOpenAI client pointed at OpenRouter **or** local Ollama (`/v1`)
- RAG: `sentence-transformers` + `faiss-cpu` + `numpy`
- Logging: stdlib `logging.getLogger(__name__)` in `src/` (no `print` except dry-run / CLI)

**Modules:**

| File | Role |
|------|------|
| `src/main.py` | Entry, signal handling, wire handlers, join, run |
| `src/config.py` | Single `Settings` singleton from env |
| `src/zoom_client.py` | `DryRunClient` + `BridgeClient` |
| `src/brain.py` | Persona prompt, LLM client, RAG retriever |
| `src/handlers.py` | Greeter, ChatHandler, SlashCommands, MessageScheduler |

### Playwright bridge (`bridge/node-bridge/`)
- Node.js + Express 5
- Playwright Chromium (headed, not headless — Zoom web client)
- Listens on `127.0.0.1` + `BRIDGE_PORT` (default 8765)
- Joins via Zoom **web client** panelist URL: `https://app.zoom.us/wc/{meetingId}/join?tk=...`

### C++ bridge (`bridge/`) — alternate
- C++17, Zoom Linux Meeting SDK v6.x, cpp-httplib, nlohmann/json
- Ubuntu 22.04/24.04 only for headless SDK
- Smaller API surface (no polls / no draft-submit flag unless you add them)

### LLM backends
1. **OpenRouter** (remote): `OPENROUTER_BASE_URL=https://openrouter.ai/api/v1` + real API key; free-model fallback list on 429/404
2. **Local Ollama** (no paid key): `OPENROUTER_BASE_URL=http://127.0.0.1:11434/v1`, `OPENROUTER_API_KEY=ollama`, `ANTHROPIC_MODEL=<ollama-tag>` e.g. `qwen2.5:3b`

Env field is still named `OPENROUTER_*` / `ANTHROPIC_MODEL` for historical reasons; treat as “OpenAI-compatible base URL + model id”.

---

## 4. Repository layout (target)

```
.
├── README.md
├── CLAUDE.md                          # agent/session conventions
├── docs/
│   ├── SYSTEM-CONTEXT.md              # THIS FILE
│   ├── TODAY-3-WEBINARS.md            # multi-slot ops
│   └── SESSION-3-HANDOFF.md
├── .env.example
├── .env.<meeting_id>                  # one env file per live webinar slot
├── requirements.txt                   # full (incl. RAG)
├── requirements-lite.txt              # Docker/schedule-only without embeddings
├── Dockerfile / docker-compose.yml    # C++ bridge path
├── knowledge/                         # .md / .txt for RAG
├── vector_store/                      # gitignored: index.faiss + chunks.pkl
├── schedules/
│   ├── ai-for-students.json
│   ├── claude-code.json
│   └── codex.json
├── scripts/
│   ├── ingest_knowledge.py
│   └── sid_watch.py                   # optional legacy schedule watcher
├── src/                               # Python orchestrator
└── bridge/
    ├── CMakeLists.txt / src/          # C++ Zoom SDK bridge
    ├── zoomsdk/                       # gitignored SDK drop-in
    └── node-bridge/
        ├── index.js
        └── package.json               # express, playwright
```

---

## 5. Environment variables (complete)

Loaded via pydantic-settings (`env_file=".env"`, process env overrides). Ops also `source .env.<meeting_id>` before `python -m src.main`.

| Env | Default | Meaning |
|-----|---------|---------|
| `ZOOM_BACKEND` | `dry_run` | `dry_run` \| `bridge` |
| `BRIDGE_URL` | `http://127.0.0.1:8765` | Bridge base URL |
| `MEETING_JOIN_URL` | `""` | Full panelist URL (`…/w/<id>?tk=…`); parsed for id/token/pwd |
| `MEETING_ID` | `""` | Digits only |
| `MEETING_PASSWORD` | `""` | Optional |
| `MEETING_ZAK` | `""` | Optional host ZAK |
| `MEETING_WEBINAR_TOKEN` | `""` | Panelist `tk=` value only |
| `ZOOM_SDK_KEY` / `ZOOM_SDK_SECRET` | `""` | For C++ SDK JWT auth |
| `OPENROUTER_API_KEY` | `""` | Real key or `ollama` for local |
| `OPENROUTER_BASE_URL` | OpenRouter URL | OpenAI-compatible base |
| `ANTHROPIC_MODEL` | free Llama id | Model id (OpenRouter or Ollama tag) |
| `BOT_DISPLAY_NAME` | `Heimdall AI` | **Must contain `AI` or `Assistant`** |
| `BOT_AVATAR_URL` | `""` | Optional |
| `BOT_DISCLOSURE_LINE` | disclosure string | Used in greeter |
| `HOST_EMAIL` | `""` | Only this email may run slash commands |
| `GREET_NEW_ATTENDEES` | `true` | Greeter on/off |
| `GREET_DELAY_SECONDS` | `3` | Delay before greet |
| `GREET_MAX_PER_MINUTE` | `10` | Greeter throttle |
| `ANSWER_QUESTIONS` | `true` | LLM Q&A on/off |
| `ANSWER_RATE_LIMIT_PER_USER_PER_MIN` | `2` | Per-user answer cap |
| `CHAT_AUTO_SUBMIT` | `true` | `false` = draft only (human Submit) |
| `KNOWLEDGE_DIR` | `./knowledge` | RAG sources |
| `VECTOR_STORE_PATH` | `./vector_store` | FAISS store |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformer id |
| `RAG_TOP_K` | `4` | Chunks retrieved |
| `SCHEDULE_FILE` | `""` | Path to session JSON |
| `SCHEDULE_TZ` | `Asia/Kolkata` | Clock times in schedule file |

**Startup rule:** If `ANSWER_QUESTIONS=true` and API key empty, allow start only when `OPENROUTER_BASE_URL` is local (`127.0.0.1` / `localhost` / `0.0.0.0`).

---

## 6. Bridge HTTP + SSE contract

Bind: `127.0.0.1` only. Port from `BRIDGE_PORT` (Node) or compile-time default 8765 (C++).

### Endpoints (Playwright bridge — production Mac)

| Method | Path | Body | Response / behavior |
|--------|------|------|---------------------|
| `GET` | `/health` | — | `{status, in_meeting, meeting_state, has_page, reconnecting}`. Treat live if `in_meeting` **or** `waiting` (simulive lobby). |
| `GET` | `/events` | — | SSE stream; comment keepalive every ~15s |
| `POST` | `/join` | `{meeting_id?, password?, display_name?, webinar_token?, join_url?, zak?, avatar_url?}` | Returns `{ok:true}` immediately; join runs async. Need `meeting_id` or `join_url`. Prefer navigate to `https://app.zoom.us/wc/{id}/join?tk=...`. |
| `POST` | `/send_chat` | `{text, to?: "everyone"\|name, submit?: true\|false}` | Serialized (mutex/chain) so concurrent typing doesn’t garble Zoom’s textarea. Paste text in one shot (not slow keystrokes). If `submit===false`: select recipient + fill input, **do not** press Enter/Send → `{ok:true, drafted:true}`. Else Enter → Send button → Meta/Ctrl+Enter fallbacks. |
| `POST` | `/poll/launch` | `{name}` or `{poll}` | Open Polls UI, select exact/prefix name, Launch |
| `POST` | `/poll/end` | `{name?}` | End current/named poll |
| `POST` | `/reconnect` | `{}` | Recover from preview/disconnect using last join params |
| `POST` | `/leave` | `{}` | Close page/browser; emit `meeting_ended` |
| `POST` | `/focus` | `{}` | Bring Chrome to front (ops helper) |
| `POST` | `/eval` | `{code}` | Debug `page.evaluate` |

### SSE event types (Playwright)

```json
{"type":"chat_message","sender_name":"...","sender_id":"...","text":"...","is_private":false}
{"type":"joined","meeting_state":"in_meeting"}
{"type":"disconnected","reason":"...","meeting_state":"..."}
{"type":"reconnecting","strategy":"click_join"|"full_rejoin"}
{"type":"reconnected","meeting_state":"..."}
{"type":"poll_launched","poll":"..."}
{"type":"poll_ended","poll":"..."}
{"type":"meeting_ended"}
```

**Gaps vs ideal:** Playwright bridge today typically does **not** emit `participant_joined` / `participant_left` (C++ SDK bridge can). Greeter depends on join events — effectively inactive on Playwright until you add participant DOM monitoring. Chat `sender_id` is usually the cleaned display name (no email).

### Chat monitoring (Playwright)
- Prefer web client after “Join from your browser”
- Open chat panel; inject MutationObserver + polling for tip bubbles and chat list
- Dedupe tip keys ~30 seconds
- Filter noise: very short strings, clock-like text, sender==text

### Recipient selection algorithm
1. Click chat “to:” dropdown (`.chat-receiver-list__receiver`)
2. Match menu item: case-insensitive exact **or** 12-char prefix either direction
3. Never treat `"everyone"` or `"hosts and panelists"` as a named DM match
4. On miss: Escape, fall back to Everyone for send
5. For Everyone: ensure dropdown shows Everyone before typing

### Join UX details (Playwright)
- Headed Chromium, fixed window size (~1280×800), fake media devices
- Fill display name on preview; click Join
- Post-join: open chat, start monitors, reconnect watchdog (~4s poll of meeting state)
- States: `idle` | `joining` | `waiting` | `in_meeting` | `preview` | `disconnected`

---

## 7. Python client behavior (`BridgeClient`)

- On `join`: if `/health` already shows in meeting/waiting for same session, skip duplicate `/join`
- Always attach SSE `/events` in `run()`
- `send_chat`: POST with `submit`; on failure → `/reconnect`, sleep, retry once
- Map SSE `chat_message` → `ChatHandler.on_message`
- Map `joined` / `disconnected` / `reconnecting` for logging / recovery
- `DryRunClient`: terminal simulation; print drafts with `[DRAFT — click Submit]` when `submit=False`

---

## 8. Feature specs (must implement)

### 8.1 Greeter
- If `GREET_NEW_ATTENDEES` and event `participant_joined` with `role=="attendee"` and not yet greeted:
  - Throttle: max `GREET_MAX_PER_MINUTE` in rolling 60s
  - Sleep `GREET_DELAY_SECONDS`
  - Send public chat: short hello using first name + bot display name + disclosure fragment
- Disclosure line must exist in product requirements (compliance / trust)

### 8.2 Question answering (`ChatHandler`)
Pipeline for each `chat_message`:

1. If text starts with `/` → host slash handler only (email == `HOST_EMAIL`); else ignore
2. If `_quiet` or `ANSWER_QUESTIONS=false` → stop
3. If **not** private and **not** `_looks_like_question` → stop  
   **Question heuristics:** contains `?` OR any of: `how do`, `how can`, `what is`, `what's`, `why does`, `why is`, `where can`, `when does`, `can you`, `could you`, `is there` OR bot display name appears in text
4. **Handled-question skip:** key = `lower(sender_id)|normalize_ws(lower(text))`. If in `_handled_questions` → stop (do not re-answer).  
   **Claim key before LLM call.** Discard key only if LLM or send fails.  
   **If human clears a draft without Submit, keep the key** — move on; never re-draft that same question.
5. Per-user rate limit: deque of answer timestamps, max `ANSWER_RATE_LIMIT_PER_USER_PER_MIN` / 60s
6. RAG retrieve → LLM reply
7. `send_chat(reply, to=sender_id or sender_name, submit=CHAT_AUTO_SUBMIT)`

### 8.3 Draft-only mode (`CHAT_AUTO_SUBMIT=false`)
- Used for live-audience testing with human approval
- Bridge fills recipient + text; human clicks Zoom Send/Submit
- Scheduled public CTAs still use `submit=true` (auto-send)
- Only Q&A path respects `CHAT_AUTO_SUBMIT`

### 8.4 Slash commands (host email only)
| Command | Effect |
|---------|--------|
| `/schedule 5m <text>` / `90s` / `2h` / `HH:MM` | Queue public message (`HH:MM` interpreted as **UTC today** in current code) |
| `/list` | DM host pending queue |
| `/cancel <id>` | Cancel item |
| `/quiet` / `/resume` | Pause/resume auto Q&A |
| `/dm <name>: <text>` | DM participant by remembered name |

### 8.5 Message scheduler (file-driven)
**JSON schema (required):**

```json
{
  "meeting_id": "82260231356",
  "session": "AI for Students",
  "session_start_ist": "18:58:00",
  "session_end_ist": "21:43:32",
  "items": [
    { "time": "18:58:10", "poll": "Exact Poll Name In Zoom UI" },
    { "time": "18:58:20", "text": "Welcome message..." },
    { "time": "21:09:14", "text": "CTA with https://link..." },
    { "time": "HH:MM:SS", "poll": "Name", "poll_end": "HH:MM:SS", "text": "optional chat same time" }
  ]
}
```

Rules:
- `meeting_id` **must** match `MEETING_ID` or file is rejected
- Bare JSON arrays rejected
- Times `HH:MM` or `HH:MM:SS` in `SCHEDULE_TZ` → convert to fire times
- Loop every ~2 seconds
- Actions: `send_chat` to everyone (always auto-submit), `launch_poll`, `end_poll`
- **First load grace:** items ≤90s past may still fire after ~5s; **hot-reload** skips past times
- Hot-reload on file mtime change (validate JSON, replace queue, preserve `_fired_keys` so reconnect doesn’t double-blast)
- Expand payment CTAs into many timed rows (every 2–3 minutes) in the schedule file itself — scheduler has no “repeat every N minutes” DSL

### 8.6 RAG
**Ingest (`scripts/ingest_knowledge.py`):**
- Read `.md`/`.txt` under `knowledge/`
- Chunk ~800 chars, ~120 overlap, prefer paragraph breaks
- Embed with `EMBEDDING_MODEL`, FAISS `IndexFlatL2`
- Write `vector_store/index.faiss` + `chunks.pkl`

**Runtime:**
- If store missing → log warning, operate with empty context (persona must not invent host facts)
- Retrieve top-k; drop weak matches (e.g. L2 distance threshold ~1.5)
- Inject into persona prompt as `{rag_context}`

### 8.7 Persona / LLM prompt contract
Rebuild must keep equivalent constraints in the system/user prompt:

- Bot is host’s AI co-host, not the host
- Explain simply (≈ “talking to a 10-year-old”)
- **No markdown**, no bullets, no headers, **no em dashes**
- Structure: ~2 short lines + 1 example line starting with “For example,” or “Think of it like,”
- Use first name when known
- No invented pricing/commitments/personal facts — offer to flag for host
- Defer medical/legal/financial; ignore hostile/off-topic lightly
- `max_tokens` ≈ 300

`PERSONA_CONFIG` fields: `host_name`, `topics_yes`, `topics_no_extra`, `writing_samples`.

### 8.8 Local vs remote LLM
- Detect local by base URL host
- Local: only configured model (no OpenRouter free fallbacks)
- Remote: try `ANTHROPIC_MODEL` then fallback list of free OpenRouter models on rate-limit / missing endpoint

---

## 9. Multi-webinar ops model (must support)

One Mac can run N stacks. **Never share** `MEETING_ID` / `BRIDGE_URL` / `SCHEDULE_FILE` across processes.

| Slot | `BRIDGE_PORT` | Env file | Schedule |
|------|---------------|----------|----------|
| 1 | 8765 | `.env.<id1>` | `schedules/<slug1>.json` |
| 2 | 8766 | `.env.<id2>` | `schedules/<slug2>.json` |
| 3 | 8767 | `.env.<id3>` | `schedules/<slug3>.json` |

Each stack = **1 Chromium (Playwright) + 1 `node index.js` + 1 `python -m src.main`**.

```bash
# Bridge
cd bridge/node-bridge
BRIDGE_PORT=8767 nohup node index.js >> /tmp/node-bridge-8767.log 2>&1 &

# Orchestrator
nohup bash -lc '
  set -a
  source .venv/bin/activate
  source .env.82260231356
  set +a
  exec python -m src.main
' >> /tmp/python-8767.log 2>&1 &
```

**Kill by PID/port only.** Do not `pkill -f 'python -m src.main'` (kills all slots).

**Python shutdown currently calls `/leave`** — restarting Python can eject the bridge from the meeting. Prefer hot-reload for schedule edits; if restarting Python mid-session, expect rejoin.

---

## 10. Example live slot (AI for Students)

Reference configuration used in production testing:

```
ZOOM_BACKEND=bridge
BRIDGE_URL=http://127.0.0.1:8767
MEETING_ID=82260231356
MEETING_JOIN_URL=https://us06web.zoom.us/w/82260231356?tk=...
MEETING_WEBINAR_TOKEN=<tk value>
BOT_DISPLAY_NAME="GS BOT AI"
GREET_NEW_ATTENDEES=false
ANSWER_QUESTIONS=true
CHAT_AUTO_SUBMIT=false
SCHEDULE_FILE=./schedules/ai-for-students.json
SCHEDULE_TZ=Asia/Kolkata
OPENROUTER_API_KEY=ollama
OPENROUTER_BASE_URL=http://127.0.0.1:11434/v1
ANTHROPIC_MODEL=qwen2.5:3b
```

Schedule pattern for simulive:
- T+10s poll launch (exact Zoom poll title)
- T+20s welcome + “fill the poll”
- Mid-session engagement chat
- Final ~30 minutes: repeating payment CTA + resource Drive link + bootcamp dates (absolute `HH:MM:SS` rows)
- Convert relative offsets → absolute IST using webinar start; clamp CTA block to recording end time

Known session facts ops encode in human replies / knowledge (example): **this session may not provide recordings** — bot should not invent “yes you’ll get a recording” without KB support.

---

## 11. Dry-run mode (mandatory for persona iteration)

```bash
ZOOM_BACKEND=dry_run python -m src.main
```

Terminal acts as attendees; bot prints replies. **Do not test persona changes live first.**

---

## 12. Dependencies

**`requirements.txt`:** openai, pydantic, pydantic-settings, python-dotenv, httpx, fastapi, uvicorn, sentence-transformers, faiss-cpu, numpy, apscheduler, structlog  

**`requirements-lite.txt`:** same minus sentence-transformers/faiss (schedule + chat without local embeddings)

**`bridge/node-bridge/package.json`:** `express`, `playwright`

**Optional host tools:** Ollama for local models; Chrome for Testing via Playwright install.

---

## 13. Non-negotiables (product / compliance)

1. Self-hosted Zoom automation — no managed meeting-bot SaaS.
2. Bridge vs Python split preserved.
3. Display name contains `AI` or `Assistant`.
4. AI disclosure on first contact (greeter / equivalent).
5. Slash commands gated by `HOST_EMAIL`.
6. No confident confabulation of host-specific facts when RAG is empty — say so / flag for host.
7. Q&A draft mode must support human Submit for live testing.
8. Same question must not be re-drafted after human clears without sending.
9. Schedule `meeting_id` gate + hot-reload + no double-send after reload.
10. Chat sends serialized in the bridge.

---

## 14. Explicit non-goals (do not fake as done)

- Per-attendee multi-turn conversation memory
- Outbound profanity/abuse filter
- Zoom Q&A panel automation (separate from chat)
- Admin dashboard / post-session analytics UI
- Perfect greeter on Playwright until participant events exist

---

## 15. Acceptance tests (rebuild checklist)

An implementation is “feature-complete vs this system” when:

1. `ZOOM_BACKEND=dry_run` can answer a `?` question with persona-shaped plain text.
2. With bridge up, `/health` + `/join` + SSE `chat_message` round-trip works.
3. `POST /send_chat` with `submit:false` leaves text in Zoom input without sending.
4. Named recipient selection DMs the right attendee when present in dropdown.
5. `ANSWER_QUESTIONS=true` + local Ollama produces a draft without OpenRouter key.
6. Clearing a draft does not cause a second draft for the same `(sender, text)`.
7. Schedule file with matching `meeting_id` fires chat + poll at IST clock times; editing file hot-reloads; past rows on reload are skipped; already-fired keys don’t repeat.
8. Two stacks on 8765 and 8767 can run isolated with different `.env.*` files.
9. Host-only `/quiet` stops answers; `/resume` restores.
10. Display name without `AI`/`Assistant` is rejected or warned (product rule).

---

## 16. Implementation order (recommended for a from-scratch build)

1. Python dry-run + persona + config  
2. Bridge `/health` `/join` `/send_chat` `/events` (Playwright web client)  
3. Wire BridgeClient SSE → ChatHandler  
4. `submit:false` draft mode + recipient select  
5. Handled-question set + rate limits  
6. Schedule JSON loader + hot-reload + poll endpoints  
7. RAG ingest + retriever  
8. Ollama + OpenRouter dual backend  
9. Multi-port ops scripts / env-per-meeting  
10. (Optional) C++ SDK bridge with same HTTP contract for Linux servers  
11. (Optional) Participant events → greeter on Playwright  

---

## 17. Prompt you can give another LLM

Copy-paste:

> Rebuild a self-hosted Zoom webinar AI co-host exactly as specified in `docs/SYSTEM-CONTEXT.md`. Preserve the two-process architecture (Playwright or SDK bridge + Python orchestrator), the HTTP/SSE contract, schedule JSON schema, draft-only chat mode, handled-question skip, host slash commands, RAG, and OpenRouter/Ollama LLM backends. Do not use managed meeting-bot SaaS. Implement acceptance tests in section 15. Prefer Playwright Mac bridge first for webinar web client panelist join via `tk=` token.

---

## 18. Glossary

| Term | Meaning |
|------|---------|
| Panelist link | Zoom URL with `tk=` allowing bot to join as panelist |
| Simulive | Webinar that plays a recording on a schedule |
| Draft mode | Bot types reply; human submits |
| Slot | One isolated bridge port + env + schedule + Chrome |
| Bridge | Process that touches Zoom UI/SDK |
| Orchestrator | Python brain + handlers |

---

*End of system context. If anything in an existing repo conflicts with this file, treat **live Playwright + Python behavior** described here as the source of truth for product features; treat C++ bridge as an alternate transport with a subset of endpoints unless extended.*
