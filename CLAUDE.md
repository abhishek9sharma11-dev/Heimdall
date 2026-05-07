# CLAUDE.md — Om Asnani AI Webinar Bot

This file is read at the start of every Claude Code session. Keep it factual and current.

## What this project is

A self-hosted AI bot that joins Zoom webinars as a panelist named "Om Asnani AI" with the host's photo. Auto-greets attendees, answers questions in the host's voice using a local RAG knowledge base, and accepts host slash commands for scheduled messages.

**Hard constraint: self-hosted, no managed bot services.** No Recall.ai, no MeetStream, no third-party APIs except Anthropic for the LLM. The reason this constraint exists: cost control + data ownership.

## The split

This is a two-process system on the same Linux box:

- **`bridge/` — C++ daemon** that wraps the official Zoom Linux Meeting SDK. Listens on `127.0.0.1:8765`. Joins meetings, sends/receives chat. Touches the SDK; nothing else does.
- **`src/` — Python orchestrator** that talks to the bridge over HTTP/SSE. Holds the brain (LLM + RAG), greeter, slash commands, scheduler. Never imports Zoom code.

This split is deliberate. Don't merge them. If a feature touches Zoom, it goes in C++. If it touches the LLM, knowledge base, or business logic, it goes in Python.

## Tech stack

- **Python** 3.10+ with asyncio, `anthropic`, `httpx`, `pydantic-settings`, `faiss-cpu`, `sentence-transformers`
- **C++** 17 with the Zoom Linux Meeting SDK (v6.x family), `cpp-httplib` (single header), `nlohmann/json`, OpenSSL
- **Build**: CMake for the bridge, `pip install -r requirements.txt` for Python
- **LLM**: Claude (Sonnet 4.6 by default — change in `.env` via `ANTHROPIC_MODEL`)
- **OS**: Ubuntu 22.04 or 24.04 only. The Zoom Linux SDK doesn't run elsewhere headlessly.

## Directory map

```
.
├── README.md              project overview
├── CLAUDE.md              this file
├── .env.example           copy to .env, fill in
├── requirements.txt       Python deps
├── src/                   Python orchestrator
│   ├── main.py            entry point + signal handling
│   ├── config.py          pydantic Settings, single source for env
│   ├── zoom_client.py     DryRunClient + BridgeClient (HTTP/SSE)
│   ├── brain.py           PERSONA_CONFIG, ClaudeClient, RAGRetriever
│   └── handlers.py        Greeter, ChatHandler, SlashCommands, MessageScheduler
├── scripts/
│   └── ingest_knowledge.py  builds FAISS index from ./knowledge/
├── knowledge/             user content (.md, .txt) — drop files here
├── bridge/                C++ daemon
│   ├── CMakeLists.txt
│   ├── README.md          build instructions, SDK layout
│   ├── src/
│   │   ├── main.cpp       HTTP server (cpp-httplib) + SSE event bus
│   │   ├── meeting.h/cpp  SDK init, JWT auth, join/leave, participant tracking
│   │   ├── chat.h/cpp     chat send/receive
│   │   └── httplib.h      [needs download — see bridge/README.md]
│   └── zoomsdk/           [gitignored — drop unzipped Zoom Linux SDK here]
└── vector_store/          [gitignored — built by ingest_knowledge.py]
```

## Commands

```bash
# Python side
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/ingest_knowledge.py        # rebuild RAG index
ZOOM_BACKEND=dry_run python -m src.main   # local terminal sim
ZOOM_BACKEND=bridge python -m src.main    # production (needs bridge running)

python -m py_compile src/*.py             # syntax check

# C++ bridge
cd bridge
mkdir -p build && cd build
cmake ..
make -j
./zoom-bridge                              # listens on 127.0.0.1:8765
curl http://127.0.0.1:8765/health         # smoke test
```

## What's done vs stubbed

**Done and working:**
- Python orchestrator end-to-end (compiles, dry-run is functional)
- `DryRunClient` for terminal-based testing without Zoom
- `BridgeClient` with HTTP commands + SSE event consumption
- All handlers: greeter, chat router, slash commands, scheduler
- RAG with FAISS + sentence-transformers
- Persona prompt template in `src/brain.py`
- C++ HTTP server (cpp-httplib)
- C++ SDK init, JWT auth flow, Auth/Meeting service event listeners
- C++ chat event listener and send call wired

**Stubbed — three TODO markers in the C++:**
1. `bridge/src/meeting.cpp::run_pump()` — the SDK's main loop call. Different SDK versions name this differently. Look at `zoomsdk/demo/` in the official SDK download for the right pattern.
2. `bridge/src/meeting.cpp::ParticipantsListener` — the SDK adds new pure-virtual event methods between releases. Compiler will tell you which ones to stub.
3. `bridge/src/chat.cpp::Chat::send` — newer SDKs replaced `SendChatTo(uid, text)` with a builder pattern. Match what's in `zoomsdk/h/meeting_chat_interface.h`.

**Not built (intentional, future work):**
- Per-attendee conversation memory (follow-up questions don't carry context)
- Outbound profanity/abuse filter
- Q&A panel (separate from chat in webinars)
- Admin dashboard for post-session review

## SDK header reference (these live in `bridge/zoomsdk/h/` after the user downloads the SDK)

When fixing C++ TODOs, **always read the actual headers — don't guess from memory**:

| What | Header |
|---|---|
| Init/cleanup, language, branding | `zoom_sdk.h` |
| JWT auth flow | `auth_service_interface.h` |
| Join/leave, meeting status | `meeting_service_interface.h` |
| Chat send/recv | `meeting_chat_interface.h` |
| Participant list, host/co-host detection | `meeting_participants_ctrl_interface.h` |
| Common types (`SDKError`, `zchar_t`) | `zoom_sdk_def.h` |

The official sample at `bridge/zoomsdk/demo/` is the source of truth for the pump pattern.

## Working conventions

**Python:**
- Async everywhere — every I/O function is `async def`
- Type hints on all public functions
- `logging.getLogger(__name__)`, never `print` in `src/`. `print` only in dry-run UX and CLI scripts
- Settings exclusively via `src.config.settings` (single pydantic source)
- New behavior modules go in `src/handlers.py` if they're event-driven, in `src/brain.py` if they're LLM-side

**C++:**
- C++17, no exceptions for control flow (use return + err string)
- Each SDK-touching translation unit has a header with a small public API. Internal state stays static
- Never include Zoom headers from `main.cpp` — keeps build deps clean
- `std::lock_guard` for short critical sections, `std::unique_lock` only when you need condition variables
- Always emit JSON events via the `EventEmitter` callback, never call HTTP directly

**Git:**
- Work in feature branches even solo. `git switch -c sdk-pump-fix`
- Commit at every working state, however small
- `bridge/zoomsdk/`, `vector_store/`, `.venv/`, `__pycache__/`, `bridge/build/` are gitignored

## Workflow Claude Code should follow

This project rewards **Explore → Plan → Implement → Test → Commit**, not "just edit the file."

For C++ SDK matching specifically:
1. Read the relevant header in `bridge/zoomsdk/h/` first
2. Read the official sample in `bridge/zoomsdk/demo/` if it's about init/auth/pump patterns
3. Propose the change with a diff and which header lines support it
4. Compile (`cd bridge/build && make -j`) and surface compiler errors
5. Don't move on if it doesn't link cleanly

For persona iteration:
1. Edit `PERSONA_CONFIG` in `src/brain.py`
2. Run `ZOOM_BACKEND=dry_run python -m src.main`
3. Try 5-10 representative questions
4. Adjust the prompt, repeat
5. **Don't go anywhere near a real Zoom meeting until dry-run feels right.** This saves 80% of the integration debugging.

For knowledge base growth:
1. Add a new `.md` file to `knowledge/` (one topic per file)
2. `python scripts/ingest_knowledge.py`
3. Verify retrieval with a relevant question in dry-run
4. The bot's quality is bounded by knowledge base quality

## Non-negotiables

- The bot's display name MUST contain "AI" or "Assistant" — `src/config.py` defaults to "Om Asnani AI". Don't remove the suffix.
- The disclosure line fires on first contact with each attendee in `src/handlers.py::Greeter`. Don't disable.
- The `HOST_EMAIL` check in `src/handlers.py::ChatHandler._is_host` must remain — it's the only thing keeping random attendees from running slash commands.
- When the LLM doesn't have grounded RAG context for a factual question, the persona must say so and offer to flag for the human host. Don't let it confabulate.

## How to ask me (Claude Code) for help on this project

Good prompts for this codebase:
- "Read `bridge/zoomsdk/h/meeting_chat_interface.h` and tell me what `SendChatTo` looks like in our SDK version."
- "The compiler is complaining that `ParticipantsListener` has unimplemented pure virtuals. Find them and stub them."
- "Walk me through what happens when an attendee sends `?` in chat — start at the SSE event and end at the response back to Zoom."
- "Add a new slash command `/whoami` that DMs the host the bot's current config."

Bad prompts (will produce mush):
- "Make it work"
- "Fix the C++"
- "Improve the bot"

## Cost notes

The bot is cheap to run. Per 60-min webinar with ~50 attendees:
- Claude API: ~$0.50
- VPS (DigitalOcean droplet, $10/mo): pro-rated to pennies
- No per-minute platform fees (that's the whole point of self-hosting)

If a session is unexpectedly expensive, check `src/handlers.py::ChatHandler._check_rate` — per-user rate limit defaults to 2 replies/minute. Lower it if you're getting abused.
