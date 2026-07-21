"""
Entry point. Wires the client, brain, and handlers together,
joins a meeting, and runs until the meeting ends.

Usage:
  ZOOM_BACKEND=dry_run python -m src.main
  ZOOM_BACKEND=bridge MEETING_ID=123 MEETING_PASSWORD=xxx python -m src.main
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from urllib.parse import parse_qs, urlparse

from .config import settings
from .handlers import ChatHandler, Greeter, MessageScheduler, SlashCommands
from .zoom_client import make_client


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


async def main() -> int:
    _setup_logging()
    log = logging.getLogger("main")

    # If a full join URL is provided, parse meeting id / pwd / panelist tk token from it.
    # This lets you paste a single Zoom panelist link into .env as MEETING_JOIN_URL.
    if settings.meeting_join_url:
        try:
            u = urlparse(settings.meeting_join_url)
            q = parse_qs(u.query)
            # Common Zoom params:
            # - pwd= passcode
            # - tk= webinar panelist token
            # - joinConfNo= meeting/webinar id (sometimes present)
            # Treat the join URL as source of truth when it provides fields.
            # This avoids stale env vars overriding a fresh panelist link.
            if q.get("pwd"):
                settings.meeting_password = q["pwd"][0]
            if q.get("tk"):
                settings.meeting_webinar_token = q["tk"][0]

            if q.get("joinConfNo"):
                settings.meeting_id = q["joinConfNo"][0]
            else:
                # Fallback: /j/<id> or /s/<id> or /w/<id> in path
                parts = [p for p in u.path.split("/") if p]
                for i, part in enumerate(parts):
                    if part in {"j", "s", "w"} and i + 1 < len(parts):
                        cand = parts[i + 1]
                        if cand.isdigit():
                            settings.meeting_id = cand
                            break
        except Exception as e:
            log.warning("failed to parse MEETING_JOIN_URL: %s", e)

    if settings.answer_questions and not settings.openrouter_api_key:
        print(
            "ERROR: OPENROUTER_API_KEY not set in .env (required when ANSWER_QUESTIONS=true)",
            file=sys.stderr,
        )
        return 1

    client = make_client()
    scheduler = MessageScheduler(client)
    slash = SlashCommands(client, scheduler)
    chat = ChatHandler(client, slash)
    slash.register_chat_handler(chat)
    greeter = Greeter(client)

    # Wire callbacks
    client.on_chat_message(chat.on_message)

    async def on_join(p):
        slash.remember_participant(p)
        await greeter.on_join(p)

    async def on_leave(p):
        slash.forget_participant(p)

    client.on_participant_joined(on_join)
    client.on_participant_left(on_leave)

    # Graceful shutdown
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows

    try:
        await client.join(settings.meeting_id, settings.meeting_password)
    except Exception as e:
        log.exception("failed to join meeting: %s", e)
        return 2

    log.info("bot is live as '%s'", settings.bot_display_name)

    sched_task = asyncio.create_task(scheduler.run())
    run_task = asyncio.create_task(client.run())

    try:
        # Run until either the client exits or we get a signal
        done, pending = await asyncio.wait(
            [run_task, asyncio.create_task(stop.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        sched_task.cancel()
        run_task.cancel()
        try:
            await client.leave()
        except Exception:
            pass
        log.info("shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
