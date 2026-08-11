#!/usr/bin/env python3
"""Watch sid.json and POST due chat rows to the local bridge (:8765)."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "sid.json"
BRIDGE = "http://127.0.0.1:8765/send_chat"
MEETING = "87264482000"


def load_rows(raw: str) -> list:
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        mid = str(data.get("meeting_id") or "").strip()
        if mid and mid != MEETING:
            print(f"[sid-watch] skip file meeting_id={mid} (want {MEETING})", flush=True)
            return []
        items = data.get("items") or data.get("schedule") or []
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        return items
    raise ValueError(f"unexpected schedule type: {type(data)}")


def normalize_time(s: str) -> str | None:
    s = (s or "").strip().lower().replace(" ", "")
    if not s:
        return None
    ampm = None
    if s.endswith("pm"):
        ampm = "pm"
        s = s[:-2]
    elif s.endswith("am"):
        ampm = "am"
        s = s[:-2]
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 1:
        h, m, sec = parts[0], 0, 0
    elif len(parts) == 2:
        h, m, sec = parts[0], parts[1], 0
    elif len(parts) == 3:
        h, m, sec = parts
    else:
        return None
    now = datetime.now()
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif ampm is None and h < 12 and now.hour >= 12:
        h += 12
    return f"{h:02d}:{m:02d}:{sec:02d}"


def send(text: str) -> bool:
    data = json.dumps({"text": text, "to": "everyone"}).encode()
    req = urllib.request.Request(
        BRIDGE, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(
                f"[sid-watch] SENT {datetime.now().strftime('%H:%M:%S')}: "
                f"{text[:70]!r} -> {r.read().decode()}",
                flush=True,
            )
            return True
    except Exception as e:
        print(f"[sid-watch] send failed: {e}", flush=True)
        return False


def main() -> None:
    sent: set[tuple[str, str]] = set()
    print("[sid-watch] watching sid.json → :8765", flush=True)
    last_mtime = None
    boot = True
    while True:
        try:
            mtime = PATH.stat().st_mtime
            if last_mtime != mtime:
                last_mtime = mtime
                print(
                    f"[sid-watch] sid.json updated @ {datetime.now().strftime('%H:%M:%S')}",
                    flush=True,
                )
            rows = load_rows(PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[sid-watch] read error: {e}", flush=True)
            time.sleep(2)
            continue

        now = datetime.now()
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_t = str(row.get("time") or "").strip()
            text = str(row.get("text") or "").strip()
            if not raw_t or not text:
                continue
            try:
                norm = normalize_time(raw_t)
                if not norm:
                    continue
                h, m, s = [int(x) for x in norm.split(":")]
                target = now.replace(hour=h, minute=m, second=s, microsecond=0)
            except Exception as e:
                print(f"[sid-watch] bad time {raw_t!r}: {e}", flush=True)
                continue
            key = (norm, text)
            if key in sent:
                continue
            delta = (now - target).total_seconds()
            if boot and delta > 180:
                sent.add(key)
                continue
            if -5 <= delta <= 180:
                print(f"[sid-watch] due {norm} (delta={delta:.0f}s)", flush=True)
                if send(text):
                    sent.add(key)
            elif delta > 180:
                print(f"[sid-watch] skip old {norm}: {text[:40]!r}", flush=True)
                sent.add(key)
        boot = False
        time.sleep(2)


if __name__ == "__main__":
    main()
