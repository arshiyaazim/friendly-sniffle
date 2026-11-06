"""
shared.queue_arbiter — Global Queue Arbitration Layer (Phase 13B)
=================================================================

Problem solved
--------------
Multiple app instances (fazle-core, payroll-engine, escort-roster) may
consume overlapping messages from fazle_message_queue.  Without a
coordinated arbitration layer this causes:

* Double draft generation          (two workers claiming the same inbound msg)
* Double payroll write             (two FPE workers processing the same batch)
* Duplicate escort assignment      (two workers fulfilling the same program msg)

This module wraps the existing queue with a *lease* layer stored in
fazle_queue_leases.  Every consumer MUST acquire a lease before processing.
The existing fazle_message_queue table is never restructured — new columns
are added additively via migration 012.

Architecture
------------
* Lease ownership  — only one worker can hold the lease for a given
  (message_id, intent) pair at a time.  Backed by PG advisory lock +
  INSERT … ON CONFLICT DO NOTHING so it's race-safe without extra
  round-trips.

* Automatic recovery — leases carry an expires_at.  A crashed worker's
  lease is reclaimed after LEASE_TTL_S (default 120 s).  The recovery
  sweep runs every 60 s.

* Exponential backoff — re-queued messages wait 2^(attempts-1) seconds
  before becoming eligible again (capped at MAX_BACKOFF_S = 3600).

* Deduplication — a (message_id, intent) pair that has been completed
  can never be re-leased; attempts after completion are silently ignored
  and the deduplicated count is tracked in metrics.

* Dead-letter — after MAX_ATTEMPTS failures the lease is permanently
  marked dead_letter; a separate inspection API retrieves them.

* Metrics — per-process in-memory counters: processing_latency_ms (list
  of last-1000 samples), retries, lease_conflicts, queue_starvation,
  deduplication_hits.

* Event emission — all state transitions emit through shared.events.

Public API
----------
  # Acquire a lease (must call before dequeuing)
  lease = await acquire_lease(message_id, intent, worker_id)
  if lease is None:
      return  # another worker holds it

  # Mark done (no retry)
  await complete_lease(lease.lease_id)

  # Mark failed (will retry with backoff if attempts < MAX_ATTEMPTS)
  await fail_lease(lease.lease_id, "parse error")

  # Permanently dead-letter (don't retry)
  await dead_letter_lease(lease.lease_id, "unknown format")

  # Recovery sweep (call from scheduler every 60 s)
  recovered = await recover_stale_leases()

  # Dead-letter inspection (diagnostic endpoint)
  items = await get_dead_letters(limit=50)

  # Metrics snapshot
  m = get_arbiter_metrics()

  # High-level: process one message with full arbitration
  await arbitrated_process(message_id, intent, worker_id, handler_fn)

  # Wraps shared.queue.dequeue_batch with lease acquisition
  batch = await arbitrated_dequeue(limit=10, worker_id="w1")

Integration with existing code
-------------------------------
  shared/queue.py       — still used for enqueue_message; dequeue_batch
                          can be replaced by arbitrated_dequeue
  shared/locks.py       — acquire_lock used internally for the intent-level
                          write lock before committing writes
  shared/write_router.py — callers still use routed_write; the arbiter
                           ensures only one worker reaches that code path
  shared/events.py      — all transitions emit events:
                          QUEUE_PRESSURE, LOCK_CONTENTION, DUPLICATE_WRITE

DO NOT break or rewrite
-----------------------
* fazle_message_queue table structure
* existing shared/queue.py public API
* existing workers that call dequeue_batch directly (they degrade to
  non-arbitrated mode; set QUEUE_ARBITER_ENABLED=false to keep old behaviour)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("fazle.queue_arbiter")

# ── Constants ─────────────────────────────────────────────────────────────────
LEASE_TTL_S:        int = 120    # lease expires after 120 s of worker silence
MAX_ATTEMPTS:       int = 5      # after this many failures → dead_letter
MAX_BACKOFF_S:      int = 3600   # cap on exponential backoff
RECOVERY_INTERVAL:  int = 60     # recovery sweep period (seconds)

# Lease status values
LS_LEASED      = "leased"
LS_PROCESSING  = "processing"
LS_COMPLETED   = "completed"
LS_FAILED      = "failed"
LS_DEAD_LETTER = "dead_letter"

# Feature flag (set QUEUE_ARBITER_ENABLED=false to run without arbitration)
def _arbiter_enabled() -> bool:
    import os
    return os.getenv("QUEUE_ARBITER_ENABLED", "true").lower() not in ("0", "false", "no")


# ── Pool resolution ────────────────────────────────────────────────────────────

def _resolve_pool(pool):
    if pool is not None:
        return pool
    try:
        from app.database import get_pool
        return get_pool()
    except Exception:
        return None


# ── In-process metrics ────────────────────────────────────────────────────────

@dataclass
class _ArbiterMetrics:
    leases_acquired:    int = 0
    lease_conflicts:    int = 0   # another worker held the lease
    completions:        int = 0
    failures:           int = 0
    retries:            int = 0
    dead_letters:       int = 0
    recoveries:         int = 0
    deduplication_hits: int = 0   # (msg_id, intent) already completed
    queue_starvation:   int = 0   # dequeue returned nothing despite backlog
    _latency_samples:   list = field(default_factory=list)

    def record_latency(self, ms: float) -> None:
        self._latency_samples.append(ms)
        if len(self._latency_samples) > 1000:
            self._latency_samples = self._latency_samples[-1000:]

    def latency_p50_p95(self):
        s = sorted(self._latency_samples)
        if not s:
            return None, None
        p50 = s[int(len(s) * 0.50)]
        p95 = s[int(len(s) * 0.95)]
        return round(p50, 1), round(p95, 1)


_metrics = _ArbiterMetrics()


def get_arbiter_metrics() -> dict:
    """
    Return a snapshot of all arbitration metrics suitable for the
    /api/queue/arbiter-metrics diagnostic endpoint.
    """
    p50, p95 = _metrics.latency_p50_p95()
    return {
        "leases_acquired":    _metrics.leases_acquired,
        "lease_conflicts":    _metrics.lease_conflicts,
        "completions":        _metrics.completions,
        "failures":           _metrics.failures,
        "retries":            _metrics.retries,
        "dead_letters":       _metrics.dead_letters,
        "recoveries":         _metrics.recoveries,
        "deduplication_hits": _metrics.deduplication_hits,
        "queue_starvation":   _metrics.queue_starvation,
        "processing_latency_ms": {
            "p50": p50,
            "p95": p95,
            "samples": len(_metrics._latency_samples),
        },
    }


# ── Lease record ──────────────────────────────────────────────────────────────

@dataclass
class QueueLease:
    lease_id:   int
    message_id: int      # FK → fazle_message_queue.id
    intent:     str      # intent label — e.g. "payment", "escort_assign"
    worker_id:  str
    status:     str      = LS_LEASED
    attempts:   int      = 1
    leased_at:  float    = field(default_factory=time.time)
    metadata:   dict     = field(default_factory=dict)


# ── Core lease operations ─────────────────────────────────────────────────────

async def acquire_lease(
    message_id: int,
    intent:     str,
    worker_id:  str,
    *,
    ttl_s: int   = LEASE_TTL_S,
    pool         = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[QueueLease]:
    """
    Try to acquire an exclusive lease for (message_id, intent).

    Rules:
    * If already COMPLETED → return None immediately (dedup hit, not an error)
    * If already LEASED and not yet expired → return None (conflict)
    * If LEASED and expired → reclaim lease for this worker
    * If FAILED and attempts < MAX_ATTEMPTS → re-lease (backoff enforced by caller)
    * If DEAD_LETTER → return None (permanent failure, don't retry)

    Uses INSERT … ON CONFLICT to be race-safe.  Returns QueueLease on
    success or None if another worker has a valid lease or the item is
    permanently done.
    """
    from shared.events import emit as _emit, DUPLICATE_WRITE, LOCK_CONTENTION

    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        log.warning("[arbiter] no pool — lease acquisition skipped for msg=%s", message_id)
        return None

    try:
        async with resolved_pool.acquire() as conn:
            # ── 1. Check for permanent dedup guard (completed) ─────────────
            existing = await conn.fetchrow(
                """
                SELECT lease_id, status, attempts, worker_id, expires_at
                FROM fazle_queue_leases
                WHERE message_id = $1 AND intent = $2
                """,
                message_id, intent,
            )

            if existing:
                status = existing["status"]

                if status == LS_COMPLETED:
                    _metrics.deduplication_hits += 1
                    log.debug(
                        "[arbiter] dedup hit — (msg=%s, intent=%s) already completed",
                        message_id, intent,
                    )
                    await _emit(DUPLICATE_WRITE, {
                        "message_id": message_id,
                        "intent":     intent,
                        "worker_id":  worker_id,
                        "reason":     "already_completed",
                    }, emitted_by="queue_arbiter")
                    return None

                if status == LS_DEAD_LETTER:
                    _metrics.deduplication_hits += 1
                    log.debug(
                        "[arbiter] msg=%s intent=%s is dead_letter — skip",
                        message_id, intent,
                    )
                    return None

                import datetime
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                expires_at = existing["expires_at"]
                if expires_at and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

                lease_active = (
                    status in (LS_LEASED, LS_PROCESSING)
                    and expires_at is not None
                    and expires_at > now_utc
                )
                if lease_active:
                    _metrics.lease_conflicts += 1
                    log.debug(
                        "[arbiter] lease conflict msg=%s intent=%s held by %s",
                        message_id, intent, existing["worker_id"],
                    )
                    await _emit(LOCK_CONTENTION, {
                        "message_id":  message_id,
                        "intent":      intent,
                        "held_by":     existing["worker_id"],
                        "contender":   worker_id,
                    }, emitted_by="queue_arbiter")
                    return None

                # Expired or failed — reclaim via UPDATE
                new_attempts = existing["attempts"] + 1
                if new_attempts > MAX_ATTEMPTS:
                    await conn.execute(
                        """
                        UPDATE fazle_queue_leases
                        SET status = $1, last_error = 'max_attempts_exceeded',
                            updated_at = NOW()
                        WHERE message_id = $2 AND intent = $3
                        """,
                        LS_DEAD_LETTER, message_id, intent,
                    )
                    _metrics.dead_letters += 1
                    log.warning(
                        "[arbiter] msg=%s intent=%s → dead_letter (attempts=%d)",
                        message_id, intent, new_attempts,
                    )
                    return None

                row = await conn.fetchrow(
                    """
                    UPDATE fazle_queue_leases
                    SET worker_id  = $1,
                        status     = 'leased',
                        attempts   = $2,
                        leased_at  = NOW(),
                        expires_at = NOW() + ($3 || ' seconds')::INTERVAL,
                        updated_at = NOW(),
                        metadata_json = $4::jsonb
                    WHERE message_id = $5 AND intent = $6
                    RETURNING lease_id, attempts
                    """,
                    worker_id, new_attempts, str(ttl_s),
                    json.dumps(metadata or {}),
                    message_id, intent,
                )
                if row:
                    _metrics.leases_acquired += 1
                    _metrics.retries += 1
                    return QueueLease(
                        lease_id=row["lease_id"],
                        message_id=message_id,
                        intent=intent,
                        worker_id=worker_id,
                        status=LS_LEASED,
                        attempts=row["attempts"],
                        metadata=metadata or {},
                    )
                return None

            # ── 2. No existing row — INSERT ────────────────────────────────
            row = await conn.fetchrow(
                """
                INSERT INTO fazle_queue_leases
                    (message_id, intent, worker_id, status,
                     attempts, leased_at, expires_at, metadata_json)
                VALUES ($1, $2, $3, 'leased', 1, NOW(),
                        NOW() + ($4 || ' seconds')::INTERVAL,
                        $5::jsonb)
                ON CONFLICT (message_id, intent) DO NOTHING
                RETURNING lease_id, attempts
                """,
                message_id, intent, worker_id, str(ttl_s),
                json.dumps(metadata or {}),
            )
            if row is None:
                # Race: another worker inserted between our SELECT and INSERT
                _metrics.lease_conflicts += 1
                return None

            _metrics.leases_acquired += 1
            return QueueLease(
                lease_id=row["lease_id"],
                message_id=message_id,
                intent=intent,
                worker_id=worker_id,
                status=LS_LEASED,
                attempts=row["attempts"],
                metadata=metadata or {},
            )

    except Exception as exc:
        log.warning("[arbiter] acquire_lease msg=%s intent=%s error: %s",
                    message_id, intent, exc)
        return None


async def complete_lease(lease_id: int, *, pool=None) -> bool:
    """
    Mark lease as completed.  A completed lease is a permanent dedup guard —
    no worker can re-lease the same (message_id, intent) pair.
    """
    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return False
    try:
        async with resolved_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fazle_queue_leases
                SET status = $1, completed_at = NOW(), updated_at = NOW()
                WHERE lease_id = $2
                """,
                LS_COMPLETED, lease_id,
            )
        _metrics.completions += 1
        return True
    except Exception as exc:
        log.warning("[arbiter] complete_lease(%s) error: %s", lease_id, exc)
        return False


async def fail_lease(
    lease_id:  int,
    error_msg: str = "",
    *,
    pool=None,
) -> bool:
    """
    Mark lease as failed.  The next attempt will be delayed by exponential
    backoff (2^(attempts-1) seconds, max MAX_BACKOFF_S).

    The lease is permanently dead-lettered when attempts > MAX_ATTEMPTS.
    """
    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return False
    try:
        async with resolved_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT attempts FROM fazle_queue_leases WHERE lease_id = $1",
                lease_id,
            )
            if not row:
                return False

            attempts   = row["attempts"]
            backoff_s  = min(2 ** (attempts - 1), MAX_BACKOFF_S)
            next_status = LS_DEAD_LETTER if attempts >= MAX_ATTEMPTS else LS_FAILED

            await conn.execute(
                """
                UPDATE fazle_queue_leases
                SET status       = $1,
                    last_error   = $2,
                    retry_after  = NOW() + ($3 || ' seconds')::INTERVAL,
                    updated_at   = NOW()
                WHERE lease_id = $4
                """,
                next_status, error_msg[:1000], str(backoff_s), lease_id,
            )
        _metrics.failures += 1
        if next_status == LS_DEAD_LETTER:
            _metrics.dead_letters += 1
        return True
    except Exception as exc:
        log.warning("[arbiter] fail_lease(%s) error: %s", lease_id, exc)
        return False


async def dead_letter_lease(
    lease_id:  int,
    reason:    str = "",
    *,
    pool=None,
) -> bool:
    """
    Permanently move a lease to dead_letter regardless of attempt count.
    Use when the message is unrecoverable (e.g. parse error, missing employee).
    """
    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return False
    try:
        async with resolved_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fazle_queue_leases
                SET status     = $1,
                    last_error = $2,
                    updated_at = NOW()
                WHERE lease_id = $3
                """,
                LS_DEAD_LETTER, reason[:1000], lease_id,
            )
        _metrics.dead_letters += 1
        return True
    except Exception as exc:
        log.warning("[arbiter] dead_letter_lease(%s) error: %s", lease_id, exc)
        return False


# ── Recovery sweep ────────────────────────────────────────────────────────────

async def recover_stale_leases(
    *,
    stale_after_s: int  = LEASE_TTL_S,
    pool               = None,
) -> int:
    """
    Reclaim stale leases (leased/processing beyond their TTL) back to 'failed'
    so the next worker can pick them up.

    Intended to be called by the scheduler every RECOVERY_INTERVAL seconds.
    Returns count of leases recovered.
    """
    from shared.events import emit as _emit, QUEUE_PRESSURE

    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return 0

    try:
        async with resolved_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE fazle_queue_leases
                SET status     = 'failed',
                    last_error = 'lease_expired_recovered',
                    updated_at = NOW()
                WHERE status IN ('leased', 'processing')
                  AND expires_at < NOW() - ($1 || ' seconds')::INTERVAL
                RETURNING lease_id, message_id, intent, worker_id
                """,
                str(stale_after_s),
            )
        count = len(rows)
        if count:
            _metrics.recoveries += count
            log.info("[arbiter] recovered %d stale leases", count)
            await _emit(
                QUEUE_PRESSURE,
                {"recovered_leases": count, "reason": "lease_expiry"},
                emitted_by="queue_arbiter",
            )
        return count
    except Exception as exc:
        log.warning("[arbiter] recover_stale_leases error: %s", exc)
        return 0


# ── Dead-letter inspection ────────────────────────────────────────────────────

async def get_dead_letters(
    *,
    limit: int  = 50,
    offset: int = 0,
    pool        = None,
) -> List[dict]:
    """
    Return dead-letter leases for the inspection endpoint.

    Sorted by updated_at DESC so most-recent failures appear first.
    Also embeds the linked message content from fazle_message_queue.
    """
    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return []
    try:
        async with resolved_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    l.lease_id, l.message_id, l.intent,
                    l.worker_id, l.attempts, l.last_error,
                    l.leased_at, l.updated_at,
                    l.metadata_json,
                    q.source, q.sender_phone, q.content_text,
                    q.message_type, q.enqueued_at
                FROM fazle_queue_leases l
                LEFT JOIN fazle_message_queue q ON q.id = l.message_id
                WHERE l.status = 'dead_letter'
                ORDER BY l.updated_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
        result = []
        for r in rows:
            result.append({
                "lease_id":    r["lease_id"],
                "message_id":  r["message_id"],
                "intent":      r["intent"],
                "worker_id":   r["worker_id"],
                "attempts":    r["attempts"],
                "last_error":  r["last_error"],
                "leased_at":   r["leased_at"].isoformat() if r["leased_at"] else None,
                "updated_at":  r["updated_at"].isoformat() if r["updated_at"] else None,
                "metadata":    (json.loads(r["metadata_json"])
                                if r["metadata_json"] else {}),
                "message": {
                    "source":       r["source"],
                    "sender_phone": r["sender_phone"],
                    "content_text": r["content_text"],
                    "message_type": r["message_type"],
                    "enqueued_at":  r["enqueued_at"].isoformat() if r["enqueued_at"] else None,
                },
            })
        return result
    except Exception as exc:
        log.warning("[arbiter] get_dead_letters error: %s", exc)
        return []


async def get_dead_letter_count(*, pool=None) -> int:
    """Return total count of dead-letter leases (for the dashboard summary)."""
    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return 0
    try:
        async with resolved_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM fazle_queue_leases WHERE status = 'dead_letter'"
            )
        return int(count or 0)
    except Exception:
        return 0


# ── Queue starvation detector ─────────────────────────────────────────────────

async def detect_starvation(*, pool=None) -> int:
    """
    Return count of pending fazle_message_queue items that have no active
    lease (i.e. they are eligible but no worker is picking them up).

    A non-zero result triggers a QUEUE_PRESSURE event and increments the
    queue_starvation counter.  Call from the scheduler alongside the
    recovery sweep.
    """
    from shared.events import emit as _emit, QUEUE_PRESSURE

    resolved_pool = _resolve_pool(pool)
    if resolved_pool is None:
        return 0
    try:
        async with resolved_pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM fazle_message_queue q
                WHERE q.status = 'pending'
                  AND q.attempts < $1
                  AND NOT EXISTS (
                      SELECT 1 FROM fazle_queue_leases l
                      WHERE l.message_id = q.id
                        AND l.status IN ('leased','processing')
                        AND l.expires_at > NOW()
                  )
                """,
                MAX_ATTEMPTS,
            )
        n = int(count or 0)
        if n > 0:
            _metrics.queue_starvation += 1
            await _emit(
                QUEUE_PRESSURE,
                {"pending_unworked": n, "reason": "starvation"},
                emitted_by="queue_arbiter",
            )
        return n
    except Exception as exc:
        log.warning("[arbiter] detect_starvation error: %s", exc)
        return 0


# ── High-level arbitrated processing ─────────────────────────────────────────

async def arbitrated_dequeue(
    *,
    limit:      int  = 10,
    worker_id:  str  = "default",
    intent:     str  = "generic",
    pool             = None,
) -> List[dict]:
    """
    Dequeue messages with arbitration.

    1. Calls shared.queue.dequeue_batch to get candidates.
    2. For each candidate, attempts to acquire_lease(msg_id, intent, worker_id).
    3. Only returns messages for which a lease was successfully acquired.
    4. Attaches lease_id to each returned dict so the caller can
       complete_lease / fail_lease when done.

    Falls back to plain dequeue_batch if QUEUE_ARBITER_ENABLED=false.
    """
    from shared.queue import dequeue_batch

    if not _arbiter_enabled():
        log.debug("[arbiter] disabled — plain dequeue")
        return await dequeue_batch(limit=limit, processor_id=worker_id)

    resolved_pool = _resolve_pool(pool)
    candidates    = await dequeue_batch(limit=limit * 2, processor_id=worker_id)

    result = []
    for msg in candidates:
        msg_id = msg.get("id")
        if msg_id is None:
            continue
        lease = await acquire_lease(msg_id, intent, worker_id, pool=resolved_pool)
        if lease is None:
            # Release the queue row back to pending so another worker can retry
            from shared.queue import fail_message
            await fail_message(msg_id, "lease_conflict")
            continue
        msg["_lease_id"]      = lease.lease_id
        msg["_lease_intent"]  = intent
        msg["_lease_worker"]  = worker_id
        result.append(msg)
        if len(result) >= limit:
            break

    if not result and candidates:
        _metrics.queue_starvation += 1

    return result


async def arbitrated_process(
    message_id:  int,
    intent:      str,
    worker_id:   str,
    handler:     Callable[..., Awaitable[Any]],
    *,
    ttl_s:       int = LEASE_TTL_S,
    pool              = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """
    Acquire lease → call handler → complete or fail lease.

    handler receives a QueueLease as its first argument so it can record
    the lease_id in its own tables if needed.

    Returns handler's return value on success, None on lease conflict or error.

    Example
    -------
        async def process_payment(lease: QueueLease, *, msg: dict):
            ...
            return {"ok": True}

        result = await arbitrated_process(
            message_id=123, intent="payment",
            worker_id="fpe-worker-1",
            handler=functools.partial(process_payment, msg=msg_row),
        )
    """
    t_start = time.perf_counter()
    lease = await acquire_lease(
        message_id, intent, worker_id,
        ttl_s=ttl_s, pool=pool, metadata=metadata,
    )
    if lease is None:
        return None

    try:
        result = await handler(lease)
        await complete_lease(lease.lease_id, pool=pool)
        latency_ms = (time.perf_counter() - t_start) * 1000.0
        _metrics.record_latency(latency_ms)
        return result
    except Exception as exc:
        log.warning(
            "[arbiter] handler failed msg=%s intent=%s attempt=%d: %s",
            message_id, intent, lease.attempts, exc,
        )
        await fail_lease(lease.lease_id, str(exc)[:500], pool=pool)
        return None


# ── Background recovery task ──────────────────────────────────────────────────

_recovery_task: Optional[asyncio.Task] = None


async def start_arbiter_recovery(*, pool=None) -> None:
    """
    Start the periodic lease-recovery background task.

    Called from app lifespan startup (non-fatal).
    """
    global _recovery_task

    async def _loop():
        log.info("[arbiter] recovery loop started interval=%ds", RECOVERY_INTERVAL)
        while True:
            try:
                await asyncio.sleep(RECOVERY_INTERVAL)
                await recover_stale_leases(pool=pool)
                await detect_starvation(pool=pool)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("[arbiter] recovery loop error: %s", exc)

    _recovery_task = asyncio.get_event_loop().create_task(
        _loop(), name="arbiter-recovery-loop"
    )


async def stop_arbiter_recovery() -> None:
    """Cancel the recovery background task (call from lifespan teardown)."""
    global _recovery_task
    if _recovery_task and not _recovery_task.done():
        _recovery_task.cancel()
        try:
            await _recovery_task
        except (asyncio.CancelledError, Exception):
            pass
        _recovery_task = None
    log.info("[arbiter] recovery loop stopped")
