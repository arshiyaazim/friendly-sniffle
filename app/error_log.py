"""
Fazle Core — error_log helper (Batch 15.4)

Records exceptions into fazle_error_log with UPSERT semantics.
Failsafe: never raises.
"""
from __future__ import annotations

import logging

log = logging.getLogger("fazle.error_log")


async def record_error(module: str, exc: BaseException, message: str | None = None) -> None:
    """UPSERT (module, error_type, msg) — increments count, updates last_seen."""
    try:
        from app.database import execute  # local import to avoid startup cycles
        err_type = type(exc).__name__
        msg = (message if message is not None else str(exc))[:1000]
        await execute(
            """INSERT INTO fazle_error_log (module, error_type, message, count, first_seen, last_seen)
               VALUES ($1, $2, $3, 1, NOW(), NOW())
               ON CONFLICT (module, error_type, md5(COALESCE(message,'')))
               DO UPDATE SET count = fazle_error_log.count + 1, last_seen = NOW()""",
            module[:80], err_type[:80], msg,
        )
    except Exception as e:  # never raise
        log.warning(f"[error_log] failed to record error from {module}: {e}")
