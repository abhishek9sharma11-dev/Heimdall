"""Persist per-session metrics (payment-link drop attendees, etc.)."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Kolkata")
OUT = Path("/tmp/hermes-payment-drop-attendees.json")
CHAT_OUT = Path("/tmp/hermes-chat-drops.json")
_lock = threading.Lock()

# First payment CTA = first scheduled chat containing an Outskill short payment link.
PAYMENT_LINK_RE = re.compile(r"https?://link\.outskill\.com/[^\s<>\"']+", re.I)


def is_payment_link_message(text: str) -> bool:
    return bool(PAYMENT_LINK_RE.search(text or ""))


def extract_payment_url(text: str) -> Optional[str]:
    m = PAYMENT_LINK_RE.search(text or "")
    return m.group(0).rstrip(").,;") if m else None


def best_attendees_count(counts: dict[str, Any]) -> Optional[int]:
    for key in ("attendees", "footer", "participants"):
        v = counts.get(key)
        if isinstance(v, int) and v >= 0:
            return v
    return None


def _load() -> dict[str, Any]:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text())
        except Exception:
            pass
    return {
        "tag": "payment_link_drop_attendees_count",
        "updated_at": None,
        "sessions": {},
    }


def record_payment_drop(
    *,
    session_id: str,
    meeting_id: str = "",
    port: int | None = None,
    schedule_time: str = "",
    payment_url: str = "",
    message_preview: str = "",
    counts: dict[str, Any] | None = None,
    source: str = "unknown",
) -> dict[str, Any]:
    """Record attendees at first payment-link drop. Idempotent per session_id."""
    counts = counts or {}
    with _lock:
        data = _load()
        sessions = data.setdefault("sessions", {})
        if session_id in sessions and sessions[session_id].get("attendees_count") is not None:
            return sessions[session_id]
        now = datetime.now(TZ)
        entry = {
            "tag": "payment_link_drop_attendees_count",
            "session_id": session_id,
            "meeting_id": meeting_id,
            "port": port,
            "schedule_time": schedule_time,
            "dropped_at": now.isoformat(),
            "payment_url": payment_url or extract_payment_url(message_preview) or "",
            "message_preview": (message_preview or "")[:160].replace("\n", " "),
            "attendees_count": best_attendees_count(counts),
            "footer": counts.get("footer"),
            "attendees": counts.get("attendees"),
            "participants": counts.get("participants"),
            "source": source,
        }
        sessions[session_id] = entry
        data["updated_at"] = now.isoformat()
        OUT.write_text(json.dumps(data, indent=2) + "\n")
        log.info(
            "payment_link_drop_attendees_count session=%s count=%s time=%s",
            session_id,
            entry["attendees_count"],
            schedule_time,
        )
        return entry


def record_chat_drop(
    *,
    session_id: str,
    meeting_id: str = "",
    port: int | None = None,
    schedule_time: str = "",
    text: str = "",
    source: str = "scheduler",
) -> dict[str, Any]:
    """Append one fired scheduled chat/poll row (daily chat-drop log)."""
    with _lock:
        data: dict[str, Any]
        if CHAT_OUT.exists():
            try:
                data = json.loads(CHAT_OUT.read_text())
            except Exception:
                data = {"drops": []}
        else:
            data = {"drops": []}
        now = datetime.now(TZ)
        entry = {
            "session_id": session_id,
            "meeting_id": meeting_id,
            "port": port,
            "schedule_time": schedule_time,
            "fired_at": now.isoformat(),
            "is_payment": is_payment_link_message(text),
            "preview": (text or "")[:160].replace("\n", " "),
            "source": source,
        }
        data.setdefault("drops", []).append(entry)
        data["updated_at"] = now.isoformat()
        # keep file bounded
        if len(data["drops"]) > 5000:
            data["drops"] = data["drops"][-4000:]
        CHAT_OUT.write_text(json.dumps(data, indent=2) + "\n")
        return entry


def first_payment_from_schedule(path: Path) -> Optional[dict[str, str]]:
    """Return {time, text, url} for earliest payment-link row in a schedule file."""
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return None
    items = doc.get("items") or []
    rows = []
    for it in items:
        text = it.get("text") or ""
        if not is_payment_link_message(text):
            continue
        t = it.get("time") or ""
        if not t:
            continue
        rows.append((t, text))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    t, text = rows[0]
    return {"time": t, "text": text, "url": extract_payment_url(text) or ""}
