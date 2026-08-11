#!/usr/bin/env python3
"""Poll Zoom footer participant counts every 10s during each session's first hour.

Uses schedules/today_sessions.json for ports + peak windows.
Prefer scripts/day_ops_runner.py for combined auto-start + peak tracking.
This script remains useful as a standalone peak monitor.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "schedules" / "today_sessions.json"
OUT = Path("/tmp/hermes-peak-attendees.json")
INTERVAL = 10

EVAL = r"""
var text = (document.body && document.body.innerText) || '';
var n = null, attendees = null, participants = null;
var btns = Array.prototype.slice.call(document.querySelectorAll('button'));
for (var i = 0; i < btns.length; i++) {
  var a = (btns[i].getAttribute('aria-label') || '') + ' ' + (btns[i].innerText || '');
  var m = a.match(/(\d+)\s*participant/i) || a.match(/\[(\d+)\]/);
  if (m) { n = parseInt(m[1], 10); break; }
}
if (n === null) {
  var m2 = text.match(/(\d+)\s*Participants?/i);
  if (m2) n = parseInt(m2[1], 10);
}
var ma = text.match(/Attendees\s*\((\d+)\)/i);
if (ma) attendees = parseInt(ma[1], 10);
var mp = text.match(/Participants\s*\((\d+)\)/i);
if (mp) participants = parseInt(mp[1], 10);
return {footer: n, attendees: attendees, participants: participants};
"""


def health(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def eval_counts(port: int) -> dict:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/eval",
            data=json.dumps({"code": EVAL}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("result") or {}
    except Exception as e:
        return {"error": str(e)}


def load() -> dict:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text())
        except Exception:
            pass
    return {"updated_at": None, "slots": {}}


def parse_hhmmss(s: str, n: datetime) -> datetime:
    h, m, sec = map(int, s.split(":"))
    return n.replace(hour=h, minute=m, second=sec, microsecond=0)


def main() -> None:
    sessions = json.loads(MANIFEST.read_text())["sessions"] if MANIFEST.exists() else []
    print(f"[peak] writing {OUT} every {INTERVAL}s for {len(sessions)} sessions", flush=True)
    data = load()
    while True:
        now = datetime.now(TZ)
        if now.hour >= 23 and now.minute > 30:
            print("[peak] stopping after 23:30", flush=True)
            break
        for s in sessions:
            if not s.get("enabled", True):
                continue
            port = int(s["port"])
            start = parse_hhmmss(s["session_start_ist"], now)
            peak_until = start + timedelta(minutes=int(s.get("peak_window_minutes") or 60))
            if not (start <= now <= peak_until):
                continue
            h = health(port)
            state = h.get("meeting_state") or "down"
            if state not in ("in_meeting", "waiting"):
                continue
            c = eval_counts(port)
            key = f"{port}:{s['id']}"
            slot = data["slots"].setdefault(
                key,
                {
                    "port": port,
                    "id": s["id"],
                    "meeting_id": s.get("meeting_id"),
                    "peak_footer": 0,
                    "peak_attendees": 0,
                    "peak_participants": 0,
                    "last": {},
                },
            )
            for field, peak_key in (
                ("footer", "peak_footer"),
                ("attendees", "peak_attendees"),
                ("participants", "peak_participants"),
            ):
                v = c.get(field)
                if isinstance(v, int) and v > (slot.get(peak_key) or 0):
                    slot[peak_key] = v
                    slot[f"{peak_key}_at"] = now.isoformat()
            slot["last"] = {**c, "meeting_state": state, "at": now.isoformat()}
            print(
                f"[peak] {now.strftime('%H:%M:%S')} {s['id']} :{port} state={state} "
                f"now={c} peaks=footer:{slot['peak_footer']} att:{slot['peak_attendees']} part:{slot['peak_participants']}",
                flush=True,
            )
        data["updated_at"] = now.isoformat()
        OUT.write_text(json.dumps(data, indent=2))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
