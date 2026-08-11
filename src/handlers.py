"""
Handlers wire client events to behavior:

- Greeter: welcomes new attendees (throttled)
- ChatHandler: routes incoming chat to brain or to slash command parser
- SlashCommands: /schedule, /list, /cancel, /quiet, /resume, /dm
- MessageScheduler: fires queued messages at the right time
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, time as time_t
from pathlib import Path
from typing import Deque, Dict, List, Optional

from .brain import ClaudeClient, RAGRetriever
from .config import settings
from .metrics_store import is_payment_link_message, record_chat_drop, record_payment_drop
from .zoom_client import ChatMessage, Participant, ZoomClient

log = logging.getLogger(__name__)

# Same DOM scrape used by peak/day-ops watchers.
_ATTENDEE_COUNT_JS = r"""
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


# ============================================================
# Greeter
# ============================================================

class Greeter:
    def __init__(self, client: ZoomClient) -> None:
        self.client = client
        self._recent: Deque[datetime] = deque()
        self._greeted: set[str] = set()

    async def on_join(self, p: Participant) -> None:
        if not settings.greet_new_attendees:
            return
        if p.role != "attendee":
            return
        if p.user_id in self._greeted:
            return

        # Throttle to N per rolling minute
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=60)
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        if len(self._recent) >= settings.greet_max_per_minute:
            log.info("greet rate-limit, skip %s", p.name)
            return

        self._recent.append(now)
        self._greeted.add(p.user_id)

        # Slight delay so we don't spam during mass arrival
        await asyncio.sleep(settings.greet_delay_seconds)

        first = p.name.split()[0] if p.name else "there"
        text = (
            f"hi {first}! 👋 i'm {settings.bot_display_name}, "
            f"{settings.bot_disclosure_line.split('—', 1)[-1].strip() if '—' in settings.bot_disclosure_line else settings.bot_disclosure_line} "
            f"drop your questions in chat anytime."
        )
        try:
            await self.client.send_chat(text, to="everyone")
        except Exception as e:
            log.warning("greet failed for %s: %s", p.name, e)


# ============================================================
# Chat handler
# ============================================================

class ChatHandler:
    """Routes every chat message: slash commands, questions, or ignore."""

    QUESTION_HINTS = (
        "?", "how do", "how can", "what is", "what's", "why does", "why is",
        "where can", "when does", "can you", "could you", "is there",
    )

    def __init__(self, client: ZoomClient, slash: "SlashCommands") -> None:
        self.client = client
        self.slash = slash
        self.brain: ClaudeClient | None = ClaudeClient() if settings.answer_questions else None
        self.rag: RAGRetriever | None = RAGRetriever() if settings.answer_questions else None
        self._answers: Dict[str, Deque[datetime]] = defaultdict(deque)
        # Once we draft/send a reply for (sender, question), never retry it —
        # even if the human clears the draft without Submit and the bridge
        # re-emits the same chat line later.
        self._handled_questions: set[str] = set()
        self._quiet = False

    def set_quiet(self, q: bool) -> None:
        self._quiet = q

    @staticmethod
    def _question_key(sender_id: str, text: str) -> str:
        norm = re.sub(r"\s+", " ", (text or "").strip().lower())
        return f"{(sender_id or '').strip().lower()}|{norm}"

    async def on_message(self, msg: ChatMessage) -> None:
        text = msg.text.strip()
        log.info("chat", extra={"sender": msg.sender_name, "text": text})

        # 1. Slash commands → host only
        if text.startswith("/"):
            if self._is_host(msg):
                await self.slash.handle(msg)
            return

        if self._quiet or not settings.answer_questions:
            return

        # Lazily initialize LLM/RAG in case ANSWER_QUESTIONS was toggled at runtime
        if self.brain is None:
            self.brain = ClaudeClient()
        if self.rag is None:
            self.rag = RAGRetriever()

        # 2. DM to bot? always reply.
        # 3. Public chat? only if it looks like a question.
        if not msg.is_private and not self._looks_like_question(text):
            return

        qkey = self._question_key(msg.sender_id or msg.sender_name, text)
        if qkey in self._handled_questions:
            log.info("skip already-handled question from %s", msg.sender_name)
            return

        # 4. Per-user rate limit
        if not self._check_rate(msg.sender_id):
            log.info("rate-limited %s", msg.sender_name)
            return

        # Claim before LLM so duplicate emissions of the same line don't
        # queue a second draft while the first is still generating.
        self._handled_questions.add(qkey)

        # 5. Get RAG context, ask brain
        try:
            assert self.rag is not None and self.brain is not None
            ctx = self.rag.retrieve(text)
            reply = await self.brain.reply(msg.sender_name, text, ctx)
        except Exception as e:
            # Allow a later retry if we never produced a draft.
            self._handled_questions.discard(qkey)
            log.exception("brain failed: %s", e)
            return

        # 6. Reply directly to the person who asked
        # Use sender_id for targeting. The Playwright bridge treats `to` as a
        # recipient key; sender_name can include extra UI text like
        # "To Hosts and Panelists..." which breaks matching.
        # CHAT_AUTO_SUBMIT=false → select name + type draft; human clicks Submit.
        # If the human deletes the draft without sending, we still keep qkey
        # handled and move on to newer questions.
        target = msg.sender_id or msg.sender_name
        try:
            await self.client.send_chat(
                reply, to=target, submit=settings.chat_auto_submit
            )
            if not settings.chat_auto_submit:
                log.info(
                    "draft ready for %s — click Submit or clear to skip",
                    msg.sender_name,
                )
        except Exception as e:
            self._handled_questions.discard(qkey)
            log.exception("send_chat failed: %s", e)

    def _is_host(self, msg: ChatMessage) -> bool:
        if not settings.host_email:
            return False
        return (msg.sender_email or "").lower() == settings.host_email.lower()

    def _looks_like_question(self, text: str) -> bool:
        low = text.lower()
        if settings.bot_display_name.lower() in low:
            return True
        return any(h in low for h in self.QUESTION_HINTS)

    def _check_rate(self, user_id: str) -> bool:
        q = self._answers[user_id]
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=60)
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= settings.answer_rate_limit_per_user_per_min:
            return False
        q.append(now)
        return True


# ============================================================
# Slash commands
# ============================================================

class SlashCommands:
    """
    Host-only chat commands:
      /schedule 5m <text> | /schedule 14:30 <text>
      /list | /cancel <id>
      /quiet | /resume
      /dm <name>: <text>
    """

    def __init__(self, client: ZoomClient, scheduler: "MessageScheduler") -> None:
        self.client = client
        self.scheduler = scheduler
        self.chat_handler: Optional[ChatHandler] = None
        self._participants: Dict[str, Participant] = {}

    def register_chat_handler(self, ch: ChatHandler) -> None:
        self.chat_handler = ch

    def remember_participant(self, p: Participant) -> None:
        self._participants[p.name.lower()] = p

    def forget_participant(self, p: Participant) -> None:
        self._participants.pop(p.name.lower(), None)

    async def handle(self, msg: ChatMessage) -> None:
        text = msg.text.strip()
        host_id = msg.sender_id

        async def reply(t: str) -> None:
            await self.client.send_chat(t, to=host_id)

        # /schedule <when> <text>
        m = re.match(r"^/schedule\s+(\S+)\s+(.+)$", text, re.DOTALL)
        if m:
            when_str, body = m.group(1), m.group(2).strip()
            try:
                fire_at = _parse_when(when_str)
            except ValueError as e:
                await reply(f"can't parse time '{when_str}': {e}")
                return
            sid = self.scheduler.schedule(fire_at, body)
            preview = body[:40] + ("..." if len(body) > 40 else "")
            await reply(
                f"✓ scheduled '{preview}' for "
                f"{fire_at.strftime('%H:%M:%S')} UTC (id: {sid})"
            )
            return

        if text == "/list":
            items = self.scheduler.list_pending()
            if not items:
                await reply("queue is empty.")
            else:
                lines = [
                    f"  {it.id}  {it.fire_at.strftime('%H:%M')}  {it.text[:50]}"
                    for it in items
                ]
                await reply("pending:\n" + "\n".join(lines))
            return

        m = re.match(r"^/cancel\s+(\S+)$", text)
        if m:
            ok = self.scheduler.cancel(m.group(1))
            await reply("cancelled." if ok else "id not found.")
            return

        if text == "/quiet":
            if self.chat_handler:
                self.chat_handler.set_quiet(True)
            await reply("auto-replies paused. /resume to re-enable.")
            return

        if text == "/resume":
            if self.chat_handler:
                self.chat_handler.set_quiet(False)
            await reply("auto-replies on.")
            return

        m = re.match(r"^/dm\s+([^:]+):\s*(.+)$", text)
        if m:
            name, body = m.group(1).strip().lower(), m.group(2).strip()
            target = self._participants.get(name)
            if not target:
                await reply(f"don't see anyone named '{name}' in the room.")
                return
            await self.client.send_chat(body, to=target.user_id)
            await reply(f"sent DM to {target.name}.")
            return

        await reply(
            "commands: /schedule <5m|14:30> <text>, /list, /cancel <id>, "
            "/quiet, /resume, /dm <name>: <text>"
        )


# ============================================================
# Message scheduler
# ============================================================

class _Scheduled:
    __slots__ = ("id", "fire_at", "text", "action", "poll_name")

    def __init__(
        self,
        sid: str,
        fire_at: datetime,
        text: str = "",
        *,
        action: str = "chat",
        poll_name: Optional[str] = None,
    ) -> None:
        self.id = sid
        self.fire_at = fire_at
        self.text = text
        self.action = action  # chat | poll_launch | poll_end
        self.poll_name = poll_name


class MessageScheduler:
    """In-memory queue. Reloads SCHEDULE_FILE whenever it changes on disk."""

    def __init__(self, client: ZoomClient) -> None:
        self.client = client
        self._items: Dict[str, _Scheduled] = {}
        self._file_ids: set[str] = set()
        self._file_path: Path | None = None
        self._tz_name: str = "Asia/Kolkata"
        self._file_mtime: float | None = None
        self._grace_past: bool = True  # only on first load; hot-reload skips past times
        # Remember successfully sent file rows so reconnect doesn't re-fire them.
        self._fired_keys: set[str] = set()
        self._row_key_by_id: Dict[str, str] = {}
        self._payment_drop_recorded: bool = False

    @staticmethod
    def _row_key(when: str, kind: str, payload: str) -> str:
        return f"{when}|{kind}|{payload}"

    def schedule(
        self,
        fire_at: datetime,
        text: str = "",
        *,
        from_file: bool = False,
        row_key: str | None = None,
        action: str = "chat",
        poll_name: Optional[str] = None,
    ) -> str:
        sid = uuid.uuid4().hex[:6]
        self._items[sid] = _Scheduled(
            sid, fire_at, text, action=action, poll_name=poll_name
        )
        if from_file:
            self._file_ids.add(sid)
        if row_key:
            self._row_key_by_id[sid] = row_key
        return sid

    def _parse_local_time(self, when: str, now_local: datetime):
        parts = [int(p) for p in when.split(":")]
        if len(parts) == 2:
            h, mi, sec = parts[0], parts[1], 0
        elif len(parts) == 3:
            h, mi, sec = parts
        else:
            raise ValueError(f"bad schedule time {when!r}")
        return now_local.replace(hour=h, minute=mi, second=sec, microsecond=0)

    def _apply_grace(
        self, when: str, target: datetime, now_local: datetime, use_grace: bool
    ) -> Optional[datetime]:
        if target > now_local:
            return target
        if use_grace and (now_local - target) <= timedelta(seconds=90):
            log.info("schedule %s missed — firing ASAP (grace)", when)
            return now_local + timedelta(seconds=5)
        log.warning("schedule %s already past — skipping", when)
        return None

    def load_file(
        self,
        path: Path,
        tz_name: str = "Asia/Kolkata",
        *,
        replace: bool = True,
        grace_past: bool | None = None,
        meeting_id: str = "",
    ) -> int:
        """Load schedule rows for one webinar only.

        Preferred shape (session-scoped):
          {
            "meeting_id": "87264482000",
            "items": [
              {"time":"19:00:00","text":"..."},
              {"time":"19:40:00","poll":"Exact Poll Name","poll_end":"19:50:00"}
            ]
          }

        A bare JSON array is rejected — schedules must declare meeting_id so they
        cannot accidentally fire on a different webinar.
        """
        import json
        from datetime import timezone as tz_utc

        self._file_path = path
        self._tz_name = tz_name
        use_grace = self._grace_past if grace_past is None else grace_past
        expected_meeting = (meeting_id or settings.meeting_id or "").strip()

        if not path.exists():
            log.warning("schedule file missing: %s", path)
            return 0

        try:
            self._file_mtime = path.stat().st_mtime
        except OSError:
            pass

        tz: object
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            offsets = {
                "Asia/Kolkata": timedelta(hours=5, minutes=30),
                "IST": timedelta(hours=5, minutes=30),
                "UTC": timedelta(0),
            }
            tz = tz_utc(offsets.get(tz_name, timedelta(hours=5, minutes=30)))
            log.warning("tzdata missing — using fixed offset for %s", tz_name)

        if replace:
            for sid in list(self._file_ids):
                self._items.pop(sid, None)
                self._row_key_by_id.pop(sid, None)
            self._file_ids.clear()

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("schedule.json invalid JSON: %s", e)
            return 0

        # Session scope: require explicit meeting_id match.
        if isinstance(raw, list):
            log.error(
                "schedule.json must be an object with meeting_id + items "
                "(refusing bare array so it cannot run on another webinar)"
            )
            return 0
        if not isinstance(raw, dict):
            log.error("schedule.json: expected object, got %s", type(raw).__name__)
            return 0

        file_meeting = str(
            raw.get("meeting_id") or raw.get("webinar_id") or ""
        ).strip()
        data = raw.get("items")
        if data is None:
            data = raw.get("messages")
        if not isinstance(data, list):
            log.error("schedule.json: missing items[] array")
            return 0

        if not file_meeting:
            log.error(
                "schedule.json missing meeting_id — pin it to this webinar "
                "before loading (e.g. \"meeting_id\": \"%s\")",
                expected_meeting or "<MEETING_ID>",
            )
            return 0
        if not expected_meeting:
            log.error("MEETING_ID not set — cannot verify schedule meeting_id")
            return 0
        if file_meeting != expected_meeting:
            log.error(
                "schedule.json is for meeting %s — current session is %s "
                "(not loading; update meeting_id or use a different schedule file)",
                file_meeting,
                expected_meeting,
            )
            return 0

        log.info(
            "loading schedule for meeting %s (%s)",
            file_meeting,
            raw.get("session") or "untitled",
        )

        now_local = datetime.now(tz)  # type: ignore[arg-type]
        loaded = 0

        def _enqueue(when: str, *, action: str, text: str = "", poll_name: str = "") -> bool:
            nonlocal loaded
            payload = poll_name if action.startswith("poll_") else text
            key = self._row_key(when, action, payload)
            if key in self._fired_keys:
                log.info("schedule %s (%s) already fired — skipping", when, action)
                return False
            try:
                target = self._parse_local_time(when, now_local)
            except ValueError as e:
                log.warning("%s", e)
                return False
            target = self._apply_grace(when, target, now_local, use_grace)
            if target is None:
                return False
            fire_utc = target.astimezone(tz_utc.utc).replace(tzinfo=None)
            sid = self.schedule(
                fire_utc,
                text,
                from_file=True,
                row_key=key,
                action=action,
                poll_name=poll_name or None,
            )
            log.info(
                "scheduled from file id=%s action=%s local=%s text=%r poll=%r",
                sid,
                action,
                target.isoformat(),
                (text or "")[:50],
                poll_name or None,
            )
            loaded += 1
            return True

        for row in data:
            if not isinstance(row, dict):
                continue
            when = str(row.get("time", "")).strip()
            text = str(row.get("text", "")).strip()
            poll_name = str(row.get("poll") or row.get("poll_name") or "").strip()
            poll_end = str(row.get("poll_end") or "").strip()
            if not when:
                continue

            if poll_name:
                _enqueue(when, action="poll_launch", poll_name=poll_name)
                if poll_end:
                    _enqueue(poll_end, action="poll_end", poll_name=poll_name)
                if text:
                    _enqueue(when, action="chat", text=text)
                continue

            if text:
                _enqueue(when, action="chat", text=text)
                continue

            log.warning("schedule row ignored (need text or poll): %s", row)

        self._grace_past = False
        return loaded

    def _maybe_reload_file(self) -> None:
        path = self._file_path
        if not path:
            return
        try:
            if not path.exists():
                return
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._file_mtime is not None and mtime == self._file_mtime:
            return
        # Debounce partial writes (editor save)
        import time
        time.sleep(0.15)
        try:
            mtime = path.stat().st_mtime
            raw = path.read_text(encoding="utf-8")
            import json
            json.loads(raw)  # don't reload mid-edit
        except Exception:
            return
        log.info("schedule.json changed — reloading")
        n = self.load_file(
            path,
            self._tz_name,
            replace=True,
            grace_past=False,
            meeting_id=settings.meeting_id,
        )
        log.info("reloaded %d scheduled item(s)", n)

    def cancel(self, sid: str) -> bool:
        self._file_ids.discard(sid)
        self._row_key_by_id.pop(sid, None)
        return self._items.pop(sid, None) is not None

    def list_pending(self) -> List[_Scheduled]:
        return sorted(self._items.values(), key=lambda x: x.fire_at)

    async def run(self) -> None:
        while True:
            self._maybe_reload_file()
            now = datetime.utcnow()
            due = [it for it in list(self._items.values()) if it.fire_at <= now]
            for it in due:
                try:
                    if it.action == "poll_launch":
                        await self.client.launch_poll(it.poll_name or "")
                        log.info("poll launched", extra={"id": it.id, "poll": it.poll_name})
                        await self._record_chat_drop(it, text=f"[poll] {it.poll_name or ''}")
                    elif it.action == "poll_end":
                        await self.client.end_poll(it.poll_name or "")
                        log.info("poll ended", extra={"id": it.id, "poll": it.poll_name})
                    else:
                        await self.client.send_chat(it.text, to="everyone")
                        log.info("scheduled fired", extra={"id": it.id})
                        await self._record_chat_drop(it, text=it.text)
                        if (
                            not self._payment_drop_recorded
                            and is_payment_link_message(it.text)
                        ):
                            await self._record_payment_drop_attendees(it)
                    key = self._row_key_by_id.get(it.id)
                    if key:
                        self._fired_keys.add(key)
                except Exception as e:
                    log.exception("scheduled action failed (%s): %s", it.action, e)
                self._file_ids.discard(it.id)
                self._row_key_by_id.pop(it.id, None)
                self._items.pop(it.id, None)
            await asyncio.sleep(2)

    async def _record_chat_drop(self, it: _Scheduled, *, text: str) -> None:
        schedule_time = ""
        key = self._row_key_by_id.get(it.id, "")
        if key:
            schedule_time = key.split("|", 1)[0]
        else:
            try:
                schedule_time = it.fire_at.astimezone().strftime("%H:%M:%S")
            except Exception:
                schedule_time = ""
        session_id = (
            Path(settings.schedule_file).stem
            if settings.schedule_file
            else settings.meeting_id or "unknown"
        )
        port = None
        try:
            from urllib.parse import urlparse

            port = urlparse(settings.bridge_url).port
        except Exception:
            pass
        try:
            record_chat_drop(
                session_id=session_id,
                meeting_id=settings.meeting_id or "",
                port=port,
                schedule_time=schedule_time,
                text=text,
                source="scheduler",
            )
        except Exception as e:
            log.warning("chat drop log failed: %s", e)

    async def _record_payment_drop_attendees(self, it: _Scheduled) -> None:
        """Snapshot attendees when the first payment link chat is sent."""
        counts: dict = {}
        try:
            raw = await self.client.eval_js(_ATTENDEE_COUNT_JS)
            if isinstance(raw, dict):
                counts = raw
        except Exception as e:
            log.warning("attendee snapshot failed: %s", e)
        # Prefer schedule clock time from row key (when|chat|text)
        schedule_time = ""
        key = self._row_key_by_id.get(it.id, "")
        if key:
            schedule_time = key.split("|", 1)[0]
        else:
            try:
                schedule_time = it.fire_at.astimezone().strftime("%H:%M:%S")
            except Exception:
                schedule_time = ""
        session_id = (
            Path(settings.schedule_file).stem
            if settings.schedule_file
            else settings.meeting_id or "unknown"
        )
        port = None
        try:
            from urllib.parse import urlparse

            port = urlparse(settings.bridge_url).port
        except Exception:
            pass
        record_payment_drop(
            session_id=session_id,
            meeting_id=settings.meeting_id or "",
            port=port,
            schedule_time=schedule_time,
            message_preview=it.text,
            counts=counts,
            source="scheduler",
        )
        self._payment_drop_recorded = True


# ============================================================
# Time parsing
# ============================================================

def _parse_when(s: str) -> datetime:
    """Accept '5m', '90s', '2h', or 'HH:MM' (24h, UTC today)."""
    now = datetime.utcnow()

    m = re.match(r"^(\d+)([smh])$", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
        }[unit]
        return now + delta

    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise ValueError("hour or minute out of range")
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    raise ValueError("use '5m', '90s', '2h' or 'HH:MM'")
