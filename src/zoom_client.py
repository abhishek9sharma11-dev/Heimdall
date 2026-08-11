"""
Python-side client. Two implementations:

- DryRunClient: terminal simulation, no bridge. Use for iterating on persona.
- BridgeClient: HTTP/SSE client that talks to the C++ bridge daemon.

The bridge daemon (./bridge/) is what actually touches the Zoom SDK.
This file never imports any Zoom code — it only speaks JSON over HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Literal, Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)


# ---- Event types --------------------------------------------------------

@dataclass
class ChatMessage:
    sender_name: str
    sender_id: str
    sender_email: Optional[str]
    text: str
    is_private: bool = False
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Participant:
    name: str
    user_id: str
    email: Optional[str]
    role: Literal["host", "co-host", "panelist", "attendee"] = "attendee"


ChatCallback = Callable[[ChatMessage], Awaitable[None]]
JoinCallback = Callable[[Participant], Awaitable[None]]
LeaveCallback = Callable[[Participant], Awaitable[None]]


# ---- Abstract base ------------------------------------------------------

class ZoomClient(ABC):
    def __init__(self) -> None:
        self._chat_cb: Optional[ChatCallback] = None
        self._join_cb: Optional[JoinCallback] = None
        self._leave_cb: Optional[LeaveCallback] = None

    def on_chat_message(self, cb: ChatCallback) -> None:
        self._chat_cb = cb

    def on_participant_joined(self, cb: JoinCallback) -> None:
        self._join_cb = cb

    def on_participant_left(self, cb: LeaveCallback) -> None:
        self._leave_cb = cb

    @abstractmethod
    async def join(self, meeting_id: str, password: str = "") -> None: ...

    @abstractmethod
    async def send_chat(
        self, text: str, to: str = "everyone", *, submit: bool = True
    ) -> None: ...

    async def launch_poll(self, name: str) -> None:
        raise NotImplementedError("launch_poll not supported on this backend")

    async def end_poll(self, name: str = "") -> None:
        raise NotImplementedError("end_poll not supported on this backend")

    async def eval_js(self, code: str) -> object:
        """Optional: evaluate JS in the live Zoom page (Playwright bridge)."""
        return None

    @abstractmethod
    async def leave(self) -> None: ...

    @abstractmethod
    async def run(self) -> None: ...


# ---- DryRun: local terminal -------------------------------------------

class DryRunClient(ZoomClient):
    """Type chat messages in your terminal as if you were an attendee."""

    def __init__(self) -> None:
        super().__init__()
        self._running = False

    async def join(self, meeting_id: str, password: str = "") -> None:
        print(f"\n[dry-run] {settings.bot_display_name} joined webinar {meeting_id or '(none)'}")
        print(f"[dry-run] Disclosure: {settings.bot_disclosure_line}\n")
        self._running = True
        if self._join_cb:
            await self._join_cb(Participant(
                name="Test Host", user_id="host_001",
                email=settings.host_email, role="host",
            ))

    async def send_chat(
        self, text: str, to: str = "everyone", *, submit: bool = True
    ) -> None:
        prefix = "[bot → all]" if to == "everyone" else f"[bot → {to}]"
        mode = "" if submit else " [DRAFT — click Submit]"
        print(f"{prefix}{mode} {text}")

    async def launch_poll(self, name: str) -> None:
        print(f"[dry-run] launch poll: {name}")

    async def end_poll(self, name: str = "") -> None:
        print(f"[dry-run] end poll: {name or '(current)'}")

    async def eval_js(self, code: str) -> object:
        return {"footer": 0, "attendees": 0, "participants": 0}

    async def leave(self) -> None:
        self._running = False
        print("[dry-run] bot left")

    async def run(self) -> None:
        print("Type chat as an attendee. 'quit' to exit.")
        print("Format: 'name: message' or just 'message'\n")
        loop = asyncio.get_running_loop()
        while self._running:
            line = await loop.run_in_executor(None, input, "> ")
            if line.strip().lower() in {"quit", "exit"}:
                break
            if not line.strip():
                continue
            if ":" in line:
                name, _, text = line.partition(":")
                name, text = name.strip(), text.strip()
            else:
                name, text = "attendee_1", line.strip()
            msg = ChatMessage(
                sender_name=name,
                sender_id=f"user_{name}",
                sender_email=settings.host_email if name.lower() == "host" else None,
                text=text,
            )
            if self._chat_cb:
                await self._chat_cb(msg)
        await self.leave()


# ---- BridgeClient: HTTP + SSE ------------------------------------------

class BridgeClient(ZoomClient):
    """
    Talks to the C++ bridge. Wire protocol:

    Commands (Python → bridge):
      POST /join        {meeting_id, password, zak, display_name, avatar_url}
      POST /send_chat   {text, to}
      POST /leave       {}

    Events (bridge → Python, via SSE on /events):
      data: {"type":"chat_message", "sender_name":..., "sender_id":...,
             "sender_email":..., "text":..., "is_private":bool}
      data: {"type":"participant_joined", "name":..., "user_id":..., "role":...}
      data: {"type":"participant_left", "name":..., "user_id":...}
      data: {"type":"meeting_ended"}
    """

    def __init__(self) -> None:
        super().__init__()
        self._cmd = httpx.AsyncClient(base_url=settings.bridge_url, timeout=60.0)
        self._sse: Optional[httpx.AsyncClient] = None
        self._stop_event = asyncio.Event()
        self._meeting_id: str = ""
        self._password: str = ""
        self._reconnect_lock = asyncio.Lock()

    async def join(self, meeting_id: str, password: str = "", *, force: bool = False) -> None:
        self._meeting_id = meeting_id
        self._password = password
        # Orchestrator restarts: keep the Playwright session if already joined.
        if not force:
            try:
                h = await self._cmd.get("/health")
                if h.status_code == 200:
                    body = h.json()
                    if body.get("in_meeting"):
                        log.info("bridge already in meeting — skipping /join")
                        return
            except Exception:
                pass
        payload = {
            "meeting_id": meeting_id,
            "password": password,
            "zak": settings.meeting_zak,
            "display_name": settings.bot_display_name,
            "avatar_url": settings.bot_avatar_url,
            "webinar_token": settings.meeting_webinar_token,
            "join_url": settings.meeting_join_url,
        }
        r = await self._cmd.post("/join", json=payload)
        if r.status_code != 200:
            log.error("bridge /join error %s: %s", r.status_code, r.text)
        r.raise_for_status()

    async def reconnect(self) -> None:
        """Ask the bridge to recover (click Join or full rejoin)."""
        async with self._reconnect_lock:
            try:
                r = await self._cmd.post("/reconnect", json={})
                r.raise_for_status()
                log.info("requested bridge reconnect: %s", r.json())
            except Exception as e:
                log.warning("reconnect endpoint failed (%s) — forcing /join", e)
                if self._meeting_id:
                    await self.join(self._meeting_id, self._password, force=True)

    async def send_chat(
        self, text: str, to: str = "everyone", *, submit: bool = True
    ) -> None:
        payload = {"text": text, "to": to, "submit": submit}
        try:
            r = await self._cmd.post("/send_chat", json=payload)
            r.raise_for_status()
            return
        except Exception as e:
            log.warning("send_chat failed (%s) — reconnect then retry once", e)
            await self.reconnect()
            await asyncio.sleep(3)
            r = await self._cmd.post("/send_chat", json=payload)
            r.raise_for_status()

    async def launch_poll(self, name: str) -> None:
        r = await self._cmd.post("/poll/launch", json={"name": name}, timeout=60.0)
        if r.status_code != 200:
            log.error("poll launch failed %s: %s", r.status_code, r.text)
        r.raise_for_status()

    async def end_poll(self, name: str = "") -> None:
        r = await self._cmd.post("/poll/end", json={"name": name}, timeout=60.0)
        if r.status_code != 200:
            log.error("poll end failed %s: %s", r.status_code, r.text)
        r.raise_for_status()

    async def eval_js(self, code: str) -> object:
        r = await self._cmd.post("/eval", json={"code": code}, timeout=20.0)
        r.raise_for_status()
        return (r.json() or {}).get("result")

    async def leave(self) -> None:
        try:
            await self._cmd.post("/leave")
        finally:
            await self._cmd.aclose()
            self._stop_event.set()

    async def run(self) -> None:
        """Connect to /events SSE stream and dispatch."""
        self._sse = httpx.AsyncClient(base_url=settings.bridge_url, timeout=None)
        try:
            async with self._sse.stream("GET", "/events") as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(line[6:])
                    except json.JSONDecodeError:
                        log.warning("bad sse line: %s", line)
                        continue
                    await self._dispatch(evt)
                    if self._stop_event.is_set():
                        break
        except (httpx.ReadError, asyncio.CancelledError):
            pass
        finally:
            await self._sse.aclose()

    async def _dispatch(self, evt: dict) -> None:
        t = evt.get("type")
        if t == "chat_message" and self._chat_cb:
            await self._chat_cb(ChatMessage(
                sender_name=evt["sender_name"],
                sender_id=evt["sender_id"],
                sender_email=evt.get("sender_email"),
                text=evt["text"],
                is_private=evt.get("is_private", False),
            ))
        elif t == "participant_joined" and self._join_cb:
            await self._join_cb(Participant(
                name=evt["name"], user_id=evt["user_id"],
                email=evt.get("email"),
                role=evt.get("role", "attendee"),
            ))
        elif t == "participant_left" and self._leave_cb:
            await self._leave_cb(Participant(
                name=evt["name"], user_id=evt["user_id"],
                email=evt.get("email"),
                role=evt.get("role", "attendee"),
            ))
        elif t in {"disconnected", "reconnecting"}:
            log.warning("bridge %s: %s", t, evt)
            if t == "disconnected":
                # Let the bridge watchdog recover; nudge it explicitly too.
                asyncio.create_task(self.reconnect())
        elif t in {"reconnected", "joined"}:
            log.info("bridge %s: %s", t, evt)
        elif t == "meeting_ended":
            # Explicit /leave only — do not tear down on transient drops.
            log.info("bridge meeting_ended (explicit leave)")
            self._stop_event.set()


# ---- Factory ----------------------------------------------------------

def make_client() -> ZoomClient:
    if settings.zoom_backend == "dry_run":
        return DryRunClient()
    if settings.zoom_backend == "bridge":
        return BridgeClient()
    raise ValueError(f"unknown backend: {settings.zoom_backend}")
