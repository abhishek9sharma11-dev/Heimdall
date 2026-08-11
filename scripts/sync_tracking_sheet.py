#!/usr/bin/env python3
"""Write Hermes session metrics into the Workshops Tracking Google Sheet.

Daily append-only:
  - Each calendar day gets its own Date + Workshop row.
  - Metrics are written only to today's row.
  - Previous days' rows are never modified.

Uses a service-account JSON (Editor on the sheet). Does not touch Zoom stacks.

Env (optional — defaults match today's setup):
  GOOGLE_SHEETS_CREDENTIALS   path to service-account JSON
                              (default: ~/.config/hermes/sheets.json)
  GOOGLE_SHEETS_ID            spreadsheet id
  GOOGLE_SHEETS_TAB           tab name (default: Tracking)

Usage:
  # dry-run: print what would be written for UK
  .venv/bin/python scripts/sync_tracking_sheet.py --session intl-claude-uk-220pm --dry-run

  # write Peak/Retention/INR/USD/Total Payments for today's workshop row
  .venv/bin/python scripts/sync_tracking_sheet.py \\
      --session intl-claude-uk-220pm \\
      --peak 199 --retention 119 --usd 12 --payments 12

  # auto-pick peak + payment-drop + dashboard payment count
  .venv/bin/python scripts/sync_tracking_sheet.py --session intl-claude-uk-220pm --auto

  # create today's row if missing, then write
  .venv/bin/python scripts/sync_tracking_sheet.py --session intl-claude-uk-220pm --auto --ensure-row
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")

DEFAULT_CREDS = Path.home() / ".config/hermes/sheets.json"
DEFAULT_SHEET_ID = "1Ykx3m5O9H07iipM-pE7um6ED1Lh0dCSok2iqaOmq4dw"
DEFAULT_TAB = "Tracking"
STUDENTS_TRACKING_TAB = "AI for Students Tracking"
DASHBOARD = os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:8780")

# Hermes session id → Tracking layout.
# Claude block uses A:G; ChatGPT block uses J:P.
# AI for Students uses a dedicated tab (daily append-only rows).
WORKSHOP_SPECS: dict[str, dict[str, str]] = {
    "intl-claude-uk-220pm": {
        "label": "INTL Claude UK (2:20PM)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",  # C..G
    },
    "claude-421pm": {
        "label": "Claude (4:21PM)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",
    },
    "claude-199dollars-651pm": {
        "label": "Claude 199$(6:51pm)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",
    },
    "claude-99dollars-651pm": {
        "label": "Claude 99$(6:51pm)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",
    },
    "gpt-650pm": {
        "label": "GPT (6:50PM)",
        "date_col": "J",
        "workshop_col": "K",
        "metrics_start": "L",  # L..P
    },
    "gpt-wp-650pm": {
        "label": "GPT (WP) (6:50PM)",
        "date_col": "J",
        "workshop_col": "K",
        "metrics_start": "L",
    },
    "ai-for-students-day1": {
        "label": "AI for Students Day-1 (CLS15)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",
        "tab": STUDENTS_TRACKING_TAB,
    },
    "ai-for-students-day2": {
        "label": "AI for Students Day-2 (CLS14)",
        "date_col": "A",
        "workshop_col": "B",
        "metrics_start": "C",
        "tab": STUDENTS_TRACKING_TAB,
    },
}

# Back-compat alias used by older callers / docs.
WORKSHOP_LABELS = {k: v["label"] for k, v in WORKSHOP_SPECS.items()}


def _sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_path = Path(
        os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    ).expanduser()
    if not creds_path.is_file():
        raise SystemExit(f"credentials not found: {creds_path}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=scopes
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sheet_id() -> str:
    return os.environ.get("GOOGLE_SHEETS_ID", DEFAULT_SHEET_ID)


def _tab() -> str:
    return os.environ.get("GOOGLE_SHEETS_TAB", DEFAULT_TAB)


def sheet_date(dt: datetime | None = None) -> str:
    """Sheet date label, e.g. 6-Aug-2026."""
    d = dt or datetime.now(IST)
    return d.strftime("%-d-%b-%Y")


def _spec(session_id: str) -> dict[str, str]:
    spec = WORKSHOP_SPECS.get(session_id)
    if not spec:
        raise SystemExit(
            f"unknown session {session_id!r}; known: {sorted(WORKSHOP_SPECS)}"
        )
    return spec


def _tab_for(spec: dict[str, str]) -> str:
    return spec.get("tab") or _tab()


TRACKING_HEADERS = [
    "Date",
    "Workshop",
    "Peak Showup",
    "Retention",
    "INR",
    "USD",
    "Total Payments",
]


def ensure_tracking_tab(svc: Any, tab: str, *, dry_run: bool = False) -> None:
    """Create tab + header row if missing (AI for Students daily tracker)."""
    meta = svc.spreadsheets().get(spreadsheetId=_sheet_id()).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in titles:
        print(f"{'DRY-RUN ' if dry_run else ''}create tab {tab!r}")
        if not dry_run:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=_sheet_id(),
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
    # Ensure header row
    hdr_rng = f"'{tab}'!A1:G1"
    existing = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(), range=hdr_rng)
        .execute()
        .get("values", [])
    )
    if not existing or existing[0][:2] != TRACKING_HEADERS[:2]:
        print(f"{'DRY-RUN ' if dry_run else ''}write headers {hdr_rng} = {TRACKING_HEADERS}")
        if not dry_run:
            svc.spreadsheets().values().update(
                spreadsheetId=_sheet_id(),
                range=hdr_rng,
                valueInputOption="USER_ENTERED",
                body={"values": [TRACKING_HEADERS]},
            ).execute()


def find_row(
    svc: Any,
    *,
    workshop: str,
    date_str: str | None = None,
    date_col: str = "A",
    workshop_col: str = "B",
    tab: str | None = None,
) -> int | None:
    """Return 1-based sheet row for Date+Workshop match, or None."""
    if date_str is None:
        date_str = sheet_date()
    tab = tab or _tab()
    rng = f"'{tab}'!{date_col}1:{workshop_col}500"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(), range=rng)
        .execute()
    )
    rows = result.get("values", [])
    workshop_norm = workshop.strip().lower()
    date_norm = date_str.strip().lower()
    for i, r in enumerate(rows, start=1):
        if len(r) < 2:
            continue
        if r[0].strip().lower() == date_norm and r[1].strip().lower() == workshop_norm:
            return i
    return None


def ensure_row(
    svc: Any,
    *,
    workshop: str,
    date_str: str | None = None,
    date_col: str = "A",
    workshop_col: str = "B",
    tab: str | None = None,
    dry_run: bool = False,
) -> int:
    """Find or append a Tracking row for date+workshop. Returns 1-based row."""
    if date_str is None:
        date_str = sheet_date()
    tab = tab or _tab()
    existing = find_row(
        svc,
        workshop=workshop,
        date_str=date_str,
        date_col=date_col,
        workshop_col=workshop_col,
        tab=tab,
    )
    if existing is not None:
        return existing

    rng = f"'{tab}'!{date_col}1:{workshop_col}500"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(), range=rng)
        .execute()
    )
    rows = result.get("values", [])
    next_row = len(rows) + 1
    # Keep a blank separator if the last row already has content.
    if rows and any((c or "").strip() for c in rows[-1]):
        next_row = len(rows) + 1
    target = f"'{tab}'!{date_col}{next_row}:{workshop_col}{next_row}"
    values = [[date_str, workshop]]
    print(f"{'DRY-RUN ' if dry_run else ''}create row {target} = {values[0]}")
    if dry_run:
        return next_row
    (
        svc.spreadsheets()
        .values()
        .update(
            spreadsheetId=_sheet_id(),
            range=target,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )
    return next_row


def _verify_row_identity(
    svc: Any,
    row: int,
    *,
    date_str: str,
    workshop: str,
    date_col: str,
    workshop_col: str,
    tab: str | None = None,
) -> None:
    """Refuse to write if the target row is not exactly today's Date+Workshop."""
    tab = tab or _tab()
    rng = f"'{tab}'!{date_col}{row}:{workshop_col}{row}"
    result = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=_sheet_id(), range=rng)
        .execute()
    )
    values = (result.get("values") or [[]])[0]
    got_date = (values[0] if len(values) > 0 else "").strip()
    got_ws = (values[1] if len(values) > 1 else "").strip()
    if got_date.lower() != date_str.strip().lower() or got_ws.lower() != workshop.strip().lower():
        raise SystemExit(
            f"refusing to overwrite row {row}: found ({got_date!r}, {got_ws!r}) "
            f"but expected ({date_str!r}, {workshop!r}). Previous days are immutable."
        )


def write_metrics(
    svc: Any,
    row: int,
    *,
    metrics_start: str = "C",
    peak: int | None,
    retention: int | None,
    inr: float | str | None,
    usd: float | int | None,
    payments: int | None,
    tab: str | None = None,
    dry_run: bool,
) -> None:
    # Peak | Retention | INR | USD | Total Payments
    tab = tab or _tab()
    start = metrics_start.upper()
    end = chr(ord(start) + 4)
    values = [
        "" if peak is None else peak,
        "" if retention is None else retention,
        "" if inr is None else inr,
        "" if usd is None else usd,
        "" if payments is None else payments,
    ]
    rng = f"'{tab}'!{start}{row}:{end}{row}"
    print(f"{'DRY-RUN ' if dry_run else ''}write {rng} = {values}")
    if dry_run:
        return
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": rng, "values": [values]}],
    }
    resp = (
        svc.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=_sheet_id(), body=body)
        .execute()
    )
    print(f"updated cells: {resp.get('totalUpdatedCells')}")


def _http_json(method: str, url: str, payload: dict | None = None, timeout: int = 20) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _refresh_payment_window(session_id: str, day: str) -> None:
    """Point dashboard payment query window at `day` (YYYY-MM-DD)."""
    try:
        _http_json(
            "POST",
            f"{DASHBOARD}/api/slots/{session_id}/payment",
            {"start_date": day, "end_date": day},
            timeout=15,
        )
    except Exception as e:
        print(f"warn: could not refresh payment window: {e}", file=sys.stderr)


def auto_metrics(session_id: str, *, day: datetime | None = None) -> dict[str, Any]:
    """Pull peak footer, payment-drop retention, dashboard payment count."""
    d = day or datetime.now(IST)
    day_iso = d.strftime("%Y-%m-%d")
    out: dict[str, Any] = {
        "peak": None,
        "retention": None,
        "inr": "",
        "usd": "",
        "payments": None,
    }

    peak_path = Path("/tmp/hermes-peak-attendees.json")
    if peak_path.is_file():
        peak_data = json.loads(peak_path.read_text())
        dated = None
        legacy_today = None
        for key, slot in (peak_data.get("slots") or {}).items():
            if not isinstance(slot, dict) or slot.get("id") != session_id:
                continue
            if str(key).startswith(f"{day_iso}:") or slot.get("date") == day_iso:
                dated = slot
                break
            # legacy undated key — only if the peak itself was recorded today
            peak_at = (
                slot.get("peak_footer_at")
                or slot.get("peak_attendees_at")
                or ((slot.get("last") or {}).get("at") or "")
            )
            if str(peak_at).startswith(day_iso):
                legacy_today = slot
        best = dated or legacy_today
        if best:
            out["peak"] = best.get("peak_footer") or best.get("peak_attendees")

    drop_path = Path("/tmp/hermes-payment-drop-attendees.json")
    if drop_path.is_file():
        drop = json.loads(drop_path.read_text())
        sessions = drop.get("sessions") or {}
        sess = sessions.get(f"{day_iso}:{session_id}")
        if sess is None:
            # legacy undated key — only if dropped today
            legacy = sessions.get(session_id)
            if legacy and str(legacy.get("dropped_at") or "").startswith(day_iso):
                sess = legacy
        if sess:
            out["retention"] = sess.get("attendees_count")
        elif session_id in sessions:
            dropped = str((sessions.get(session_id) or {}).get("dropped_at") or "")
            if dropped and not dropped.startswith(day_iso):
                print(
                    f"warn: retention for {session_id} is from {dropped[:10]} "
                    f"— ignoring for {day_iso}",
                    file=sys.stderr,
                )

    # Prefer full slot detail (uncapped payment list) for INR/USD counts.
    _refresh_payment_window(session_id, day_iso)
    try:
        detail = _http_json(
            "GET",
            f"{DASHBOARD}/api/slots/{session_id}?force=1",
            timeout=30,
        )
        rev = (detail.get("revenue") or {}) if isinstance(detail, dict) else {}
        payments = rev.get("payments") or []
        count = rev.get("count")
        if count is None:
            count = len(payments)
        out["payments"] = count
        currency_counts: Counter[str] = Counter(
            str(p.get("currency") or "").upper()
            for p in payments
            if p.get("currency")
        )
        if count and payments and len(payments) < int(count):
            # Detail list somehow truncated — fall back to single-currency totals.
            totals = rev.get("totals_by_currency") or {}
            if len(totals) == 1:
                currency_counts = Counter({next(iter(totals)): int(count)})
            else:
                print(
                    "warn: payment list shorter than count; "
                    "leaving INR/USD blank to avoid wrong split",
                    file=sys.stderr,
                )
                currency_counts = Counter()
        out["inr"] = currency_counts.get("INR", "") or ""
        out["usd"] = currency_counts.get("USD", "") or ""
    except Exception as e:
        print(f"warn: dashboard slot detail unavailable: {e}", file=sys.stderr)
        # Fallback: status refresh (recent list may be capped).
        try:
            dash = _http_json("POST", f"{DASHBOARD}/api/payments/refresh", {}, timeout=20)
            for s in dash.get("slots") or []:
                if s.get("id") != session_id:
                    continue
                rev = s.get("revenue") or {}
                count = rev.get("count")
                out["payments"] = count
                totals = rev.get("totals_by_currency") or {}
                recent = rev.get("recent") or []
                currency_counts = Counter(
                    str(p.get("currency") or "").upper()
                    for p in recent
                    if p.get("currency")
                )
                if count is not None and len(totals) == 1:
                    currency_counts = Counter({next(iter(totals)): count})
                elif count is not None and len(recent) != count:
                    currency_counts = Counter()
                    print(
                        "warn: exact INR/USD payment split unavailable; "
                        "leaving currency counts blank",
                        file=sys.stderr,
                    )
                out["inr"] = currency_counts.get("INR", "") or ""
                out["usd"] = currency_counts.get("USD", "") or ""
                break
        except Exception as e2:
            print(f"warn: dashboard payments unavailable: {e2}", file=sys.stderr)

    return out


def sync_session(
    session_id: str,
    *,
    date_str: str | None = None,
    peak: int | None = None,
    retention: int | None = None,
    inr: Any = None,
    usd: Any = None,
    payments: int | None = None,
    auto: bool = False,
    ensure: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Library entry: append today's Date+Workshop row if needed, then write metrics
    only on that row. Never updates rows for other dates.
    """
    spec = _spec(session_id)
    workshop = spec["label"]
    tab = _tab_for(spec)
    target_date = date_str or sheet_date()
    metrics: dict[str, Any] = {
        "peak": peak,
        "retention": retention,
        "inr": "" if inr is None else inr,
        "usd": usd,
        "payments": payments,
    }
    if auto:
        auto_m = auto_metrics(session_id)
        for k, v in auto_m.items():
            if metrics.get(k) is None or (k in ("inr", "usd") and metrics[k] in (None, "")):
                if v is not None:
                    metrics[k] = v

    svc = _sheets_service()
    if spec.get("tab"):
        ensure_tracking_tab(svc, tab, dry_run=dry_run)
    if ensure:
        row = ensure_row(
            svc,
            workshop=workshop,
            date_str=target_date,
            date_col=spec["date_col"],
            workshop_col=spec["workshop_col"],
            tab=tab,
            dry_run=dry_run,
        )
    else:
        row = find_row(
            svc,
            workshop=workshop,
            date_str=target_date,
            date_col=spec["date_col"],
            workshop_col=spec["workshop_col"],
            tab=tab,
        )
        if row is None:
            raise SystemExit(
                f"no Tracking row for date={target_date!r} workshop={workshop!r}"
            )

    if not dry_run:
        _verify_row_identity(
            svc,
            row,
            date_str=target_date,
            workshop=workshop,
            date_col=spec["date_col"],
            workshop_col=spec["workshop_col"],
            tab=tab,
        )

    write_metrics(
        svc,
        row,
        metrics_start=spec["metrics_start"],
        peak=metrics["peak"],
        retention=metrics["retention"],
        inr=metrics["inr"],
        usd=metrics["usd"],
        payments=metrics["payments"],
        tab=tab,
        dry_run=dry_run,
    )
    return {
        "ok": True,
        "session_id": session_id,
        "workshop": workshop,
        "tab": tab,
        "row": row,
        "date": target_date,
        "metrics": metrics,
        "dry_run": dry_run,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="Hermes session id")
    ap.add_argument("--date", default=None, help="Sheet date e.g. 6-Aug-2026")
    ap.add_argument("--peak", type=int, default=None)
    ap.add_argument("--retention", type=int, default=None)
    ap.add_argument("--inr", default=None)
    ap.add_argument("--usd", default=None)
    ap.add_argument("--payments", type=int, default=None)
    ap.add_argument("--auto", action="store_true", help="Fill from peak/drop/dashboard")
    ap.add_argument(
        "--ensure-row",
        action="store_true",
        default=True,
        help="Create Date+Workshop row if missing (default on)",
    )
    ap.add_argument(
        "--no-ensure-row",
        action="store_true",
        help="Fail if the Tracking row does not already exist",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    usd_val: Any = args.usd
    if isinstance(usd_val, str) and usd_val.isdigit():
        usd_val = int(usd_val)

    summary = sync_session(
        args.session,
        date_str=args.date,
        peak=args.peak,
        retention=args.retention,
        inr=args.inr,
        usd=usd_val,
        payments=args.payments,
        auto=args.auto,
        ensure=not args.no_ensure_row,
        dry_run=args.dry_run,
    )
    print("summary:", json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
