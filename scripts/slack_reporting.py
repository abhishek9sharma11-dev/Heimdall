#!/usr/bin/env python3
"""Generate completed-session reports and optionally deliver them to Slack.

Slack delivery is deliberately best-effort.  The report files are generated
before any network call, and a Slack failure never raises into session cleanup.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANNEL = "C0BTCQYF90"
SLACK_API = "https://slack.com/api"
IST = ZoneInfo("Asia/Kolkata")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def metric_row(data: dict, meeting_id: str, *, payment: bool = False) -> dict:
    values = (data.get("sessions") if payment else data.get("slots")) or {}
    found = {}
    for value in values.values():
        if str(value.get("meeting_id") or "") == str(meeting_id):
            found = value
    return found


def report_row(
    session: dict[str, Any],
    peak: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
    sheet_sync: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build one stable, non-secret summary row for CSV/XLSX output."""
    peak = peak or {}
    payment = payment or {}
    sheet_sync = sheet_sync or {}
    return {
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "session_id": str(session.get("id") or ""),
        "session": str(session.get("session") or session.get("id") or ""),
        "meeting_id": str(session.get("meeting_id") or ""),
        "session_start_ist": str(session.get("session_start_ist") or ""),
        "session_end_ist": str(session.get("session_end_ist") or ""),
        "peak_attendees": str(peak.get("peak_attendees") or ""),
        "peak_participants": str(peak.get("peak_participants") or ""),
        "retention_at_payment_drop": str(payment.get("attendees_count") or ""),
        "payment_drop_time": str(payment.get("schedule_time") or ""),
        "sheet_sync": "ok" if sheet_sync.get("ok") else ("failed" if sheet_sync else "not_attempted"),
    }


def _write_xlsx(path: Path, row: dict[str, str]) -> None:
    """Write a dependency-free, valid XLSX containing the report row."""
    headers = list(row)
    cells = headers, [row[key] for key in headers]
    rows_xml = []
    for values in cells:
        row_xml = []
        for index, value in enumerate(values, start=1):
            col = ""
            n = index
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            row_xml.append(
                f'<c r="{col}{len(rows_xml) + 1}" t="inlineStr">'
                f"<is><t>{xml_escape(str(value))}</t></is></c>"
            )
        rows_xml.append(f'<row r="{len(rows_xml) + 1}">{"".join(row_xml)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows_xml)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Session Report" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def generate_reports(
    session: dict[str, Any],
    peak: dict[str, Any] | None = None,
    payment: dict[str, Any] | None = None,
    sheet_sync: dict[str, Any] | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    row = report_row(session, peak, payment, sheet_sync)
    output_dir = output_dir or Path("/tmp/hermes-session-reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{row['session_id'] or row['meeting_id']}-{row['date']}-session-report"
    csv_path = output_dir / f"{stem}.csv"
    xlsx_path = output_dir / f"{stem}.xlsx"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    _write_xlsx(xlsx_path, row)
    return csv_path, xlsx_path


def slack_payloads(
    session: dict[str, Any], row: dict[str, str], files: tuple[Path, Path]
) -> dict[str, Any]:
    channel = os.environ.get("SLACK_CHANNEL_ID") or DEFAULT_CHANNEL
    label = row["session"] or row["meeting_id"] or "session"
    text = (
        f"Session complete: {label}\n"
        f"Meeting ID: {row['meeting_id']}\n"
        f"Peak attendees: {row['peak_attendees'] or '—'} | "
        f"Retention: {row['retention_at_payment_drop'] or '—'}\n"
        f"Sheet sync: {row['sheet_sync']}"
    )
    return {
        "channel": channel,
        "message": {"channel": channel, "text": text},
        "files": [
            {"filename": path.name, "length": path.stat().st_size, "channel_id": channel}
            for path in files
        ],
    }


def _api_call(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Slack {method} request failed: {type(exc).__name__}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Slack {method} rejected request: {result.get('error', 'unknown_error')}")
    return result


def _upload_external(path: Path, channel: str, thread_ts: str | None, token: str) -> dict[str, Any]:
    meta = _api_call(
        "files.getUploadURLExternal",
        {"filename": path.name, "length": path.stat().st_size},
        token,
    )
    upload_request = urllib.request.Request(
        meta["upload_url"],
        data=path.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upload_request, timeout=60) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Slack file upload returned HTTP {response.status}")
    except (OSError, urllib.error.HTTPError) as exc:
        raise RuntimeError(f"Slack file upload failed: {type(exc).__name__}") from exc
    complete = {"files": [{"id": meta["file_id"], "title": path.name}], "channel_id": channel}
    if thread_ts:
        complete["thread_ts"] = thread_ts
    return _api_call("files.completeUploadExternal", complete, token)


def deliver_reports(
    session: dict[str, Any],
    files: tuple[Path, Path],
    *,
    row: dict[str, str] | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    row = row or report_row(session)
    payload = slack_payloads(session, row, files)
    if dry_run is None:
        dry_run = os.environ.get("HERMES_SLACK_DRY_RUN", "0") == "1"
    if dry_run:
        return {"ok": True, "dry_run": True, "payload": payload}
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "skipped": True, "error": "SLACK_BOT_TOKEN not configured"}
    try:
        message = _api_call("chat.postMessage", payload["message"], token)
        thread_ts = message.get("ts")
        for path in files:
            _upload_external(path, payload["channel"], thread_ts, token)
        return {"ok": True, "dry_run": False, "channel": payload["channel"], "files": [p.name for p in files]}
    except Exception as exc:
        # Deliberately omit exception details that could contain remote URLs or
        # request data.  The token is never interpolated into logs or results.
        return {"ok": False, "error": str(exc)}


def finalize_report(
    session: dict[str, Any],
    peak: dict[str, Any],
    payment: dict[str, Any],
    sheet_sync: dict[str, Any],
    *,
    output_dir: Path | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    files = generate_reports(session, peak, payment, sheet_sync, output_dir)
    row = report_row(session, peak, payment, sheet_sync)
    delivery = deliver_reports(session, files, row=row, dry_run=dry_run)
    return {"report_files": [str(path) for path in files], "delivery": delivery}


def _dry_run() -> int:
    with TemporaryDirectory(prefix="hermes-slack-dry-run-") as tmp:
        session = {
            "id": "dry-run-session",
            "session": "Slack report dry run",
            "meeting_id": "00000000000",
            "session_start_ist": "19:00:00",
            "session_end_ist": "20:00:00",
        }
        result = finalize_report(
            session,
            {"peak_attendees": 42, "peak_participants": 40},
            {"attendees_count": 31, "schedule_time": "19:30:00"},
            {"ok": True},
            output_dir=Path(tmp),
            dry_run=True,
        )
        assert result["delivery"]["payload"]["channel"] == (os.environ.get("SLACK_CHANNEL_ID") or DEFAULT_CHANNEL)
        assert all(Path(path).exists() for path in result["report_files"])
        print(json.dumps(result, indent=2))
    return 0


def _real_test() -> int:
    if os.environ.get("HERMES_SLACK_REAL_TEST") != "1":
        print("HERMES_SLACK_REAL_TEST=1 is required for the real Slack test", file=sys.stderr)
        return 2
    session = {"id": "slack-real-test", "session": "Heimdall Slack real delivery test", "meeting_id": "00000000000"}
    with TemporaryDirectory(prefix="hermes-slack-real-test-") as tmp:
        result = finalize_report(session, {}, {}, {"ok": True}, output_dir=Path(tmp))
        print(json.dumps({"ok": result["delivery"].get("ok"), "files": result["report_files"]}))
        return 0 if result["delivery"].get("ok") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--real-test", action="store_true")
    args = parser.parse_args()
    if args.real_test:
        raise SystemExit(_real_test())
    raise SystemExit(_dry_run() if args.dry_run else 0)
