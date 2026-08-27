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
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import settings
from .handlers import ChatHandler, Greeter, MessageScheduler, SlashCommands
from .zoom_client import make_client


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


async def _wait_for_webinar_join(log: logging.Logger) -> None:
    raw = (settings.webinar_start_at or "").strip()
    if not raw:
        log.warning("WEBINAR_START_AT not set; joining immediately")
        return
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(settings.schedule_tz))
        join_at = start - timedelta(minutes=settings.webinar_join_lead_minutes)
    except (ValueError, TypeError) as e:
        raise ValueError(
            "WEBINAR_START_AT must be ISO datetime, e.g. 2026-08-16T19:00:00"
        ) from e
    delay = (join_at - datetime.now(start.tzinfo)).total_seconds()
    if delay <= 0:
        log.info("webinar join time has arrived; joining now")
        return
    log.info("webinar starts at %s; waiting %.0f minutes to join", start.isoformat(), delay / 60)
    await asyncio.sleep(delay)


async def _wait_for_webinar_end(log: logging.Logger, stop: asyncio.Event) -> None:
    raw = (settings.webinar_end_at or "").strip()
    if not raw:
        return
    try:
        end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=ZoneInfo(settings.schedule_tz))
    except (ValueError, TypeError):
        log.error("WEBINAR_END_AT must be ISO datetime, e.g. 2026-08-16T17:37:00")
        return
    delay = (end - datetime.now(end.tzinfo)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    log.info("webinar end time reached; leaving Zoom and stopping reconnects")
    stop.set()


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

    _local = any(
        h in (settings.openrouter_base_url or "").lower()
        for h in ("127.0.0.1", "localhost", "0.0.0.0")
    )
    if settings.answer_questions and not settings.openrouter_api_key and not _local:
        log.warning(
            "OPENROUTER_API_KEY not set; joining with AI answers disabled. "
            "Scheduled chat still runs. Set the key to enable answers."
        )
        settings.answer_questions = False

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
        await _wait_for_webinar_join(log)
        await client.join(settings.meeting_id, settings.meeting_password)
        await client.wait_until_ready()
    except Exception as e:
        log.exception("failed to join meeting: %s", e)
        return 2

    log.info("bot is live as '%s'", settings.bot_display_name)

    if settings.schedule_file and str(settings.schedule_file):
        n = scheduler.load_file(
            settings.schedule_file,
            settings.schedule_tz,
            meeting_id=settings.meeting_id,
        )
        log.info("loaded %d scheduled message(s) from %s", n, settings.schedule_file)

    sched_task = asyncio.create_task(scheduler.run())
    run_task = asyncio.create_task(client.run())
    end_task = asyncio.create_task(_wait_for_webinar_end(log, stop))

    try:
        # Run until either the client exits or we get a signal
        done, pending = await asyncio.wait(
            [run_task, asyncio.create_task(stop.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        sched_task.cancel()
        end_task.cancel()
        run_task.cancel()
        try:
            await client.leave()
        except Exception:
            pass
        log.info("shutdown complete")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
