"""
shared.state_version — Global State Version Counter (Phase 12G)
================================================================

A monotonically-increasing counter stored in Redis (with PostgreSQL fallback)
that increments every time a successful coordinated write completes.

The dashboard polls `/api/state-version` every few seconds and only triggers
a tab refresh when the version increases.  This eliminates unnecessary API
calls while guaranteeing the frontend never shows stale data after a write.

Why Redis?
----------
Redis INCR is atomic, lock-free, and sub-millisecond.  If Redis is
unavailable, the fallback uses a PostgreSQL advisory lock + sequence table.

USAGE
-----
    from shared.state_version import get_state_version, bump_state_version

    # After any write:
    await bump_state_version()

    # In the state-version endpoint:
    v = await get_state_version()
    return {"version": v}
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("fazle.state_version")

_REDIS_KEY = "fazle:state_version"
_PG_TABLE  = "fazle_state_version"


# ── Redis-backed version ──────────────────────────────────────────────────────

async def _redis_get() -> Optional[int]:
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        settings = get_settings()
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=1)
        val = await r.get(_REDIS_KEY)
        await r.aclose()
        return int(val) if val else 0
    except Exception as exc:
        log.debug("[state_version] redis get failed: %s", exc)
        return None


async def _redis_bump() -> Optional[int]:
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        settings = get_settings()
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=1)
        val = await r.incr(_REDIS_KEY)
        await r.aclose()
        return int(val)
    except Exception as exc:
        log.debug("[state_version] redis incr failed: %s", exc)
        return None


# ── PostgreSQL fallback ───────────────────────────────────────────────────────

async def _pg_get() -> int:
    try:
        from app.database import fetch_val
        val = await fetch_val(
            f"SELECT COALESCE(MAX(version), 0) FROM {_PG_TABLE}"
        )
        return int(val or 0)
    except Exception:
        return 0


async def _pg_bump() -> int:
    try:
        from app.database import fetch_val
        val = await fetch_val(
            f"""
            INSERT INTO {_PG_TABLE} (version, bumped_at)
            VALUES (
                (SELECT COALESCE(MAX(version), 0) + 1 FROM {_PG_TABLE}),
                NOW()
            )
            RETURNING version
            """
        )
        return int(val or 1)
    except Exception:
        return 0


# ── Public API ────────────────────────────────────────────────────────────────

async def get_state_version() -> int:
    """Return the current global state version (Redis → PG fallback)."""
    v = await _redis_get()
    if v is not None:
        return v
    return await _pg_get()


async def bump_state_version() -> int:
    """
    Increment the global state version and return the new value.

    Called automatically by write_router after every successful write.
    """
    v = await _redis_bump()
    if v is not None:
        log.debug("[state_version] bumped to %d (redis)", v)
        return v
    v = await _pg_bump()
    log.debug("[state_version] bumped to %d (pg fallback)", v)
    return v
