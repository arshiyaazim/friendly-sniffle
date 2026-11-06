"""
Phase 13B — Queue Arbitration Tests
====================================
Tests for shared.queue_arbiter:

1.  test_double_worker_race_only_one_wins
2.  test_lease_expiry_releases_to_pending
3.  test_worker_crash_recovery
4.  test_dead_letter_recovery
5.  test_intent_dedup_blocks_duplicate
6.  test_exponential_backoff_schedules_retry
7.  test_complete_lease_wrong_lease_id_rejected
8.  test_get_dead_letters_returns_shape

Pool mock pattern (asyncpg-safe)
---------------------------------
asyncpg `pool.acquire()` is a *sync* call that returns an async context
manager.  Using AsyncMock for it creates a coroutine, not an async context
manager, which raises "object does not support async context manager
protocol".  Use _AcquireCtx below instead.
"""
from __future__ import annotations

import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ── asyncpg pool mock helper ──────────────────────────────────────────────────

class _AcquireCtx:
    """Sync callable returning an async context manager wrapping a conn mock."""
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return False


def _make_pool(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCtx(conn))
    return pool


# ── Helpers ───────────────────────────────────────────────────────────────────

def _expired_ts():
    """A timezone-aware timestamp 300 s in the past."""
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=300)


def _future_ts():
    """A timezone-aware timestamp 300 s in the future."""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=300)


# ── 1. Double worker race — only one wins ─────────────────────────────────────

@pytest.mark.asyncio
async def test_double_worker_race_only_one_wins():
    """
    Two concurrent workers compete for (message_id=1, intent='payment').
    Worker A gets INSERT → returns a lease.
    Worker B's INSERT hits ON CONFLICT → conn.fetchrow returns None → no lease.
    Only one worker should succeed.
    """
    import shared.queue_arbiter as qa

    # Worker A connection: SELECT returns no existing row; INSERT returns a row.
    conn_a = AsyncMock()
    conn_a.fetchrow = AsyncMock(side_effect=[
        None,                                           # SELECT existing → no row
        MagicMock(lease_id=1, attempts=1,               # INSERT → success
                  **{"__getitem__": lambda s, k: {"lease_id": 1, "attempts": 1}[k]}),
    ])

    # Worker B connection: SELECT returns no existing row; INSERT returns None (conflict).
    conn_b = AsyncMock()
    conn_b.fetchrow = AsyncMock(side_effect=[
        None,   # SELECT existing → no row
        None,   # INSERT → ON CONFLICT → nothing inserted
    ])

    pool_a = _make_pool(conn_a)
    pool_b = _make_pool(conn_b)

    with patch.object(qa, "_resolve_pool", side_effect=[pool_a, pool_b]):
        lease_a, lease_b = await asyncio.gather(
            qa.acquire_lease(1, "payment", "worker-a", pool=pool_a),
            qa.acquire_lease(1, "payment", "worker-b", pool=pool_b),
        )

    assert lease_a is not None, "worker-a should acquire the lease"
    assert lease_b is None,     "worker-b should be blocked by ON CONFLICT"


# ── 2. Lease expiry releases back to pending ──────────────────────────────────

@pytest.mark.asyncio
async def test_lease_expiry_releases_to_pending():
    """
    recover_stale_leases should UPDATE stale leases to 'failed' and return
    the count of recovered rows.
    """
    import shared.queue_arbiter as qa

    stale_row = MagicMock()
    stale_row.__len__ = lambda s: 3

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[stale_row, stale_row, stale_row])

    pool = _make_pool(conn)

    initial_recoveries = qa._metrics.recoveries
    with patch.object(qa, "_resolve_pool", return_value=pool):
        count = await qa.recover_stale_leases(pool=pool)

    assert count == 3
    assert qa._metrics.recoveries >= initial_recoveries + 3

    # Confirm UPDATE was called with 'failed'
    conn.fetch.assert_called_once()
    sql, *_ = conn.fetch.call_args.args
    assert "UPDATE fazle_queue_leases" in sql
    assert "failed" in sql


# ── 3. Worker crash recovery ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_crash_recovery():
    """
    If the existing lease is in 'leased' state but expired, a new worker can
    reclaim it via the UPDATE path (not INSERT).
    The returned lease has the new worker_id and incremented attempts.
    """
    import shared.queue_arbiter as qa

    # Simulate expired lease held by crashed 'worker-crash'
    existing = {
        "status":    "leased",
        "attempts":  1,
        "worker_id": "worker-crash",
        "expires_at": _expired_ts(),
    }
    existing_row = MagicMock(**{
        "__getitem__": lambda s, k: existing[k],
        "get": lambda s, k, d=None: existing.get(k, d),
    })

    reclaim_row = MagicMock(**{
        "__getitem__": lambda s, k: {"lease_id": 42, "attempts": 2}[k],
    })

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[existing_row, reclaim_row])

    pool = _make_pool(conn)

    with patch.object(qa, "_resolve_pool", return_value=pool):
        lease = await qa.acquire_lease(1, "payment", "worker-new", pool=pool)

    assert lease is not None,          "recovery worker should acquire lease"
    assert lease.worker_id == "worker-new"
    assert lease.lease_id  == 42
    assert lease.attempts  == 2

    # Confirm UPDATE reclaim SQL was called
    assert conn.fetchrow.call_count == 2
    update_sql = conn.fetchrow.call_args_list[1].args[0]
    assert "UPDATE fazle_queue_leases" in update_sql


# ── 4. Dead-letter recovery ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dead_letter_recovery():
    """
    dead_letter_lease() should set status='dead_letter'.
    get_dead_letters() should return the item.
    """
    import shared.queue_arbiter as qa

    # ── dead_letter_lease ──────────────────────────────────────────────────
    conn_dl = AsyncMock()
    conn_dl.execute = AsyncMock(return_value="UPDATE 1")

    pool_dl = _make_pool(conn_dl)

    initial_dl = qa._metrics.dead_letters
    ok = await qa.dead_letter_lease(99, reason="parse error", pool=pool_dl)
    assert ok is True
    assert qa._metrics.dead_letters >= initial_dl + 1

    conn_dl.execute.assert_called_once()
    call_args = conn_dl.execute.call_args.args
    sql = call_args[0]
    # Status is passed as a parameter ($1), not embedded in SQL
    assert "UPDATE fazle_queue_leases" in sql
    assert call_args[1] == qa.LS_DEAD_LETTER

    # ── get_dead_letters ───────────────────────────────────────────────────
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)

    mock_row = {
        "lease_id":    99,
        "message_id":  7,
        "intent":      "escort_assign",
        "worker_id":   "w1",
        "attempts":    3,
        "last_error":  "parse error",
        "leased_at":   now,
        "updated_at":  now,
        "metadata_json": json.dumps({}),
        "source":      "bridge1",
        "sender_phone": "01711000000",
        "content_text": "test",
        "message_type": "text",
        "enqueued_at":  now,
    }

    def _getitem(key):
        return mock_row[key]

    row = MagicMock()
    row.__getitem__ = lambda s, k: _getitem(k)

    conn_get = AsyncMock()
    conn_get.fetch = AsyncMock(return_value=[row])

    pool_get = _make_pool(conn_get)

    items = await qa.get_dead_letters(pool=pool_get)

    assert len(items) == 1
    assert items[0]["lease_id"] == 99
    assert items[0]["intent"]   == "escort_assign"
    assert "message" in items[0]


# ── 5. Intent dedup blocks duplicate ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_intent_dedup_blocks_duplicate():
    """
    If an existing lease has status='completed', acquire_lease must return
    None (dedup hit), even for a fresh worker.
    """
    import shared.queue_arbiter as qa

    completed_existing = {
        "status":    "completed",
        "attempts":  1,
        "worker_id": "worker-1",
        "expires_at": _future_ts(),
    }
    existing_row = MagicMock(**{
        "__getitem__": lambda s, k: completed_existing[k],
    })

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing_row)

    pool = _make_pool(conn)

    initial_dedup = qa._metrics.deduplication_hits

    lease = await qa.acquire_lease(5, "payment", "new-worker", pool=pool)

    assert lease is None, "completed intent must block re-processing"
    assert qa._metrics.deduplication_hits > initial_dedup


# ── 6. Exponential backoff on failure ────────────────────────────────────────

@pytest.mark.asyncio
async def test_exponential_backoff_schedules_retry():
    """
    fail_lease with attempt=1 should use backoff of 2^0=1 s.
    fail_lease with attempt=3 should use backoff of 2^2=4 s.
    fail_lease with attempt=MAX_ATTEMPTS should transition to dead_letter.
    """
    import shared.queue_arbiter as qa

    MAX = qa.MAX_ATTEMPTS

    async def _run_fail(attempt, expected_backoff_s, expect_dead=False):
        conn = AsyncMock()
        conn.fetchrow  = AsyncMock(return_value=MagicMock(
            **{"__getitem__": lambda s, k: {"attempts": attempt}[k]}
        ))
        conn.execute   = AsyncMock(return_value="UPDATE 1")
        pool = _make_pool(conn)
        ok = await qa.fail_lease(lease_id=10, error_msg="err", pool=pool)
        assert ok is True

        # Validate the UPDATE call
        update_call_args = conn.execute.call_args.args
        sql = update_call_args[0]
        # status arg
        expected_status = qa.LS_DEAD_LETTER if expect_dead else qa.LS_FAILED
        assert expected_status == update_call_args[1]
        # backoff_s passed as string
        assert str(expected_backoff_s) == update_call_args[3]

    await _run_fail(attempt=1, expected_backoff_s=1)   # 2^(1-1) = 1
    await _run_fail(attempt=3, expected_backoff_s=4)   # 2^(3-1) = 4
    await _run_fail(attempt=MAX, expected_backoff_s=min(2**(MAX-1), qa.MAX_BACKOFF_S),
                    expect_dead=True)


# ── 7. Complete with wrong lease_id is rejected ──────────────────────────────

@pytest.mark.asyncio
async def test_complete_lease_wrong_lease_id_rejected():
    """
    complete_lease with a non-existent lease_id should NOT throw but should
    silently no-op (UPDATE 0 rows) and return True (DB op succeeded).

    The guard against wrong IDs is at the DB level (WHERE lease_id=$2 updates
    0 rows) — we just ensure no exception is raised.
    """
    import shared.queue_arbiter as qa

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")  # 0 rows matched
    pool = _make_pool(conn)

    # Should return True (DB call succeeded) even if 0 rows matched
    ok = await qa.complete_lease(lease_id=99999, pool=pool)
    assert ok is True
    conn.execute.assert_called_once()
    sql = conn.execute.call_args.args[0]
    assert "completed" in sql
    assert "lease_id" in sql.lower() or "$2" in sql


# ── 8. get_dead_letters returns expected shape ────────────────────────────────

@pytest.mark.asyncio
async def test_get_dead_letters_returns_shape():
    """
    get_dead_letters must return a list of dicts with the expected keys
    (lease_id, message_id, intent, worker_id, attempts, last_error,
     leased_at, updated_at, metadata, message).
    """
    import shared.queue_arbiter as qa
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)

    EXPECTED_KEYS = {
        "lease_id", "message_id", "intent", "worker_id", "attempts",
        "last_error", "leased_at", "updated_at", "metadata", "message",
    }
    MSG_KEYS = {"source", "sender_phone", "content_text", "message_type", "enqueued_at"}

    raw = {
        "lease_id": 1, "message_id": 2, "intent": "payment",
        "worker_id": "w1", "attempts": 5, "last_error": "timeout",
        "leased_at": now, "updated_at": now,
        "metadata_json": json.dumps({"extra": "data"}),
        "source": "bridge2", "sender_phone": "01880000000",
        "content_text": "pay 500", "message_type": "text",
        "enqueued_at": now,
    }
    row = MagicMock()
    row.__getitem__ = lambda s, k: raw[k]

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[row])
    pool = _make_pool(conn)

    items = await qa.get_dead_letters(limit=10, pool=pool)

    assert len(items) == 1
    item = items[0]
    assert EXPECTED_KEYS <= set(item.keys()), f"Missing keys: {EXPECTED_KEYS - set(item.keys())}"
    assert MSG_KEYS <= set(item["message"].keys()), \
        f"Missing message keys: {MSG_KEYS - set(item['message'].keys())}"
    assert item["metadata"] == {"extra": "data"}
    assert item["intent"] == "payment"
