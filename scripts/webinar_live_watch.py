#!/usr/bin/env python3
"""Poll Hermes bridges every 10s after 18:45 IST for webinar go-live."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Kolkata")
START_WATCH = (18, 45)  # 6:45 PM IST
INTERVAL_SEC = 10

SLOTS = [
    (8765, "Vibe Coding", "81944769138"),
    (8766, "Solopreneur", "81627025299"),
    (8767, "Copilot", "81044354791"),
    (8768, "GPT", "85905783106"),
    (8769, "AI For Students", "89626609811"),
]


def now_ist() -> datetime:
    return datetime.now(TZ)


def health(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"status": "down", "error": str(e), "meeting_state": "down"}


def page_snip(port: int) -> str:
    try:
        body = json.dumps(
            {
                "code": "return (document.body && document.body.innerText || '').slice(0, 280);"
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/eval",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
            return (data.get("result") or "").replace("\n", " | ")[:200]
    except Exception:
        return ""


def main() -> None:
    print(
        f"[watch] started {now_ist().strftime('%H:%M:%S')} IST — "
        f"will poll every {INTERVAL_SEC}s after {START_WATCH[0]:02d}:{START_WATCH[1]:02d}",
        flush=True,
    )
    announced_live: set[int] = set()

    while True:
        n = now_ist()
        if (n.hour, n.minute) < START_WATCH:
            # Sleep until ~18:45, wake occasionally to log heartbeat
            target = n.replace(
                hour=START_WATCH[0], minute=START_WATCH[1], second=0, microsecond=0
            )
            wait = max(5, (target - n).total_seconds())
            print(
                f"[watch] {n.strftime('%H:%M:%S')} waiting until 18:45 "
                f"({int(wait)}s) …",
                flush=True,
            )
            time.sleep(min(wait, 60))
            continue

        line = [n.strftime("%H:%M:%S")]
        for port, name, mid in SLOTS:
            h = health(port)
            state = h.get("meeting_state") or "?"
            line.append(f"{port}:{state}")
            if state == "in_meeting" and port not in announced_live:
                snip = page_snip(port)
                print(
                    f"[watch] LIVE {name} (:{port} mid={mid}) — {snip}",
                    flush=True,
                )
                announced_live.add(port)
            elif state == "waiting" and port not in announced_live:
                # one-line hint first few times via state only
                pass

        print("[watch] " + " | ".join(line), flush=True)

        if len(announced_live) == len(SLOTS):
            print("[watch] all 5 sessions in_meeting — stopping watcher", flush=True)
            break

        # Stop overnight
        if n.hour >= 23:
            print("[watch] past 23:00 — stopping", flush=True)
            break

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
