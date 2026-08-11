#!/usr/bin/env python3
"""Write docs/daily/YYYY-MM-DD.md + docs/DAILY-RUN-TODAY.md from live metrics.

Aggregates for every session in schedules/today_sessions.json:
  - chat drops (planned from schedule + fired from /tmp/hermes-chat-drops.json)
  - peak attendees (first hour) from /tmp/hermes-peak-attendees.json
  - payment_link_drop_attendees_count from /tmp/hermes-payment-drop-attendees.json
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "schedules" / "today_sessions.json"
DAILY_DIR = ROOT / "docs" / "daily"
TODAY_MD = ROOT / "docs" / "DAILY-RUN-TODAY.md"
PEAK_OUT = Path("/tmp/hermes-peak-attendees.json")
PAY_OUT = Path("/tmp/hermes-payment-drop-attendees.json")
CHAT_OUT = Path("/tmp/hermes-chat-drops.json")
TZ = ZoneInfo("Asia/Kolkata")
PAYMENT_LINK_RE = re.compile(r"https?://link\.outskill\.com/[^\s<>\"']+", re.I)


def now() -> datetime:
    return datetime.now(TZ)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def health(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"meeting_state": "down"}


def schedule_chats(path: Path) -> list[dict]:
    doc = load_json(ROOT / path if not Path(path).is_absolute() else Path(path), {})
    if not doc and path:
        doc = load_json(ROOT / str(path), {})
    items = []
    for it in doc.get("items") or []:
        if it.get("text"):
            items.append(
                {
                    "time": it.get("time") or "",
                    "is_payment": bool(PAYMENT_LINK_RE.search(it.get("text") or "")),
                    "preview": (it.get("text") or "").replace("\n", " ")[:80],
                }
            )
        elif it.get("poll"):
            items.append(
                {
                    "time": it.get("time") or "",
                    "is_payment": False,
                    "preview": f"[poll] {it.get('poll')}",
                }
            )
    items.sort(key=lambda x: x["time"])
    return items


def first_payment(chats: list[dict]) -> dict | None:
    for c in chats:
        if c.get("is_payment"):
            return c
    return None


def peak_for(sid: str, port: int, peaks: dict) -> dict:
    slots = peaks.get("slots") or {}
    # Prefer port:id key; fall back to bare port
    for key in (f"{port}:{sid}", str(port), sid):
        if key in slots:
            return slots[key]
    # fuzzy: any key ending with :sid or starting with port:
    for k, v in slots.items():
        if k.endswith(f":{sid}") or v.get("id") == sid or v.get("port") == port:
            return v
    return {}


def chat_fired_count(sid: str, meeting_id: str, chats_log: dict) -> tuple[int, list]:
    rows = []
    for r in chats_log.get("drops") or []:
        if r.get("session_id") == sid or r.get("meeting_id") == meeting_id:
            rows.append(r)
    return len(rows), rows


def render(date_str: str, sessions: list[dict]) -> str:
    peaks = load_json(PEAK_OUT, {"slots": {}})
    pays = load_json(PAY_OUT, {"sessions": {}})
    chats_log = load_json(CHAT_OUT, {"drops": []})
    n = now()
    lines = [
        f"# Daily Run — {date_str}",
        "",
        f"_Auto-generated {n.strftime('%Y-%m-%d %H:%M:%S %Z')} — do not edit by hand; rerun `scripts/write_daily_run.py`._",
        "",
        "Metrics (always): **chat drops** · **peak attendees (first 60 min @ 10s)** · **payment_link_drop_attendees_count**",
        "",
        "See playbook: [`docs/DAILY-RUN.md`](../DAILY-RUN.md)",
        "",
        "## Summary",
        "",
        "| Session | Start IST | Port | State | Chats fired/planned | Peak (best) | Pay-drop attendees | Pay-drop time |",
        "|---------|-----------|------|-------|---------------------|-------------|--------------------|---------------|",
    ]

    detail_blocks: list[str] = []

    for s in sessions:
        sid = s["id"]
        port = int(s["port"])
        h = health(port)
        state = h.get("meeting_state") or "down"
        chats = schedule_chats(s.get("schedule_file") or "")
        planned = len([c for c in chats if not str(c["preview"]).startswith("[poll]")])
        fired_n, fired_rows = chat_fired_count(sid, s.get("meeting_id") or "", chats_log)
        pk = peak_for(sid, port, peaks)
        best_peak = None
        for k in ("peak_attendees", "peak_footer", "peak_participants"):
            v = pk.get(k)
            if isinstance(v, int) and v > 0:
                best_peak = v
                break
        if best_peak is None:
            for k in ("peak_attendees", "peak_footer", "peak_participants"):
                v = pk.get(k)
                if isinstance(v, int):
                    best_peak = v
                    break
        pay = (pays.get("sessions") or {}).get(sid) or {}
        pay_count = pay.get("attendees_count")
        pay_time = pay.get("schedule_time") or "—"
        fp = first_payment(chats)

        lines.append(
            f"| {s.get('session') or sid} | {(s.get('session_start_ist') or '')[:5]} | "
            f"`{port}` | `{state}` | {fired_n}/{planned} | "
            f"{best_peak if best_peak is not None else '—'} | "
            f"{pay_count if pay_count is not None else '—'} | {pay_time} |"
        )

        detail_blocks.append(f"### {s.get('session') or sid}")
        detail_blocks.append("")
        detail_blocks.append(
            f"- **id:** `{sid}` · **meeting:** `{s.get('meeting_id')}` · **port:** `{port}`"
        )
        detail_blocks.append(
            f"- **window:** `{s.get('session_start_ist')}` → `{s.get('session_end_ist')}` IST"
        )
        detail_blocks.append(f"- **bridge:** `{state}` · **enabled:** `{s.get('enabled')}`")
        detail_blocks.append(
            f"- **schedule:** `{s.get('schedule_file')}` · **env:** `{s.get('env_file')}`"
        )
        detail_blocks.append("")
        detail_blocks.append("#### Peak attendees (first 60 min)")
        detail_blocks.append("")
        if pk:
            detail_blocks.append(
                f"- peak_footer: **{pk.get('peak_footer', '—')}**"
                f" · peak_attendees: **{pk.get('peak_attendees', '—')}**"
                f" · peak_participants: **{pk.get('peak_participants', '—')}**"
            )
            last = pk.get("last") or {}
            if last:
                detail_blocks.append(
                    f"- last sample: footer={last.get('footer')} attendees={last.get('attendees')} "
                    f"participants={last.get('participants')} at `{last.get('at', '—')}`"
                )
        else:
            detail_blocks.append("- _pending / not in peak window yet_")
        detail_blocks.append("")
        detail_blocks.append("#### Payment link drop attendees count")
        detail_blocks.append("")
        if pay:
            detail_blocks.append(f"- **attendees_count:** **{pay.get('attendees_count')}**")
            detail_blocks.append(f"- schedule_time: `{pay.get('schedule_time')}`")
            detail_blocks.append(f"- dropped_at: `{pay.get('dropped_at')}`")
            detail_blocks.append(f"- url: {pay.get('payment_url') or '—'}")
            detail_blocks.append(
                f"- raw: footer={pay.get('footer')} attendees={pay.get('attendees')} "
                f"participants={pay.get('participants')} source=`{pay.get('source')}`"
            )
        elif fp:
            detail_blocks.append(
                f"- _pending — first payment CTA scheduled `{fp['time']}`: `{fp['preview'][:60]}…`_"
            )
        else:
            detail_blocks.append("- _no `link.outskill.com` row in schedule_")
        detail_blocks.append("")
        detail_blocks.append("#### Chat drops")
        detail_blocks.append("")
        detail_blocks.append(f"- planned chat rows: **{planned}** · fired logged: **{fired_n}**")
        if chats:
            detail_blocks.append("")
            detail_blocks.append("| Time | Type | Preview |")
            detail_blocks.append("|------|------|---------|")
            for c in chats:
                kind = "payment" if c["is_payment"] else ("poll" if c["preview"].startswith("[poll]") else "chat")
                detail_blocks.append(f"| `{c['time']}` | {kind} | {c['preview'][:70]} |")
        detail_blocks.append("")

    lines.append("")
    lines.append("## Per-session detail")
    lines.append("")
    lines.extend(detail_blocks)
    lines.append("---")
    lines.append("")
    lines.append("### Raw metric files")
    lines.append("")
    lines.append(f"- peak: `{PEAK_OUT}`")
    lines.append(f"- payment drop: `{PAY_OUT}`")
    lines.append(f"- chat drops: `{CHAT_OUT}`")
    lines.append(f"- manifest: `{MANIFEST}`")
    lines.append("")
    return "\n".join(lines)


def write() -> Path:
    manifest = load_json(MANIFEST, {"sessions": []})
    date_str = manifest.get("date") or now().strftime("%Y-%m-%d")
    sessions = manifest.get("sessions") or []
    body = render(date_str, sessions)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    dated = DAILY_DIR / f"{date_str}.md"
    dated.write_text(body)
    TODAY_MD.write_text(body)
    return dated


def main() -> None:
    path = write()
    print(f"wrote {path}")
    print(f"wrote {TODAY_MD}")


if __name__ == "__main__":
    main()
