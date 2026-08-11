#!/usr/bin/env python3
"""Print payment_link_drop_attendees_count for all sessions today."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path("/tmp/hermes-payment-drop-attendees.json")
MANIFEST = Path(__file__).resolve().parents[1] / "schedules" / "today_sessions.json"


def main() -> None:
    data = json.loads(OUT.read_text()) if OUT.exists() else {"sessions": {}}
    sessions = data.get("sessions") or {}
    planned = []
    if MANIFEST.exists():
        planned = json.loads(MANIFEST.read_text()).get("sessions") or []

    print("tag: payment_link_drop_attendees_count")
    print(f"file: {OUT}")
    print(f"updated_at: {data.get('updated_at')}")
    print()
    print(f"{'session':<32} {'sched':>8} {'count':>7}  url")
    print("-" * 90)
    seen = set()
    for s in planned:
        sid = s["id"]
        seen.add(sid)
        e = sessions.get(sid)
        if e:
            print(
                f"{sid:<32} {e.get('schedule_time') or '—':>8} "
                f"{str(e.get('attendees_count')):>7}  {(e.get('payment_url') or '')[:50]}"
            )
        else:
            print(f"{sid:<32} {'pending':>8} {'—':>7}")
    for sid, e in sessions.items():
        if sid in seen:
            continue
        print(
            f"{sid:<32} {e.get('schedule_time') or '—':>8} "
            f"{str(e.get('attendees_count')):>7}  {(e.get('payment_url') or '')[:50]}"
        )


if __name__ == "__main__":
    main()
