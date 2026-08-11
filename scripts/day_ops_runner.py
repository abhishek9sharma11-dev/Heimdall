#!/usr/bin/env python3
"""Day ops: auto-start webinar stacks 30 min before start; track peak attendees;
auto-sync Peak/Retention/INR/USD/Total Payments into Workshops Tracking after
session_end (+ grace).

Reads schedules/today_sessions.json. For each enabled session:
  - At (session_start - start_lead_minutes): start bridge+python via hermes_slots.sh
  - For first peak_window_minutes after session_start: poll participant counts every 10s
  - At first payment-link drop: snapshot attendees (retention)
  - At session_end + 5 min: write metrics to Google Sheet Tracking tab

Does not stop other sessions. Safe to leave running all day.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "schedules" / "today_sessions.json"
HERMES = ROOT / "scripts" / "hermes_slots.sh"
PEAK_OUT = Path("/tmp/hermes-peak-attendees.json")
PAY_DROP_OUT = Path("/tmp/hermes-payment-drop-attendees.json")
SHEET_SYNC_OUT = Path("/tmp/hermes-sheet-sync.json")
LOG = Path("/tmp/day-ops-runner.log")
TZ = ZoneInfo("Asia/Kolkata")
TICK_SEC = 5
PEAK_INTERVAL = 10
DAILY_MD_INTERVAL = 60
# Wait a few minutes after session_end so late payments can land, then write Tracking.
SHEET_SYNC_GRACE_MIN = 5
PAYMENT_LINK_RE = re.compile(r"https?://link\.outskill\.com/[^\s<>\"']+", re.I)

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


def log(msg: str) -> None:
    # stdout only — launcher redirects to LOG; avoid double-write
    print(f"{datetime.now(TZ).strftime('%H:%M:%S')} {msg}", flush=True)


def now() -> datetime:
    return datetime.now(TZ)


def parse_hhmmss(s: str) -> datetime:
    h, m, sec = map(int, s.split(":"))
    n = now()
    return n.replace(hour=h, minute=m, second=sec, microsecond=0)


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


def load_peak() -> dict:
    if PEAK_OUT.exists():
        try:
            return json.loads(PEAK_OUT.read_text())
        except Exception:
            pass
    return {"updated_at": None, "slots": {}}


def save_peak(data: dict) -> None:
    PEAK_OUT.write_text(json.dumps(data, indent=2))


def load_pay_drops() -> dict:
    if PAY_DROP_OUT.exists():
        try:
            return json.loads(PAY_DROP_OUT.read_text())
        except Exception:
            pass
    return {
        "tag": "payment_link_drop_attendees_count",
        "updated_at": None,
        "sessions": {},
    }


def save_pay_drops(data: dict) -> None:
    PAY_DROP_OUT.write_text(json.dumps(data, indent=2) + "\n")


def load_sheet_sync() -> dict:
    if SHEET_SYNC_OUT.exists():
        try:
            return json.loads(SHEET_SYNC_OUT.read_text())
        except Exception:
            pass
    return {"updated_at": None, "sessions": {}}


def save_sheet_sync(data: dict) -> None:
    SHEET_SYNC_OUT.write_text(json.dumps(data, indent=2) + "\n")


def sync_tracking_sheet(session_id: str) -> tuple[bool, str]:
    """Write Peak/Retention/INR/USD/Total Payments into Workshops Tracking."""
    try:
        r = subprocess.run(
            [
                str(ROOT / ".venv/bin/python"),
                str(ROOT / "scripts/sync_tracking_sheet.py"),
                "--session",
                session_id,
                "--auto",
                "--ensure-row",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as e:
        return False, f"sync launch error: {e}"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        return False, out[-500:] or f"exit {r.returncode}"
    return True, out[-500:]


def first_payment_from_schedule(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except Exception:
        return None
    rows = []
    for it in doc.get("items") or []:
        text = it.get("text") or ""
        m = PAYMENT_LINK_RE.search(text)
        if not m:
            continue
        t = it.get("time") or ""
        if t:
            rows.append((t, text, m.group(0).rstrip(").,;")))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    t, text, url = rows[0]
    return {"time": t, "text": text, "url": url}


def best_count(counts: dict) -> int | None:
    for k in ("attendees", "footer", "participants"):
        v = counts.get(k)
        if isinstance(v, int) and v >= 0:
            return v
    return None


def capture_payment_drop(s: dict, first: dict, counts: dict, pay: dict) -> None:
    sid = s["id"]
    n = now()
    day = n.strftime("%Y-%m-%d")
    # One retention snapshot per session per calendar day — never overwrite older days.
    key = f"{day}:{sid}"
    sessions = pay.setdefault("sessions", {})
    if key in sessions and sessions[key].get("attendees_count") is not None:
        return
    # Clear legacy undated key only if it belongs to a previous day.
    legacy = sessions.get(sid)
    if legacy and not str(legacy.get("dropped_at") or "").startswith(day):
        pass  # leave historical legacy entry; prefer dated keys going forward
    entry = {
        "tag": "payment_link_drop_attendees_count",
        "session_id": sid,
        "meeting_id": s.get("meeting_id"),
        "port": s.get("port"),
        "date": day,
        "schedule_time": first["time"],
        "dropped_at": n.isoformat(),
        "payment_url": first.get("url") or "",
        "message_preview": (first.get("text") or "")[:160].replace("\n", " "),
        "attendees_count": best_count(counts),
        "footer": counts.get("footer"),
        "attendees": counts.get("attendees"),
        "participants": counts.get("participants"),
        "source": "day_ops",
    }
    sessions[key] = entry
    pay["updated_at"] = n.isoformat()
    save_pay_drops(pay)
    log(
        f"payment_link_drop_attendees_count {sid} :{s['port']} "
        f"at={first['time']} count={entry['attendees_count']} counts={counts}"
    )


def start_slot(port: int, env_file: str) -> bool:
    h = health(port)
    state = h.get("meeting_state")
    if state in ("waiting", "in_meeting", "joining"):
        log(f"skip start :{port} already {state}")
        return True
    if h.get("status") == "ok":
        # bridge up but idle — stop then restart clean
        subprocess.run([str(HERMES), "stop", str(port)], cwd=str(ROOT), check=False)
        time.sleep(1)
    env_path = ROOT / env_file
    if not env_path.exists():
        log(f"ERROR missing env {env_file}")
        return False
    # require join URL
    text = env_path.read_text()
    if "MEETING_JOIN_URL=\n" in text or "MEETING_JOIN_URL=\r" in text or "MEETING_JOIN_URL=$" in text:
        log(f"ERROR :{port} empty MEETING_JOIN_URL in {env_file}")
        return False
    if "MEETING_JOIN_URL=" not in text:
        log(f"ERROR :{port} no MEETING_JOIN_URL in {env_file}")
        return False
    # empty value after =
    for line in text.splitlines():
        if line.startswith("MEETING_JOIN_URL="):
            val = line.split("=", 1)[1].strip().strip("'\"")
            if not val:
                log(f"ERROR :{port} empty join URL — cannot start")
                return False
    log(f"starting :{port} with {env_file}")
    r = subprocess.run(
        [str(HERMES), "start", str(port), env_file],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        log(f"start FAILED :{port}: {r.stdout[-400:]} {r.stderr[-400:]}")
        return False
    log(f"start OK :{port}: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'ok'}")
    return True


def try_fill_missing_join_from_excel(sessions: list[dict]) -> None:
    """If Claude 199$ (or any blocked slot) gains a zoom URL in Workshops (2).xlsx, write it into .env."""
    xlsx = Path("/Users/growthschool/Downloads/Workshops (2).xlsx")
    if not xlsx.exists():
        return
    try:
        import openpyxl
        import re
        from urllib.parse import urlparse, parse_qs
    except Exception:
        return
    try:
        wb = openpyxl.load_workbook(xlsx, data_only=True)
    except Exception as e:
        log(f"excel reload skip: {e}")
        return
    urls_by_mid: dict[str, str] = {}
    for sheet in wb.sheetnames:
        mid = None
        url = None
        for row in wb[sheet].iter_rows(values_only=True):
            a = row[0] if row else None
            b = row[1] if row and len(row) > 1 else None
            a_s = "" if a is None else str(a).strip()
            b_s = "" if b is None else str(b).strip()
            if a_s.lower().startswith("webinar") and b_s:
                mid = re.sub(r"[^\d]", "", b_s.split(".")[0])
            if b_s.startswith("http") and "zoom.us/w/" in b_s:
                url = b_s.split()[0].strip()
        if mid and url:
            urls_by_mid[mid] = url
    for s in sessions:
        if s.get("enabled") and s.get("join_url_present"):
            continue
        mid = s["meeting_id"]
        url = urls_by_mid.get(mid)
        if not url:
            continue
        tk = parse_qs(urlparse(url).query).get("tk", [""])[0]
        env = ROOT / s["env_file"]
        text = env.read_text() if env.exists() else ""
        lines = []
        for line in text.splitlines():
            if line.startswith("MEETING_JOIN_URL="):
                lines.append(f"MEETING_JOIN_URL={url}")
            elif line.startswith("MEETING_WEBINAR_TOKEN="):
                lines.append(f"MEETING_WEBINAR_TOKEN={tk}")
            else:
                lines.append(line)
        if not any(l.startswith("MEETING_JOIN_URL=") for l in lines):
            lines.append(f"MEETING_JOIN_URL={url}")
            lines.append(f"MEETING_WEBINAR_TOKEN={tk}")
        env.write_text("\n".join(lines) + "\n")
        s["join_url_present"] = True
        if s.get("force_disabled"):
            s["enabled"] = False
            log(f"filled join URL for {s['id']} but force_disabled — leaving enabled=false")
        else:
            s["enabled"] = True
            log(f"filled join URL for {s['id']} mid={mid} from Workshops (2).xlsx")


def main() -> None:
    if not MANIFEST.exists():
        raise SystemExit(f"missing {MANIFEST}")
    manifest = json.loads(MANIFEST.read_text())
    sessions = manifest["sessions"]
    started: set[str] = set()
    last_peak_at: dict[str, float] = {}
    peak = load_peak()
    pay = load_pay_drops()
    sheet_sync = load_sheet_sync()
    first_pay: dict[str, dict] = {}
    last_daily_md = 0.0
    for s in sessions:
        fp = first_payment_from_schedule(ROOT / s["schedule_file"])
        if fp:
            first_pay[s["id"]] = fp
            log(
                f"  first_payment {s['id']} @ {fp['time']} → {fp['url'][:60]}"
            )
        else:
            log(f"  first_payment {s['id']} — none found in schedule")

    log(
        f"day_ops_runner live — {len(sessions)} sessions "
        f"({sum(1 for s in sessions if s.get('enabled'))} enabled)"
    )
    for s in sessions:
        lead = int(s.get("start_lead_minutes") or 30)
        st = parse_hhmmss(s["session_start_ist"])
        go = st - timedelta(minutes=lead)
        flag = "READY" if s.get("enabled") else "BLOCKED(no join URL)"
        log(
            f"  {s['id']} :{s['port']} start={s['session_start_ist']} "
            f"auto_start={go.strftime('%H:%M:%S')} {flag}"
        )

    # Mark already-live slots as started
    for s in sessions:
        h = health(int(s["port"]))
        if h.get("meeting_state") in ("waiting", "in_meeting", "joining"):
            started.add(s["id"])
            log(f"already live {s['id']} :{s['port']} state={h.get('meeting_state')}")

    while True:
        n = now()
        if n.hour >= 23 and n.minute >= 45:
            log("past 23:45 — day_ops_runner exiting")
            break

        # Periodically pick up join URLs dropped into Excel / .env
        if int(time.time()) % 60 < TICK_SEC + 1:
            try_fill_missing_join_from_excel(sessions)
            # Re-apply force_disabled / enabled from disk
            try:
                disk = json.loads(MANIFEST.read_text()).get("sessions") or []
                by_id = {x["id"]: x for x in disk}
                for s in sessions:
                    d = by_id.get(s["id"])
                    if not d:
                        continue
                    if d.get("force_disabled"):
                        s["force_disabled"] = True
                        s["enabled"] = False
                    elif "enabled" in d:
                        s["enabled"] = bool(d["enabled"])
            except Exception:
                pass
        for s in sessions:
            if s.get("force_disabled"):
                s["enabled"] = False
                continue
            if s.get("enabled") and s.get("join_url_present"):
                continue
            env = ROOT / s["env_file"]
            if not env.exists():
                continue
            for line in env.read_text().splitlines():
                if line.startswith("MEETING_JOIN_URL="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val.startswith("http"):
                        s["join_url_present"] = True
                        if not s.get("force_disabled"):
                            s["enabled"] = True
                            log(f"enabled {s['id']} — join URL appeared in {s['env_file']}")
                    break

        for s in sessions:
            if s.get("force_disabled") or not s.get("enabled"):
                continue
            sid = s["id"]
            port = int(s["port"])
            lead = int(s.get("start_lead_minutes") or 30)
            peak_mins = int(s.get("peak_window_minutes") or 60)
            start_dt = parse_hhmmss(s["session_start_ist"])
            end_dt = parse_hhmmss(s["session_end_ist"])
            auto_at = start_dt - timedelta(minutes=lead)
            peak_until = start_dt + timedelta(minutes=peak_mins)

            # Auto-start window: from lead time until session end
            if sid not in started and n >= auto_at and n <= end_dt + timedelta(minutes=10):
                if start_slot(port, s["env_file"]):
                    started.add(sid)
                else:
                    # retry next ticks
                    pass

            # Peak tracking: session start → +1h, only if bridge is live
            if start_dt <= n <= peak_until:
                last = last_peak_at.get(sid, 0.0)
                if time.time() - last >= PEAK_INTERVAL:
                    last_peak_at[sid] = time.time()
                    h = health(port)
                    state = h.get("meeting_state") or "down"
                    if state in ("in_meeting", "waiting"):
                        c = eval_counts(port)
                        key = f"{n.strftime('%Y-%m-%d')}:{port}:{sid}"
                        slot = peak["slots"].setdefault(
                            key,
                            {
                                "port": port,
                                "id": sid,
                                "meeting_id": s["meeting_id"],
                                "session_start_ist": s["session_start_ist"],
                                "date": n.strftime("%Y-%m-%d"),
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
                                slot[f"{peak_key}_at"] = n.isoformat()
                        slot["last"] = {
                            **c,
                            "meeting_state": state,
                            "at": n.isoformat(),
                        }
                        peak["updated_at"] = n.isoformat()
                        save_peak(peak)
                        log(
                            f"peak {sid} :{port} state={state} now={c} "
                            f"peaks=f:{slot['peak_footer']} a:{slot['peak_attendees']} p:{slot['peak_participants']}"
                        )

            # First payment-link drop → snapshot attendees once (per calendar day)
            fp = first_pay.get(sid)
            day_key = f"{n.strftime('%Y-%m-%d')}:{sid}"
            existing = (pay.get("sessions") or {}).get(day_key)
            if fp and not (existing and existing.get("attendees_count") is not None):
                drop_at = parse_hhmmss(fp["time"])
                # Capture in a short window around the scheduled drop (±90s)
                if abs((n - drop_at).total_seconds()) <= 90:
                    h = health(port)
                    state = h.get("meeting_state") or "down"
                    if state in ("in_meeting", "waiting"):
                        c = eval_counts(port)
                        capture_payment_drop(s, fp, c, pay)

            # End-of-session → auto-write Tracking sheet (Peak / Retention / INR / USD / Total)
            sync_at = end_dt + timedelta(minutes=SHEET_SYNC_GRACE_MIN)
            prior = (sheet_sync.get("sessions") or {}).get(sid) or {}
            already = bool(prior.get("ok")) and prior.get("date") == n.strftime("%Y-%m-%d")
            last_try = prior.get("synced_at")
            recently_tried = False
            if last_try and prior.get("date") == n.strftime("%Y-%m-%d"):
                try:
                    recently_tried = (
                        n - datetime.fromisoformat(last_try)
                    ).total_seconds() < 300
                except Exception:
                    recently_tried = False
            if (
                sid in started
                and not already
                and not recently_tried
                and n >= sync_at
                and n <= end_dt + timedelta(minutes=120)
            ):
                ok, detail = sync_tracking_sheet(sid)
                entry = {
                    "session_id": sid,
                    "meeting_id": s.get("meeting_id"),
                    "port": port,
                    "date": n.strftime("%Y-%m-%d"),
                    "synced_at": n.isoformat(),
                    "ok": ok,
                    "detail": detail[-400:],
                }
                sheet_sync.setdefault("sessions", {})[sid] = entry
                sheet_sync["updated_at"] = n.isoformat()
                save_sheet_sync(sheet_sync)
                if ok:
                    log(f"sheet_sync OK {sid} :{port} — Tracking updated")
                else:
                    log(f"sheet_sync FAILED {sid} :{port}: {detail[-200:]}")

        # Refresh docs/DAILY-RUN-TODAY.md periodically
        if time.time() - last_daily_md >= DAILY_MD_INTERVAL:
            last_daily_md = time.time()
            try:
                r = subprocess.run(
                    [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/write_daily_run.py")],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if r.returncode == 0:
                    log("daily_run.md refreshed")
                else:
                    log(f"daily_run.md refresh failed: {r.stderr[-200:]}")
            except Exception as e:
                log(f"daily_run.md refresh error: {e}")

        time.sleep(TICK_SEC)


if __name__ == "__main__":
    main()
