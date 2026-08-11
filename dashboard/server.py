"""
Hermes Mission Control — multi-slot bridge health + live payments (DATABASE_URL).

Usage:
  # DATABASE_URL in repo .env, then:
  .venv/bin/python -m dashboard.server
  # open http://127.0.0.1:8780
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import payments_db
from .payments_db import format_totals_by_currency
from .schedule_upload import parse_upload, write_schedule

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
STATIC = ROOT / "static"
SLOTS_PATH = ROOT / "slots.json"
PAYMENTS_PATH = ROOT / "payments.json"
COHORTS_PATH = ROOT / "cohorts.json"
PEAK_PATH = Path("/tmp/hermes-peak-attendees.json")
PAY_DROP_PATH = Path("/tmp/hermes-payment-drop-attendees.json")
TZ = ZoneInfo("Asia/Kolkata")
HOST = "127.0.0.1"
PORT = int(os.environ.get("HERMES_PORT") or "8780")

_payments_lock = threading.Lock()
_cohorts_lock = threading.Lock()


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
    if end_t and now > end_t:
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


def build_slot_detail(slot_id: str, *, force_payments: bool = True) -> dict[str, Any] | None:
    """Full session payload for the payments detail page."""
    now = datetime.now(TZ)
    slot_def = next((s for s in _load_slots() if s["id"] == slot_id), None)
    if not slot_def:
        return None

    env = _read_env_kv(REPO / slot_def["env_file"])
    sched = _load_schedule(slot_def["schedule_file"])
    health = _bridge_health(int(slot_def["port"]))
    phase = _session_phase(sched, now)
    pay_cfg = {**_default_payment_cfg(slot_id), **(_load_payments().get(slot_id) or {})}
    revenue = _slot_revenue(slot_id, pay_cfg, force=force_payments)
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
            "bot_health": 100
            if health["online"] and not health["reconnecting"]
            else (50 if health["online"] else 0),
            "health": health,
            "schedule_items": len(sched.get("items") or []),
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
        sched = _load_schedule(slot["schedule_file"])
        health = _bridge_health(int(slot["port"]))
        phase = _session_phase(sched, now)

        if health["online"]:
            bots_online += 1
        if health["in_meeting"] or phase == "live":
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
        if health["online"] and health["in_meeting"]:
            status_label = "live" if phase != "ended" else "wrapping"
        elif health["online"]:
            status_label = health["meeting_state"]
        elif phase == "scheduled":
            status_label = "scheduled"

        pay_cfg = {**_default_payment_cfg(slot["id"]), **(payments_store.get(slot["id"]) or {})}
        revenue = _slot_revenue(slot["id"], pay_cfg, force=force_payments)
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
                "cohort_id": metrics.get("cohort_id"),
                "bot_health": 100
                if health["online"] and not health["reconnecting"]
                else (50 if health["online"] else 0),
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

        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()

        if path == "/session" or path == "/session.html":
            self.path = "/session.html"
            return super().do_GET()

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

            import base64

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

        return self._json(404, {"ok": False, "error": "not found"})


def main() -> None:
    _load_dotenv()
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
