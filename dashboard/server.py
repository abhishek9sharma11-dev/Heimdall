"""
Hermes Mission Control — multi-slot bridge health + live payments (DATABASE_URL).

Usage:
  # DATABASE_URL in repo .env, then:
  .venv/bin/python -m dashboard.server
  # open http://127.0.0.1:8780
"""
from __future__ import annotations

import json
import csv
import io
import os
import re
import threading
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import urllib.parse
from zoneinfo import ZoneInfo

from . import payments_db
from .payments_db import format_totals_by_currency
from .schedule_upload import parse_upload, write_schedule
import subprocess
import shlex
import base64
import urllib.request
import sys

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
STATIC = ROOT / "static"
SLOTS_PATH = ROOT / "slots.json"
PAYMENTS_PATH = ROOT / "payments.json"
COHORTS_PATH = ROOT / "cohorts.json"
PEAK_PATH = Path("/tmp/hermes-peak-attendees.json")
PAY_DROP_PATH = Path("/tmp/hermes-payment-drop-attendees.json")
TZ = ZoneInfo("Asia/Kolkata")
HOST = os.environ.get("HERMES_HOST") or "127.0.0.1"
PORT = int(os.environ.get("HERMES_PORT") or "8780")

_payments_lock = threading.Lock()
_cohorts_lock = threading.Lock()
_bot_procs_lock = threading.Lock()
# mapping: session_key -> info dict for either subprocess or docker-launched session
_bot_procs: dict[str, dict[str, Any]] = {}
_PLAYWRIGHT_PORTS = tuple(range(8765, 8778))
_zoom_registration_cache: dict[str, tuple[float, int | None, str | None]] = {}
_ZOOM_REGISTRATION_CACHE_SEC = 300
_TRACKER_EVAL = r'''var t=(document.body&&document.body.innerText)||'';var a=null;var m=t.match(/Attendees\s*\((\d+)\)/i);if(m)a=parseInt(m[1],10);var p=t.match(/Participants\s*\((\d+)\)/i);return {attendees:a,participants:p?parseInt(p[1],10):null};'''


def _pick_playwright_port() -> int:
    """Pick an unused local port for a visible Playwright bridge."""
    import urllib.error
    import urllib.request

    used = {
        int(entry.get('bridge_port'))
        for entry in _bot_procs.values()
        if entry.get('bridge_port')
    }
    for port in _PLAYWRIGHT_PORTS:
        if port in used:
            continue
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=0.25)
        except (urllib.error.URLError, TimeoutError, OSError):
            return port
    raise RuntimeError('no free Playwright bridge port (8765-8775)')


def _load_dotenv() -> None:
    """Lightweight .env loader (does not override existing env)."""
    for path in (REPO / ".env", ROOT / ".env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def _load_slots() -> list[dict[str, Any]]:
    slots = json.loads(SLOTS_PATH.read_text())
    out: list[dict[str, Any]] = []
    for s in slots:
        if s.get("enabled") is False:
            continue
        mid = str(s.get("meeting_id") or "")
        if not mid or mid.upper() == "TODO":
            continue
        out.append(s)

    # A newly uploaded session schedule is useful dashboard state even before
    # someone adds a permanent slot definition for it. Discover those files so
    # scheduled workshops are visible immediately.
    known_ids = {str(s.get("meeting_id") or "") for s in out}
    schedule_dir = REPO / "schedules"
    if schedule_dir.exists():
        for path in sorted(schedule_dir.glob("*.json")):
            try:
                sched = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            mid = str(sched.get("meeting_id") or "")
            if not mid or mid in known_ids or not sched.get("session_start_ist"):
                continue
            out.append({
                "id": re.sub(r"[^A-Za-z0-9_-]+", "-", mid).strip("-").lower() or "scheduled-session",
                "account": "Scheduled",
                "account_key": "scheduled",
                "port": 0,
                "meeting_id": mid,
                "env_file": "__scheduled__.env",
                "schedule_file": str(path.relative_to(REPO)),
                "mode": "simulive",
                "icon": "videocam",
                "enabled": True,
            })
            known_ids.add(mid)
    return out


def _load_payments() -> dict[str, Any]:
    if not PAYMENTS_PATH.exists():
        return {}
    try:
        return json.loads(PAYMENTS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_payments(data: dict[str, Any]) -> None:
    with _payments_lock:
        PAYMENTS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _load_cohorts() -> dict[str, Any]:
    if not COHORTS_PATH.exists():
        return {}
    try:
        return json.loads(COHORTS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_cohorts(data: dict[str, Any]) -> None:
    with _cohorts_lock:
        COHORTS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def _metrics_for_slot(session_id: str) -> dict[str, Any]:
    """Peak + retention from day_ops trackers; cohort_id from cohorts.json / sheet sync."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    peak: int | None = None
    retention: int | None = None
    last_count: int | None = None

    if PEAK_PATH.exists():
        try:
            peak_data = json.loads(PEAK_PATH.read_text())
        except json.JSONDecodeError:
            peak_data = {}
        dated = None
        legacy = None
        for key, slot in (peak_data.get("slots") or {}).items():
            if not isinstance(slot, dict) or slot.get("id") != session_id:
                continue
            if str(key).startswith(f"{today}:") or slot.get("date") == today:
                dated = slot
                break
            peak_at = (
                slot.get("peak_footer_at")
                or slot.get("peak_attendees_at")
                or ((slot.get("last") or {}).get("at") or "")
            )
            if str(peak_at).startswith(today):
                legacy = slot
        best = dated or legacy
        if best:
            for k in ("peak_footer", "peak_attendees", "peak_participants"):
                v = best.get(k)
                if isinstance(v, int) and v > 0:
                    peak = v if peak is None else max(peak, v)
            last = best.get("last") or {}
            for k in ("footer", "attendees", "participants"):
                v = last.get(k)
                if isinstance(v, int) and v >= 0:
                    last_count = v
                    break

    if PAY_DROP_PATH.exists():
        try:
            drop = json.loads(PAY_DROP_PATH.read_text())
        except json.JSONDecodeError:
            drop = {}
        sessions = drop.get("sessions") or {}
        sess = sessions.get(f"{today}:{session_id}")
        if sess is None:
            legacy = sessions.get(session_id)
            if legacy and str(legacy.get("dropped_at") or "").startswith(today):
                sess = legacy
        if sess and sess.get("attendees_count") is not None:
            try:
                retention = int(sess["attendees_count"])
            except (TypeError, ValueError):
                retention = None

    cohorts = _load_cohorts()
    entry = cohorts.get(session_id) or {}
    cohort_id = str(entry.get("cohort_id") or "").strip() or None

    return {
        "peak_attendees": peak,
        "retention": retention,
        "last_attendees": last_count,
        "cohort_id": cohort_id,
    }


def _sync_cohort_from_sheet(session_id: str, meeting_id: str | None = None) -> str | None:
    """Best-effort: read 'Cohort id' from Workshops sheet tab when present.

    Does not fail the dashboard if Sheets is unavailable. Returns cohort_id or None.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        return None

    creds_path = Path.home() / ".config/hermes/sheets.json"
    if not creds_path.is_file():
        return None

    # Map session → sheet tab title (must match Workshops workbook)
    TAB_BY_SESSION = {
        "intl-claude-uk-220pm": "INTL Claude UK (2:20PM)",
        "claude-421pm": "Claude (4:21PM)",
        "claude-99dollars-651pm": "Claude 99$(6:51pm)",
        "claude-199dollars-651pm": "Claude 199$(6:51pm)",
        "gpt-650pm": "GPT (6:50PM)",
        "gpt-wp-650pm": "GPT (WP) (6:50PM)",
        "vibe-coding-651pm": "Vibe Coding (6:51PM) ",
        "solopreneur-652pm": "Solopreneur (6:52PM)",
        "copilot-652pm": "Copilot (6:52PM)",
    }
    tab = TAB_BY_SESSION.get(session_id)
    if not tab:
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(creds_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        sheet_id = os.environ.get(
            "GOOGLE_SHEETS_ID", "1Ykx3m5O9H07iipM-pE7um6ED1Lh0dCSok2iqaOmq4dw"
        )
        vals = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!A1:B60")
            .execute()
            .get("values", [])
        )
    except Exception:
        return None

    cohort: str | None = None
    for r in vals:
        a = (r[0] if r else "") or ""
        b = (r[1] if len(r) > 1 else "") or ""
        if re.search(r"cohort\s*id", a, re.I) or re.search(r"cohort\s*id", b, re.I):
            # value may be in B, or next non-empty cell
            cand = b.strip() if b.strip() and not re.search(r"cohort", b, re.I) else ""
            if not cand:
                # look ahead one row
                continue
            cohort = cand
            break
        if re.search(r"cohort\s*id", a, re.I) and not b.strip():
            # UUID/id on following rows — take first non-empty
            idx = vals.index(r)
            for nxt in vals[idx + 1 : idx + 4]:
                for c in nxt:
                    c = (c or "").strip()
                    if c and not re.search(r"session\s*end|payment", c, re.I):
                        cohort = c
                        break
                if cohort:
                    break
            break

    if cohort:
        store = _load_cohorts()
        prev = store.get(session_id) or {}
        if prev.get("cohort_id") != cohort:
            store[session_id] = {
                **prev,
                "cohort_id": cohort,
                "meeting_id": meeting_id,
                "source": "google_sheet",
                "updated_at": datetime.now(TZ).isoformat(),
            }
            _save_cohorts(store)
    return cohort


def _read_env_kv(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_schedule(rel: str) -> dict[str, Any]:
    path = REPO / rel
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _effective_schedule(
    slot: dict[str, Any], sched: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """Use slot timing as the dashboard fallback before a schedule is uploaded."""
    if sched.get("session_start_ist") or not slot.get("session_start_ist"):
        return sched
    # A bare slot start is only enough to show a future scheduled card. Once
    # that time has passed, do not pretend an old slot is still live without a
    # real schedule/end time.
    if now is not None:
        try:
            sh, sm, ss = [int(x) for x in str(slot["session_start_ist"]).split(":")]
            start = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
            if now >= start:
                return sched
        except (ValueError, AttributeError):
            return sched
    merged = dict(sched)
    merged["session_start_ist"] = slot["session_start_ist"]
    if slot.get("session_end_ist") and not merged.get("session_end_ist"):
        merged["session_end_ist"] = slot["session_end_ist"]
    return merged


def _bridge_health(port: int) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=1.2) as resp:
            data = json.loads(resp.read().decode())
            return {
                "online": True,
                "in_meeting": bool(data.get("in_meeting")),
                "meeting_state": data.get("meeting_state", "unknown"),
                "has_page": bool(data.get("has_page")),
                "reconnecting": bool(data.get("reconnecting")),
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {
            "online": False,
            "in_meeting": False,
            "meeting_state": "offline",
            "has_page": False,
            "reconnecting": False,
        }


def _session_phase(sched: dict[str, Any], now: datetime) -> str:
    start = sched.get("session_start_ist")
    end = sched.get("session_end_ist")
    if not start:
        return "unknown"
    try:
        sh, sm, ss = [int(x) for x in start.split(":")]
        start_t = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
        if end:
            eh, em, es = [int(x) for x in end.split(":")]
            end_t = now.replace(hour=eh, minute=em, second=es, microsecond=0)
        else:
            end_t = None
    except (ValueError, AttributeError):
        return "unknown"

    if now < start_t:
        return "scheduled"
    if end_t and now >= end_t:
        return "ended"
    return "live"


def _elapsed_label(sched: dict[str, Any], now: datetime, phase: str) -> str:
    start = sched.get("session_start_ist")
    if not start:
        return ""
    try:
        sh, sm, ss = [int(x) for x in start.split(":")]
        start_t = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
    except (ValueError, AttributeError):
        return ""
    if phase == "scheduled":
        return f"Scheduled: {start[:5]} IST"
    if phase == "ended":
        return "Session ended"
    mins = max(0, int((now - start_t).total_seconds() // 60))
    if mins < 60:
        return f"Started {mins}m ago"
    h, m = divmod(mins, 60)
    return f"Started {h}h {m}m ago"


def _default_payment_cfg(slot_id: str) -> dict[str, Any]:
    today = date.today().isoformat()
    return {
        "payment_link_ids": "",
        "payment_link_label": "",
        "start_date": today,
        "end_date": today,
        "currency": "",
    }


def _slot_revenue(slot_id: str, pay_cfg: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    ids = pay_cfg.get("payment_link_ids") or ""
    if isinstance(ids, list):
        ids_str = ",".join(str(x) for x in ids)
    else:
        ids_str = str(ids).strip()
    if not ids_str:
        return {
            "ok": False,
            "configured": payments_db.configured(),
            "count": 0,
            "total_amount": 0,
            "currency": pay_cfg.get("currency") or "INR",
            "payments": [],
            "error": "Add a paymentLink UUID to track revenue",
            "pending_config": True,
        }
    return payments_db.fetch_payments(
        ids_str,
        pay_cfg.get("start_date") or date.today().isoformat(),
        pay_cfg.get("end_date") or date.today().isoformat(),
        pay_cfg.get("currency") or "",
        force=force,
    )


def _zoom_registrations(meeting_id: str) -> tuple[int | None, str | None]:
    """Return webinar registration count when Zoom Server-to-Server OAuth is configured.

    Meeting SDK credentials and the panelist link cannot read registrants. The optional
    OAuth credentials are deliberately required so the dashboard never reports a guess.
    """
    override = os.environ.get(f"ZOOM_REGISTRATIONS_{meeting_id}")
    if override and override.isdigit():
        return int(override), "env_override"
    account = os.environ.get("ZOOM_OAUTH_ACCOUNT_ID")
    client = os.environ.get("ZOOM_OAUTH_CLIENT_ID")
    secret = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET")
    if not (account and client and secret):
        return None, "Configure ZOOM_OAUTH_ACCOUNT_ID, ZOOM_OAUTH_CLIENT_ID and ZOOM_OAUTH_CLIENT_SECRET"
    import time
    cached = _zoom_registration_cache.get(meeting_id)
    if cached and time.time() - cached[0] < _ZOOM_REGISTRATION_CACHE_SEC:
        return cached[1], cached[2]
    try:
        token_req = urllib.request.Request(
            "https://zoom.us/oauth/token?grant_type=account_credentials&account_id=" + urllib.parse.quote(account),
            method="POST",
            headers={"Authorization": "Basic " + base64.b64encode(f"{client}:{secret}".encode()).decode()},
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token = json.loads(resp.read().decode())["access_token"]
        total = 0
        next_token = ""
        while True:
            url = f"https://api.zoom.us/v2/webinars/{urllib.parse.quote(meeting_id)}/registrants?page_size=300"
            if next_token:
                url += "&next_page_token=" + urllib.parse.quote(next_token)
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            total += len(data.get("registrants") or [])
            next_token = data.get("next_page_token") or ""
            if not next_token:
                break
        result = (total, None)
    except Exception as exc:  # best-effort dashboard metric
        result = (None, f"Zoom registrations unavailable: {exc}")
    _zoom_registration_cache[meeting_id] = (time.time(), result[0], result[1])
    return result


def _report_for_slot(slot: dict[str, Any], sched: dict[str, Any], metrics: dict[str, Any], revenue: dict[str, Any]) -> dict[str, Any]:
    payments = revenue.get("payments") or []
    currency_counts: dict[str, int] = {}
    for payment in payments:
        code = str(payment.get("currency") or "").upper()
        if code:
            currency_counts[code] = currency_counts.get(code, 0) + 1
    registrations, registration_error = _zoom_registrations(str(slot["meeting_id"]))
    return {
        "date": datetime.now(TZ).strftime("%Y-%m-%d"),
        "workshop": sched.get("session") or slot["id"],
        "peak_showup": metrics.get("peak_attendees"),
        "retention": metrics.get("retention"),
        "inr": currency_counts.get("INR") if revenue.get("ok") else None,
        "usd": currency_counts.get("USD") if revenue.get("ok") else None,
        "total_payments": int(revenue.get("count") or 0) if revenue.get("ok") else None,
        "zoom_registrations": registrations,
        "zoom_registrations_error": registration_error,
    }


def _tracker_counts(port: int) -> dict[str, Any]:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/eval",
            data=json.dumps({"code": _TRACKER_EVAL}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode()).get("result") or {}
    except Exception:
        return {}


def _metrics_tracker_loop() -> None:
    """Continuously collect first-hour peak and payment-drop retention for dashboard slots."""
    import time
    while True:
        try:
            now = datetime.now(TZ)
            day = now.strftime("%Y-%m-%d")
            peak_data = json.loads(PEAK_PATH.read_text()) if PEAK_PATH.exists() else {"slots": {}}
            peak_data.setdefault("slots", {})
            drop_data = json.loads(PAY_DROP_PATH.read_text()) if PAY_DROP_PATH.exists() else {"sessions": {}}
            drop_data.setdefault("sessions", {})
            changed_peak = changed_drop = False
            for slot in _load_slots():
                sched = _load_schedule(slot.get("schedule_file", ""))
                start = sched.get("session_start_ist")
                health = _bridge_health(int(slot["port"]))
                if not start and not health.get("in_meeting"):
                    continue
                try:
                    if start:
                        h, m, s = [int(x) for x in start.split(":")]
                        start_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                    else:
                        # Ad-hoc dashboard sessions may not have a CSV yet. Start
                        # their first-hour measurement window when first observed live.
                        start_dt = None
                except (ValueError, AttributeError):
                    continue
                counts = _tracker_counts(int(slot["port"]))
                if not counts:
                    continue
                key = f"{day}:{slot['port']}:{slot['id']}"
                item = peak_data["slots"].setdefault(key, {"id": slot["id"], "meeting_id": slot["meeting_id"], "date": day, "last": {}})
                if start_dt is None:
                    raw_tracker_start = item.get("tracker_started_at")
                    try:
                        start_dt = datetime.fromisoformat(raw_tracker_start) if raw_tracker_start else now
                    except (TypeError, ValueError):
                        start_dt = now
                    if not raw_tracker_start:
                        item["tracker_started_at"] = now.isoformat()
                        changed_peak = True
                item["last"] = {**counts, "at": now.isoformat()}
                if start_dt <= now <= start_dt + timedelta(minutes=60):
                    for field in ("attendees", "participants"):
                        value = counts.get(field)
                        target = f"peak_{field}"
                        if isinstance(value, int) and value > int(item.get(target) or 0):
                            item[target] = value
                            item[f"{target}_at"] = now.isoformat()
                            changed_peak = True
                fp = next((x for x in sorted(sched.get("items") or [], key=lambda x: x.get("time", "")) if "link.outskill.com" in (x.get("text") or "")), None)
                drop_key = f"{day}:{slot['id']}"
                if fp and drop_key not in drop_data["sessions"]:
                    try:
                        dh, dm, ds = [int(x) for x in fp["time"].split(":")]
                        drop_dt = now.replace(hour=dh, minute=dm, second=ds, microsecond=0)
                        if abs((now - drop_dt).total_seconds()) <= 90:
                            value = counts.get("attendees")
                            if isinstance(value, int):
                                drop_data["sessions"][drop_key] = {"session_id": slot["id"], "date": day, "schedule_time": fp["time"], "dropped_at": now.isoformat(), "attendees_count": value, "source": "dashboard_tracker"}
                                changed_drop = True
                    except (ValueError, AttributeError):
                        pass
            if changed_peak:
                peak_data["updated_at"] = now.isoformat()
                PEAK_PATH.write_text(json.dumps(peak_data, indent=2))
            if changed_drop:
                drop_data["updated_at"] = now.isoformat()
                PAY_DROP_PATH.write_text(json.dumps(drop_data, indent=2) + "\n")
        except Exception:
            pass
        time.sleep(10)


def build_slot_detail(slot_id: str, *, force_payments: bool = True) -> dict[str, Any] | None:
    """Full session payload for the payments detail page."""
    now = datetime.now(TZ)
    slot_def = next((s for s in _load_slots() if s["id"] == slot_id), None)
    if not slot_def:
        return None

    env = _read_env_kv(REPO / slot_def["env_file"])
    sched = _effective_schedule(slot_def, _load_schedule(slot_def["schedule_file"]), now)
    health = _bridge_health(int(slot_def["port"]))
    phase = _session_phase(sched, now)
    pay_cfg = {**_default_payment_cfg(slot_id), **(_load_payments().get(slot_id) or {})}
    revenue = _slot_revenue(slot_id, pay_cfg, force=force_payments)
    metrics = _metrics_for_slot(slot_id)
    report = _report_for_slot(slot_def, sched, metrics, revenue)
    answer_qs = env.get("ANSWER_QUESTIONS", "true").lower() in ("1", "true", "yes")

    return {
        "ok": True,
        "bot": "Hermes",
        "database": {
            "configured": payments_db.configured(),
            "label": payments_db.db_label(),
        },
        "slot": {
            "id": slot_def["id"],
            "account": slot_def["account"],
            "port": slot_def["port"],
            "meeting_id": slot_def["meeting_id"],
            "session": sched.get("session") or slot_def["id"],
            "session_start_ist": sched.get("session_start_ist", ""),
            "session_end_ist": sched.get("session_end_ist", ""),
            "mode": slot_def.get("mode", "simulive"),
            "icon": slot_def.get("icon", "videocam"),
            "bot_name": env.get("BOT_DISPLAY_NAME") or "Hermes AI",
            "auto_chat": answer_qs,
            "phase": phase,
            "elapsed": _elapsed_label(sched, now, phase),
            "bot_health": 0 if phase == "ended" else (
                100
                if health["online"] and not health["reconnecting"]
                else (50 if health["online"] else 0)
            ),
            "health": health,
            "schedule_items": len(sched.get("items") or []),
            "report": report,
        },
        "payment": pay_cfg,
        "revenue": {
            "ok": bool(revenue.get("ok")),
            "count": revenue.get("count") or 0,
            "total_amount": revenue.get("total_amount") or 0,
            "totals_by_currency": revenue.get("totals_by_currency") or {},
            "total_display": revenue.get("total_display")
            or format_totals_by_currency(revenue.get("totals_by_currency") or {}),
            "currency": revenue.get("currency") or "",
            "error": revenue.get("error"),
            "pending_config": bool(revenue.get("pending_config")),
            "payments": revenue.get("payments") or [],
            "diagnostic": revenue.get("diagnostic"),
        },
    }


def format_inr_compact(amount: float | None) -> str:
    if amount is None:
        return "—"
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return "—"
    if n >= 100000:
        return f"{n / 100000:.1f}".rstrip("0").rstrip(".") + "L"
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n:.0f}" if n == int(n) else f"{n:.2f}"


def build_status(*, force_payments: bool = False) -> dict[str, Any]:
    now = datetime.now(TZ)
    payments_store = _load_payments()
    slots_out: list[dict[str, Any]] = []
    live_count = 0
    bots_online = 0
    peak_attendees = 0
    revenue_by_currency: dict[str, float] = {}
    revenue_count = 0
    revenue_ready = False

    db_ok = payments_db.configured()

    for slot in _load_slots():
        env = _read_env_kv(REPO / slot["env_file"])
        sched = _effective_schedule(slot, _load_schedule(slot["schedule_file"]), now)
        health = _bridge_health(int(slot["port"]))
        phase = _session_phase(sched, now)

        if health["online"]:
            bots_online += 1
        if phase != "ended" and (health["in_meeting"] or phase == "live"):
            live_count += 1

        attendees = 0
        metrics = _metrics_for_slot(slot["id"])
        # Prefer live last sample from peak tracker; fall back to placeholder only if unknown
        if metrics.get("last_attendees") is not None:
            attendees = int(metrics["last_attendees"])
        elif health["in_meeting"]:
            attendees = 0
        if metrics.get("peak_attendees"):
            peak_attendees = max(peak_attendees, int(metrics["peak_attendees"]))
        else:
            peak_attendees = max(peak_attendees, attendees)

        bot_name = env.get("BOT_DISPLAY_NAME") or "Hermes AI"
        answer_qs = env.get("ANSWER_QUESTIONS", "true").lower() in ("1", "true", "yes")

        status_label = "offline"
        if phase == "ended":
            status_label = "ended"
        elif health["online"] and health["in_meeting"]:
            status_label = "live" if phase != "ended" else "wrapping"
        elif health["online"]:
            status_label = health["meeting_state"]
        elif phase == "scheduled":
            status_label = "scheduled"

        pay_cfg = {**_default_payment_cfg(slot["id"]), **(payments_store.get(slot["id"]) or {})}
        revenue = _slot_revenue(slot["id"], pay_cfg, force=force_payments)
        report = _report_for_slot(slot, sched, metrics, revenue)
        if revenue.get("ok"):
            revenue_ready = True
            revenue_count += int(revenue.get("count") or 0)
            for code, amt in (revenue.get("totals_by_currency") or {}).items():
                try:
                    revenue_by_currency[code] = round(
                        revenue_by_currency.get(code, 0.0) + float(amt), 2
                    )
                except (TypeError, ValueError):
                    pass

        slot_display = revenue.get("total_display") or format_totals_by_currency(
            revenue.get("totals_by_currency") or {}
        )
        slots_out.append(
            {
                "id": slot["id"],
                "account": slot["account"],
                "port": slot["port"],
                "meeting_id": slot["meeting_id"],
                "session": sched.get("session") or slot["id"],
                "session_start_ist": sched.get("session_start_ist", ""),
                "session_end_ist": sched.get("session_end_ist", ""),
                "mode": slot.get("mode", "simulive"),
                "icon": slot.get("icon", "videocam"),
                "bot_name": bot_name,
                "answer_questions": answer_qs,
                "auto_chat": answer_qs,
                "phase": phase,
                "elapsed": _elapsed_label(sched, now, phase),
                "attendees": attendees,
                "peak_attendees": metrics.get("peak_attendees"),
                "retention": metrics.get("retention"),
                "report": report,
                "report_url": f"/api/slots/{slot['id']}/report.csv",
                "cohort_id": metrics.get("cohort_id"),
                "bot_health": 0 if phase == "ended" else (
                    100
                    if health["online"] and not health["reconnecting"]
                    else (50 if health["online"] else 0)
                ),
                "status": status_label,
                "health": health,
                "schedule_items": len(sched.get("items") or []),
                "payment": pay_cfg,
                "revenue": {
                    "ok": bool(revenue.get("ok")),
                    "count": revenue.get("count") or 0,
                    "total_amount": revenue.get("total_amount") or 0,
                    "totals_by_currency": revenue.get("totals_by_currency") or {},
                    "total_display": slot_display if revenue.get("ok") else "—",
                    "currency": revenue.get("currency") or "",
                    "error": revenue.get("error"),
                    "pending_config": bool(revenue.get("pending_config")),
                    "recent": (revenue.get("payments") or [])[:8],
                },
            }
        )

    return {
        "bot": "Hermes",
        "system_online": bots_online > 0,
        "now_ist": now.strftime("%H:%M:%S"),
        "database": {
            "configured": db_ok,
            "label": payments_db.db_label(),
        },
        # backward-compat for older UI
        "metabase": {
            "configured": db_ok,
            "url": payments_db.db_label(),
            "database_id": None,
        },
        "metrics": {
            "live_sessions": live_count,
            "bots_connected": bots_online,
            "bots_total": len(slots_out),
            "todays_revenue": revenue_by_currency if revenue_ready else None,
            "todays_revenue_display": format_totals_by_currency(revenue_by_currency)
            if revenue_ready
            else "—",
            "payment_count": revenue_count,
            "peak_attendees": peak_attendees,
        },
        "slots": slots_out,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        if args and isinstance(args[0], str) and re.search(r" 5\d\d ", fmt % args):
            super().log_message(fmt, *args)

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode() or "{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/status":
            force = qs.get("force", ["0"])[0] in ("1", "true", "yes")
            return self._json(200, build_status(force_payments=force))

        if path == "/api/payments":
            return self._json(200, _load_payments())

        m = re.fullmatch(r"/api/slots/([A-Za-z0-9_-]+)", path)
        if m:
            slot_id = m.group(1)
            force = qs.get("force", ["1"])[0] in ("1", "true", "yes")
            detail = build_slot_detail(slot_id, force_payments=force)
            if not detail:
                return self._json(404, {"ok": False, "error": f"unknown slot {slot_id}"})
            return self._json(200, detail)

        m = re.fullmatch(r"/api/slots/([A-Za-z0-9_-]+)/report\.csv", path)
        if m:
            slot_id = m.group(1)
            slot_def = next((s for s in _load_slots() if s["id"] == slot_id), None)
            if not slot_def:
                return self._json(404, {"ok": False, "error": f"unknown slot {slot_id}"})
            env = _read_env_kv(REPO / slot_def["env_file"])
            sched = _load_schedule(slot_def["schedule_file"])
            pay_cfg = {**_default_payment_cfg(slot_id), **(_load_payments().get(slot_id) or {})}
            revenue = _slot_revenue(slot_id, pay_cfg, force=True)
            report = _report_for_slot(slot_def, sched, _metrics_for_slot(slot_id), revenue)
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            writer.writerow(["Date", "Workshop", "Peak Showup", "Retention", "INR", "USD", "Total Payments", "Zoom Registrations"])
            writer.writerow([
                report["date"], report["workshop"], report["peak_showup"] if report["peak_showup"] is not None else "",
                report["retention"] if report["retention"] is not None else "", report["inr"] if report["inr"] is not None else "",
                report["usd"] if report["usd"] is not None else "", report["total_payments"] if report["total_payments"] is not None else "",
                report["zoom_registrations"] if report["zoom_registrations"] is not None else "",
            ])
            body = out.getvalue().encode()
            filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{report['workshop']}-{report['date']}-report.csv")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()

        if path == "/session" or path == "/session.html":
            self.path = "/session.html"
            return super().do_GET()

        if path == "/connect" or path == "/connect.html":
            self.path = "/connect.html"
            return super().do_GET()

        if path == '/api/bot/status':
            # optional query param: ?session_key=...
            sess = qs.get('session_key', [None])[0]
            out = {}
            with _bot_procs_lock:
                if sess:
                    entry = _bot_procs.get(sess)
                    if not entry:
                        out = {"ok": True, "session_key": sess, "running": False}
                    elif entry.get('type') == 'proc':
                        p = entry.get('proc')
                        out = {"ok": True, "session_key": sess, "running": bool(p and p.poll() is None), "pid": getattr(p, 'pid', None)}
                    elif entry.get('type') == 'docker':
                        project = entry.get('project')
                        try:
                            r = subprocess.run(['docker', 'compose', '-p', project, 'ps'], cwd=str(REPO), capture_output=True, text=True, timeout=20)
                            out = {"ok": True, "session_key": sess, "running": (r.returncode == 0 and 'Exit' not in r.stdout), "ps": r.stdout}
                        except Exception as e:
                            out = {"ok": True, "session_key": sess, "running": True, "info": f"docker ps error: {e}"}
                    else:
                        out = {"ok": True, "session_key": sess, "running": False}
                else:
                    sessions = []
                    for k, entry in list(_bot_procs.items()):
                        if entry.get('type') == 'proc':
                            p = entry.get('proc')
                            sessions.append({"session_key": k, "type": "proc", "running": bool(p and p.poll() is None), "pid": getattr(p, 'pid', None)})
                        elif entry.get('type') == 'docker':
                            project = entry.get('project')
                            try:
                                r = subprocess.run(['docker', 'compose', '-p', project, 'ps'], cwd=str(REPO), capture_output=True, text=True, timeout=20)
                                running = (r.returncode == 0 and 'Exit' not in r.stdout)
                                sessions.append({"session_key": k, "type": "docker", "project": project, "running": running, "ps": r.stdout})
                            except Exception:
                                sessions.append({"session_key": k, "type": "docker", "project": project, "running": True})
                        else:
                            sessions.append({"session_key": k, "type": "unknown"})
                    out = {"ok": True, "sessions": sessions}
            return self._json(200, out)

        if path == "/api/schedule-template.csv":
            body = (
                "time,text,poll\n"
                "19:00:00,Hello everyone! Welcome to the session,\n"
                "19:00:10,,Demographics Poll\n"
                "19:05:00,Please fill the poll in chat,\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition", 'attachment; filename="hermes-schedule-template.csv"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        m = re.fullmatch(r"/api/slots/([A-Za-z0-9_-]+)/payment", path)
        if m:
            slot_id = m.group(1)
            known = {s["id"] for s in _load_slots()}
            if slot_id not in known:
                return self._json(404, {"ok": False, "error": f"unknown slot {slot_id}"})
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                return self._json(400, {"ok": False, "error": "invalid JSON"})

            store = _load_payments()
            prev = store.get(slot_id) or _default_payment_cfg(slot_id)
            raw_ids = body.get("payment_link_ids", prev.get("payment_link_ids", ""))
            if isinstance(raw_ids, list):
                link_ids = ",".join(str(x).strip() for x in raw_ids if str(x).strip())
            else:
                link_ids = str(raw_ids or "").strip()
            updated = {
                "payment_link_ids": link_ids,
                "payment_link_label": str(
                    body.get("payment_link_label", prev.get("payment_link_label", ""))
                ).strip(),
                "start_date": str(body.get("start_date", prev.get("start_date", ""))).strip(),
                "end_date": str(body.get("end_date", prev.get("end_date", ""))).strip(),
                # Empty currency = no filter (matches Metabase optional {{currency}})
                "currency": str(body.get("currency", prev.get("currency", "")) or "")
                .strip()
                .upper(),
            }
            # Basic date validation
            for key in ("start_date", "end_date"):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated[key] or ""):
                    return self._json(400, {"ok": False, "error": f"invalid {key}"})

            store[slot_id] = updated
            _save_payments(store)
            revenue = _slot_revenue(slot_id, updated, force=True)
            return self._json(
                200,
                {
                    "ok": True,
                    "payment": updated,
                    "revenue": revenue,
                    "total_display": revenue.get("total_display")
                    or format_totals_by_currency(revenue.get("totals_by_currency") or {}),
                },
            )

        m = re.fullmatch(r"/api/slots/([A-Za-z0-9_-]+)/schedule", path)
        if m:
            slot_id = m.group(1)
            slot_def = next((s for s in _load_slots() if s["id"] == slot_id), None)
            if not slot_def:
                return self._json(404, {"ok": False, "error": f"unknown slot {slot_id}"})
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                return self._json(400, {"ok": False, "error": "invalid JSON"})

            filename = str(body.get("filename") or "schedule.csv")
            b64 = body.get("content_base64") or ""
            if not b64:
                return self._json(400, {"ok": False, "error": "content_base64 required"})
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._json(400, {"ok": False, "error": "invalid base64"})
            if len(raw) > 5_000_000:
                return self._json(400, {"ok": False, "error": "file too large (max 5MB)"})

            try:
                items = parse_upload(filename, raw)
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"ok": False, "error": f"parse failed: {e}"})

            sched_path = REPO / slot_def["schedule_file"]
            existing = _load_schedule(slot_def["schedule_file"])
            written = write_schedule(
                sched_path,
                meeting_id=str(slot_def["meeting_id"]),
                session_name=str(existing.get("session") or slot_def["id"]),
                items=items,
                session_start_ist=str(existing.get("session_start_ist") or ""),
                session_end_ist=str(existing.get("session_end_ist") or ""),
            )
            return self._json(
                200,
                {
                    "ok": True,
                    "schedule_file": slot_def["schedule_file"],
                    "meeting_id": written["meeting_id"],
                    "session": written["session"],
                    "item_count": len(items),
                    "preview": items[:8],
                    "note": "Saved. If the bot is running with this SCHEDULE_FILE, it hot-reloads automatically.",
                },
            )

        if path == "/api/payments/refresh":
            return self._json(200, build_status(force_payments=True))

        # Start/connect bot to Zoom with provided form data + file
        if path in ("/api/connect", "/connect"):
            # Support JSON (with base64-encoded file) or multipart (fallback)
            content_type = (self.headers.get('Content-Type') or '').lower()
            meeting_id = ''
            meeting_join_url = ''
            webinar_start_at = ''
            webinar_end_at = ''
            payment_link_ids = ''
            schedule_items = None
            schedule_path = None

            if content_type.startswith('application/json'):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                try:
                    obj = json.loads(raw.decode('utf-8') or '{}')
                except Exception as e:
                    return self._json(400, {"ok": False, "error": f"invalid JSON: {e}"})
                meeting_id = str(obj.get('meeting_id') or '').strip()
                meeting_join_url = str(obj.get('meeting_join_url') or '').strip()
                webinar_start_at = str(obj.get('webinar_start_at') or '').strip()
                webinar_end_at = str(obj.get('webinar_end_at') or '').strip()
                payment_link_ids = str(obj.get('payment_link_ids') or '').strip()
                b64 = obj.get('schedule_base64')
                filename = obj.get('schedule_filename') or 'schedule.csv'
                if b64:
                    try:
                        raw_sched = base64.b64decode(b64)
                        schedule_items = parse_upload(filename, raw_sched)
                    except Exception as e:
                        return self._json(400, {"ok": False, "error": f"schedule parse failed: {e}"})
            else:
                # try multipart via cgi if available
                try:
                    fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={
                        'REQUEST_METHOD': 'POST',
                        'CONTENT_TYPE': self.headers.get('Content-Type'),
                    })
                except Exception:
                    return self._json(400, {"ok": False, "error": "server missing multipart support; send JSON instead"})
                meeting_id = (fs.getvalue('meeting_id') or '').strip()
                meeting_join_url = (fs.getvalue('meeting_join_url') or '').strip()
                webinar_start_at = (fs.getvalue('webinar_start_at') or '').strip()
                webinar_end_at = (fs.getvalue('webinar_end_at') or '').strip()
                payment_link_ids = (fs.getvalue('payment_link_ids') or '').strip()
                schedule_field = fs['schedule_file'] if 'schedule_file' in fs else None
                if schedule_field and getattr(schedule_field, 'file', None):
                    raw = schedule_field.file.read()
                    filename = getattr(schedule_field, 'filename', 'schedule.csv') or 'schedule.csv'
                    try:
                        schedule_items = parse_upload(filename, raw)
                    except Exception as e:
                        return self._json(400, {"ok": False, "error": f"schedule parse failed: {e}"})

            meeting_id = re.sub(r"\s+", "", meeting_id)

            if not meeting_id and not meeting_join_url:
                return self._json(400, {"ok": False, "error": "meeting_id or meeting_join_url required"})

            # choose meeting id from join URL if provided
            if not meeting_id and meeting_join_url:
                try:
                    u = urlparse(meeting_join_url)
                    q = parse_qs(u.query)
                    if q.get('joinConfNo'):
                        meeting_id = q['joinConfNo'][0]
                    else:
                        parts = [p for p in u.path.split('/') if p]
                        for i, part in enumerate(parts):
                            if part in {'j', 's', 'w'} and i + 1 < len(parts):
                                cand = parts[i + 1]
                                if cand.isdigit():
                                    meeting_id = cand
                                    break
                except Exception:
                    pass

            # write schedule file if uploaded
            schedule_path = None
            if schedule_items:
                sched_dir = REPO / 'schedules'
                sched_dir.mkdir(parents=True, exist_ok=True)
                fname = f"{meeting_id or 'session'}.json"
                schedule_path = sched_dir / fname
                try:
                    write_schedule(schedule_path, meeting_id=str(meeting_id or ''), session_name=str(meeting_id or schedule_path.stem), items=schedule_items)
                except Exception as e:
                    return self._json(500, {"ok": False, "error": f"failed to write schedule: {e}"})

            if not webinar_start_at and schedule_items:
                first_time = schedule_items[0].get('time', '')
                if first_time:
                    webinar_start_at = f"{datetime.now(TZ).date().isoformat()}T{first_time}"
            if not webinar_end_at and schedule_items:
                last_time = schedule_items[-1].get('time', '')
                if last_time:
                    webinar_end_at = f"{datetime.now(TZ).date().isoformat()}T{last_time}"

            # Launch bot (support Docker Compose per-session or local process)
            session_key = meeting_id or meeting_join_url
            if not session_key:
                return self._json(400, {"ok": False, "error": "cannot determine session key"})

            # Playwright is the safe/default production path. Docker/C++ is opt-in
            # from the current dashboard checkbox, including for older clients that
            # omit the field entirely.
            use_docker = False
            try:
                if content_type.startswith('application/json'):
                    use_docker = bool(obj.get('use_docker', False))
            except Exception:
                use_docker = False

            safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", session_key)[:40]

            with _bot_procs_lock:
                existing = _bot_procs.get(session_key)
                if existing and existing.get('type') == 'proc' and existing.get('proc') and existing['proc'].poll() is None:
                    return self._json(409, {"ok": False, "error": "session already running (proc)", "session_key": session_key})
                if existing and existing.get('type') == 'docker' and existing.get('project'):
                    return self._json(409, {"ok": False, "error": "session already running (docker)", "session_key": session_key})

                if use_docker:
                    # write env file for this session — seed from the repo's base .env
                    # so Zoom SDK / LLM credentials carry over, then apply overrides
                    base_env = _read_env_kv(REPO / ".env")
                    env_filename = f".env.{safe_key}"
                    env_path = REPO / env_filename
                    session_env = dict(base_env)
                    if meeting_join_url:
                        session_env['MEETING_JOIN_URL'] = meeting_join_url
                    if meeting_id:
                        session_env['MEETING_ID'] = meeting_id
                    if webinar_start_at:
                        session_env['WEBINAR_START_AT'] = webinar_start_at
                    if webinar_end_at:
                        session_env['WEBINAR_END_AT'] = webinar_end_at
                    session_env['WEBINAR_JOIN_LEAD_MINUTES'] = '30'
                    if payment_link_ids:
                        session_env['PAYMENT_LINK_IDS'] = payment_link_ids
                    # sensible defaults for runtime identity
                    session_env['BOT_DISPLAY_NAME'] = (
                        os.environ.get('BOT_DISPLAY_NAME')
                        or base_env.get('BOT_DISPLAY_NAME')
                        or f"Hermes AI ({safe_key})"
                    )
                    host_email = (
                        os.environ.get('HOST_EMAIL')
                        or os.environ.get('HOST')
                        or base_env.get('HOST_EMAIL')
                        or ''
                    )
                    if host_email:
                        session_env['HOST_EMAIL'] = host_email
                    # attempt to map to a known slot id
                    matched_slot = None
                    try:
                        for s in _load_slots():
                            if meeting_id and str(s.get('meeting_id')) == str(meeting_id):
                                matched_slot = s.get('id')
                                break
                            if s.get('id') == session_key or s.get('env_file', '') == env_filename:
                                matched_slot = s.get('id')
                                break
                    except Exception:
                        matched_slot = None
                    if matched_slot:
                        session_env['SESSION_ID'] = matched_slot
                    env_path.write_text(
                        "\n".join(f"{k}={v}" for k, v in session_env.items() if v != "") + "\n"
                    )

                    project = f"hermes_{safe_key.lower()}"[:32]
                    env_for_run = os.environ.copy()
                    env_for_run['BOT_ENV_FILE'] = env_filename
                    if schedule_path:
                        env_for_run['SCHEDULE_PATH'] = str(schedule_path)
                        env_for_run['SCHEDULE_TZ'] = 'Asia/Kolkata'

                    cmd = ['docker', 'compose', '-p', project, 'up', '-d', '--build']
                    try:
                        r = subprocess.run(cmd, cwd=str(REPO), env=env_for_run, capture_output=True, text=True, timeout=180)
                        if r.returncode != 0:
                            return self._json(500, {"ok": False, "error": f"docker compose failed: {r.stderr.strip()}", "out": r.stdout})
                    except Exception as e:
                        return self._json(500, {"ok": False, "error": f"docker compose error: {e}"})

                    entry = {"type": "docker", "project": project, "env_file": env_filename, "schedule_file": str(schedule_path) if schedule_path else None, "started_at": datetime.now(TZ).isoformat(), "compose_out": r.stdout, "matched_slot": matched_slot}
                    _bot_procs[session_key] = entry
                    return self._json(200, {"ok": True, "session_key": session_key, "project": project, "compose_out": r.stdout, "matched_slot": matched_slot})

                # fallback: spawn local python process
                env = os.environ.copy()
                env['ZOOM_BACKEND'] = 'bridge'
                bridge_proc = None
                bridge_port = None
                try:
                    bridge_port = _pick_playwright_port()
                    bridge_env = os.environ.copy()
                    bridge_env['BRIDGE_PORT'] = str(bridge_port)
                    if webinar_end_at:
                        bridge_env['BRIDGE_END_AT'] = webinar_end_at
                    bridge_env.pop('PLAYWRIGHT_BROWSERS_PATH', None)
                    bridge_proc = subprocess.Popen(
                        ['node', 'index.js'],
                        cwd=str(REPO / 'bridge' / 'node-bridge'),
                        env=bridge_env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    import time as _time
                    ready = False
                    for _ in range(40):
                        try:
                            with urllib.request.urlopen(
                                f'http://127.0.0.1:{bridge_port}/health', timeout=0.5
                            ):
                                ready = True
                                break
                        except Exception:
                            if bridge_proc.poll() is not None:
                                break
                            _time.sleep(0.25)
                    if not ready:
                        raise RuntimeError(f'Playwright bridge failed on port {bridge_port}')
                    env['BRIDGE_URL'] = f'http://127.0.0.1:{bridge_port}'
                    env['BRIDGE_PORT'] = str(bridge_port)
                except Exception as e:
                    if bridge_proc and bridge_proc.poll() is None:
                        bridge_proc.terminate()
                    return self._json(500, {"ok": False, "error": str(e)})
                if meeting_join_url:
                    env['MEETING_JOIN_URL'] = meeting_join_url
                if meeting_id:
                    env['MEETING_ID'] = meeting_id
                if webinar_start_at:
                    env['WEBINAR_START_AT'] = webinar_start_at
                if webinar_end_at:
                    env['WEBINAR_END_AT'] = webinar_end_at
                env['WEBINAR_JOIN_LEAD_MINUTES'] = '30'
                if payment_link_ids:
                    env['PAYMENT_LINK_IDS'] = payment_link_ids
                if schedule_path:
                    env['SCHEDULE_FILE'] = str(schedule_path)
                    env['SCHEDULE_TZ'] = 'Asia/Kolkata'

                default_python = REPO / '.venv' / 'bin' / 'python'
                python_value = env.get('PYTHON') or (
                    str(default_python) if default_python.exists() else sys.executable
                )
                python_cmd = shlex.split(python_value)
                cmd = python_cmd + ['-m', 'src.main']
                try:
                    proc = subprocess.Popen(cmd, env=env, cwd=str(REPO))
                except Exception as e:
                    if bridge_proc and bridge_proc.poll() is None:
                        bridge_proc.terminate()
                    return self._json(500, {"ok": False, "error": f"failed to start bot: {e}"})

                # also write env file for local runs so user can inspect defaults
                env_filename = f".env.{safe_key}"
                env_path = REPO / env_filename
                try:
                    bot_display = os.environ.get('BOT_DISPLAY_NAME') or f"Hermes AI ({safe_key})"
                    host_email = os.environ.get('HOST_EMAIL') or os.environ.get('HOST') or ''
                    lines = []
                    if meeting_join_url:
                        lines.append(f'MEETING_JOIN_URL={meeting_join_url}')
                    if meeting_id:
                        lines.append(f'MEETING_ID={meeting_id}')
                    if bridge_port:
                        lines.append(f'BRIDGE_PORT={bridge_port}')
                        lines.append(f'BRIDGE_URL=http://127.0.0.1:{bridge_port}')
                    if webinar_start_at:
                        lines.append(f'WEBINAR_START_AT={webinar_start_at}')
                    if webinar_end_at:
                        lines.append(f'WEBINAR_END_AT={webinar_end_at}')
                    lines.append('WEBINAR_JOIN_LEAD_MINUTES=30')
                    if payment_link_ids:
                        lines.append(f'PAYMENT_LINK_IDS={payment_link_ids}')
                    lines.append(f'BOT_DISPLAY_NAME={bot_display}')
                    if host_email:
                        lines.append(f'HOST_EMAIL={host_email}')
                    # try map slot id
                    matched_slot = None
                    try:
                        for s in _load_slots():
                            if meeting_id and str(s.get('meeting_id')) == str(meeting_id):
                                matched_slot = s.get('id')
                                break
                    except Exception:
                        matched_slot = None
                    if matched_slot:
                        lines.append(f'SESSION_ID={matched_slot}')
                    env_path.write_text("\n".join(lines) + "\n")
                except Exception:
                    matched_slot = None

                entry = {"type": "proc", "proc": proc, "bridge_proc": bridge_proc, "bridge_port": bridge_port, "started_at": datetime.now(TZ).isoformat(), "schedule_file": str(schedule_path) if schedule_path else None, "env_file": env_filename, "matched_slot": matched_slot}
                _bot_procs[session_key] = entry

            return self._json(200, {"ok": True, "pid": getattr(proc, 'pid', None) if not use_docker else None, "session_key": session_key, "schedule_file": str(schedule_path) if schedule_path else None, "matched_slot": matched_slot})

        # Bot control endpoints
        if path == '/api/bot/stop':
            # stop single or all; query ?session_key=... or body unknown
            qs = parse_qs(parsed.query)
            sess = qs.get('session_key', [None])[0]
            stopped = []
            with _bot_procs_lock:
                if sess:
                    entry = _bot_procs.get(sess)
                    if entry:
                        if entry.get('type') == 'proc':
                            p = entry.get('proc')
                            if p and p.poll() is None:
                                try:
                                    p.terminate()
                                except Exception:
                                    try:
                                        p.kill()
                                    except Exception:
                                        pass
                            bp = entry.get('bridge_proc')
                            if bp and bp.poll() is None:
                                try:
                                    bp.terminate()
                                except Exception:
                                    pass
                        elif entry.get('type') == 'docker':
                            project = entry.get('project')
                            try:
                                subprocess.run(['docker', 'compose', '-p', project, 'down', '--remove-orphans'], cwd=str(REPO), capture_output=True, text=True, timeout=60)
                            except Exception:
                                pass
                        _bot_procs.pop(sess, None)
                        stopped = [sess]
                else:
                    for k, entry in list(_bot_procs.items()):
                        if entry.get('type') == 'proc':
                            p = entry.get('proc')
                            if p and p.poll() is None:
                                try:
                                    p.terminate()
                                except Exception:
                                    try:
                                        p.kill()
                                    except Exception:
                                        pass
                            bp = entry.get('bridge_proc')
                            if bp and bp.poll() is None:
                                try:
                                    bp.terminate()
                                except Exception:
                                    pass
                        elif entry.get('type') == 'docker':
                            project = entry.get('project')
                            try:
                                subprocess.run(['docker', 'compose', '-p', project, 'down', '--remove-orphans'], cwd=str(REPO), capture_output=True, text=True, timeout=60)
                            except Exception:
                                pass
                        _bot_procs.pop(k, None)
                        stopped.append(k)
            return self._json(200, {"ok": True, "stopped": stopped})

        return self._json(404, {"ok": False, "error": "not found"})


def main() -> None:
    _load_dotenv()
    threading.Thread(target=_metrics_tracker_loop, name="hermes-metrics", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Hermes Mission Control → http://{HOST}:{PORT}", flush=True)
    print(
        f"Payments DB: {'configured (' + (payments_db.db_label() or '') + ')' if payments_db.configured() else 'NOT configured — set DATABASE_URL in .env'}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.shutdown()


if __name__ == "__main__":
    main()
