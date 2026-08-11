# Daily Run — always-on webinar ops

This is the permanent daily contract. **Do not ask** for these metrics at end of day — they are written automatically into `docs/daily/YYYY-MM-DD.md` and mirrored at `docs/DAILY-RUN-TODAY.md`.

Works for **any** workshop set: load sessions → auto-start → chats fire → metrics land in the daily MD.

---

## What we collect every day (no exceptions)

| Metric | Tag / field | Window | Source |
|--------|-------------|--------|--------|
| **Chat drops** | `chat_drops` | each scheduled `text` row | Scheduler fires + daily report |
| **Peak attendees** | `peak_attendees` / Peak Showup | first **60 min** after `session_start_ist`, poll every **10s** | `day_ops_runner` → `/tmp/hermes-peak-attendees.json` |
| **Payment link drop attendees** | `payment_link_drop_attendees_count` / Retention | first chat containing `link.outskill.com` | Scheduler + `day_ops_runner` → `/tmp/hermes-payment-drop-attendees.json` |
| **Payments (INR / USD / Total)** | Tracking E–G | after `session_end` + **5 min** | `day_ops_runner` → `scripts/sync_tracking_sheet.py --auto` → Workshops **Tracking** tab |

Live day file columns always include: session, meeting id, port, start, bridge state, chat planned/fired, peak (footer/attendees/participants), payment-drop time + attendees count + URL.

---

## Daily bootstrap (system, not human)

1. Drop / update Workshops Excel under `~/Downloads/Workshops*.xlsx` **or** edit `schedules/today_sessions.json`.
2. Ensure each session has: `meeting_id`, `port`, `session_start_ist`, `env_file`, `schedule_file`, panelist `MEETING_JOIN_URL`.
3. Keep `scripts/day_ops_runner.py` running (starts stacks **30 min** before each start, tracks peak + payment-drop count, refreshes the daily MD, and **writes Peak / Retention / INR / USD / Total Payments into the Workshops Tracking sheet at session_end + 5 min**).
4. Chats/polls fire from `SCHEDULE_FILE` via Python orchestrator — no manual send.

```bash
# Start / keep day ops (idempotent enough — one process)
cd /path/to/heimdall
nohup .venv/bin/python scripts/day_ops_runner.py >> /tmp/day-ops-runner.log 2>&1 &

# Force-refresh today's markdown now
.venv/bin/python scripts/write_daily_run.py
```

---

## Artifacts

| Path | Role |
|------|------|
| `docs/DAILY-RUN.md` | This playbook (stable) |
| `docs/DAILY-RUN-TODAY.md` | Latest auto report (overwrite each write) |
| `docs/daily/YYYY-MM-DD.md` | Dated archive |
| `schedules/today_sessions.json` | Today's session manifest (ports, starts, envs) |
| `/tmp/hermes-peak-attendees.json` | Raw peak samples |
| `/tmp/hermes-payment-drop-attendees.json` | Payment-drop attendees |
| `/tmp/hermes-sheet-sync.json` | End-of-session Tracking sheet sync status |
| `/tmp/hermes-chat-drops.json` | Scheduled chat fire log |
| `/tmp/day-ops-runner.log` | Ops runner log |

---

## Rules

- Auto-start lead: **30 minutes** before `session_start_ist` (`Asia/Kolkata`).
- Peak window: **60 minutes** from session start, sample every **10 seconds**.
- Payment-drop count: snapshot once at first `link.outskill.com` message (±90s via day-ops; exact on scheduler send).
- Tracking sheet sync: **session_end + 5 minutes** → Peak Showup, Retention, INR count, USD count, Total Payments (creates the Date+Workshop row if missing).
- **AI for Students** sessions write to the **AI for Students Tracking** tab (same Workshops Google Sheet) — one new row per day per Day-1 / Day-2; older rows are never changed.
- Display name must contain `AI`. Prefer `ANSWER_QUESTIONS=false` for schedule-only simulive.
- Never `pkill -f 'python -m src.main'` — kill by port / PID only.
- Missing panelist `tk=` → session stays blocked until URL appears in Excel/env; day-ops will pick it up.

---

## End of day

Open `docs/DAILY-RUN-TODAY.md` — peak, payment-drop attendees, and chat-drop summary are already there. No separate ask required.
