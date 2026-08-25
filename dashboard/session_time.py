"""Shared validation for persisted IST session times."""
from __future__ import annotations


def normalize_session_time(value: str, *, allow_empty: bool = True) -> str:
    """Return a validated HH:MM:SS value from HH:MM, HH:MM:SS, or ISO input."""
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.split("+", 1)[0].split("Z", 1)[0].strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError("session time is required")
    parts = text.split(":")
    if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts):
        raise ValueError(f"invalid session time {value!r}; expected HH:MM or HH:MM:SS")
    if len(parts) == 2:
        parts.append("0")
    hour, minute, second = (int(p) for p in parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"invalid session time {value!r}; value is out of range")
    return f"{hour:02d}:{minute:02d}:{second:02d}"
