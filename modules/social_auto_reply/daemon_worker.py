"""Background worker for social auto-reply queues."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app.database import execute
from app.error_log import record_error

from . import backlog_processor, rate_limiter, send_queue, state_tracker
from .planner_worker import process_due_threads

log = logging.getLogger("fazle.social.daemon")

_worker_task: Optional[asyncio.Task] = None


async def _loop() -> None:
    log.info("[social] daemon worker started enabled=%s", state_tracker.daemon_enabled())
    idle_sleep = int(os.getenv("SOCIAL_REPLY_IDLE_SLEEP_S", "120"))
    backlog_every = int(os.getenv("SOCIAL_BACKLOG_SCAN_EVERY_S", "600"))
    last_backlog = 0.0
    while True:
        try:
            try:
                await execute(
                    """INSERT INTO fazle_service_heartbeats (service, last_seen, queue_depth)
                       VALUES ('social_auto_reply', NOW(), 0)
                       ON CONFLICT (service)
                       DO UPDATE SET last_seen = NOW(), queue_depth = EXCLUDED.queue_depth""",
                )
            except Exception:
                pass  # heartbeat failure is non-fatal — daemon continues
            if await state_tracker.is_paused():
                await asyncio.sleep(idle_sleep)
                continue
            now = asyncio.get_running_loop().time()
            if now - last_backlog >= backlog_every:
                await backlog_processor.scan_recent(limit=int(os.getenv("SOCIAL_BACKLOG_CONVERSATION_LIMIT", "10")))
                last_backlog = now
            await process_due_threads(limit=int(os.getenv("SOCIAL_PLANNER_THREAD_LIMIT", "10")))
            if not await rate_limiter.can_send("global"):
                await asyncio.sleep(5)
                continue
            result = await send_queue.sweep_once(limit=1)
            if result.get("sent"):
                delay = await rate_limiter.mark_sent("global")
                log.info("[social] sent one reply; rate delay=%ss", delay)
                await asyncio.sleep(delay)
            elif result.get("picked"):
                await asyncio.sleep(5)
            else:
                await asyncio.sleep(idle_sleep)
        except asyncio.CancelledError:
            log.info("[social] daemon worker cancelled")
            raise
        except Exception as exc:
            log.exception("[social] daemon worker error: %s", exc)
            await record_error("social.daemon", exc)
            await asyncio.sleep(30)


def start_background_worker() -> asyncio.Task:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(_loop(), name="social-auto-reply-worker")
    return _worker_task


async def stop_background_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _worker_task = None
