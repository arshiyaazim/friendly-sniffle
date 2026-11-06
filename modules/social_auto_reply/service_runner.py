"""Standalone process entrypoint for the social auto-reply daemon."""
from __future__ import annotations

import asyncio
import logging
import signal

from app.database import close_db, init_db
from app.logging_setup import setup_logging

from . import start_social_auto_reply, stop_social_auto_reply


async def _main() -> None:
    setup_logging()
    log = logging.getLogger("fazle.social.service")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    await init_db()
    try:
        await start_social_auto_reply()
        log.info("[social] standalone daemon ready")
        await stop_event.wait()
    finally:
        await stop_social_auto_reply()
        await close_db()
        log.info("[social] standalone daemon stopped")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
