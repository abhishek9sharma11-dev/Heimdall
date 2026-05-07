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
from typing import Deque, Dict, List, Optional

from .brain import ClaudeClient, RAGRetriever
from .config import settings
from .zoom_client import ChatMessage, Participant, ZoomClient

log = logging.getLogger(__name__)


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
        self.brain = ClaudeClient()
        self.rag = RAGRetriever()
        self._answers: Dict[str, Deque[datetime]] = defaultdict(deque)
        self._quiet = False

    def set_quiet(self, q: bool) -> None:
        self._quiet = q

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

        # 2. DM to bot? always reply.
        # 3. Public chat? only if it looks like a question.
        if not msg.is_private and not self._looks_like_question(text):
            return

        # 4. Per-user rate limit
        if not self._check_rate(msg.sender_id):
            log.info("rate-limited %s", msg.sender_name)
            return

        # 5. Get RAG context, ask brain
        try:
            ctx = self.rag.retrieve(text)
            reply = await self.brain.reply(msg.sender_name, text, ctx)
        except Exception as e:
            log.exception("brain failed: %s", e)
            return

        # 6. Reply directly to the person who asked
        target = msg.sender_name
        try:
            await self.client.send_chat(reply, to=target)
        except Exception as e:
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
    __slots__ = ("id", "fire_at", "text")

    def __init__(self, sid: str, fire_at: datetime, text: str) -> None:
        self.id = sid
        self.fire_at = fire_at
        self.text = text


class MessageScheduler:
    """In-memory queue. For persistence, swap to APScheduler+SQLite."""

    def __init__(self, client: ZoomClient) -> None:
        self.client = client
        self._items: Dict[str, _Scheduled] = {}

    def schedule(self, fire_at: datetime, text: str) -> str:
        sid = uuid.uuid4().hex[:6]
        self._items[sid] = _Scheduled(sid, fire_at, text)
        return sid

    def cancel(self, sid: str) -> bool:
        return self._items.pop(sid, None) is not None

    def list_pending(self) -> List[_Scheduled]:
        return sorted(self._items.values(), key=lambda x: x.fire_at)

    async def run(self) -> None:
        while True:
            now = datetime.utcnow()
            due = [it for it in list(self._items.values()) if it.fire_at <= now]
            for it in due:
                try:
                    await self.client.send_chat(it.text, to="everyone")
                    log.info("scheduled fired", extra={"id": it.id})
                except Exception as e:
                    log.exception("scheduled send failed: %s", e)
                self._items.pop(it.id, None)
            await asyncio.sleep(2)


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
