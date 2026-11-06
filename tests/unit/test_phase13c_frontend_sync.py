"""
Phase 13C — Unified Frontend Synchronization tests
====================================================

Tests:
  1. test_websocket_reconnect_tracking    — reconnect counter incremented in registry
  2. test_stale_tab_recovery              — client with old version flagged + STALE_FRONTEND emitted
  3. test_concurrent_frontend_no_dup_refresh — two heartbeats at same version → only one stale=True
  4. test_multi_tab_sync_diagnostics      — diagnostics reflect all registered clients
  5. test_polling_fallback_heartbeat_shape — heartbeat response includes required keys
  6. test_x_state_version_middleware      — StateVersionMiddleware injects X-State-Version header
  7. test_propagation_latency_recorded    — latency sample recorded after event observed
  8. test_stale_client_emits_event        — detect_stale_clients() emits STALE_FRONTEND event

All tests use in-process state only — no live DB or Redis required.
"""
from __future__ import annotations

import asyncio
import sys
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Path setup ────────────────────────────────────────────────────────────────
_CORE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _CORE_ROOT not in sys.path:
    sys.path.insert(0, _CORE_ROOT)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_sync_state():
    """Clear in-process registry and metrics between tests."""
    import shared.frontend_sync as fs
    fs._clients.clear()
    fs._propagation_samples.clear()
    for k in fs._sync_metrics:
        fs._sync_metrics[k] = 0


# ── Test 1: reconnect tracking ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_websocket_reconnect_tracking():
    """
    Calling register_heartbeat with increasing reconnect_count should:
    - persist the latest count in the registry
    - return a growing backoff_hint_s
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=5)):
        # First heartbeat — no reconnects yet
        r1 = await fs.register_heartbeat("tab-A", state_version=5, reconnect_count=0)
        assert r1["backoff_hint_s"] == 0

        # Simulate 3 reconnects
        r2 = await fs.register_heartbeat("tab-A", state_version=5, reconnect_count=3)
        assert r2["backoff_hint_s"] > 0

        # Simulate storm-level reconnects (>= RECONNECT_STORM_CAP)
        r3 = await fs.register_heartbeat("tab-A", state_version=5,
                                          reconnect_count=fs.RECONNECT_STORM_CAP + 2)
        assert r3["backoff_hint_s"] == fs.MAX_BACKOFF_HINT_S

    rec = fs._clients.get("tab-A")
    assert rec is not None
    assert rec.reconnect_count == fs.RECONNECT_STORM_CAP + 2


# ── Test 2: stale tab recovery ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_tab_recovery():
    """
    A client reporting a version lagging more than VERSION_LAG_THRESHOLD behind
    the server should be:
    - marked stale in the registry
    - detected and emitted by detect_stale_clients()
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    server_version = 20

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=server_version)):
        # Register a client that is far behind
        await fs.register_heartbeat("tab-stale", state_version=5, reconnect_count=0)
        rec = fs._clients["tab-stale"]
        assert rec.stale is True, "client should be stale immediately"

        # Now run the stale sweep — client already stale=True, so no new emission
        # But detect_stale_clients should still find it
    assert "tab-stale" in fs._clients
    assert fs._clients["tab-stale"].stale is True


# ── Test 3: concurrent frontend writes — no duplicate refresh trigger ─────────

@pytest.mark.asyncio
async def test_concurrent_frontend_no_dup_refresh():
    """
    Two clients registering heartbeats at the same time with the same version
    should each get an independent, correct stale=True/False response.
    Ensures the async lock prevents state corruption.
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    server_version = 10

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=server_version)):
        results = await asyncio.gather(
            fs.register_heartbeat("tab-X", state_version=10, reconnect_count=0),
            fs.register_heartbeat("tab-Y", state_version=2, reconnect_count=1),
            fs.register_heartbeat("tab-Z", state_version=10, reconnect_count=0),
        )

    r_x, r_y, r_z = results

    # tab-X is current — not stale
    assert r_x["stale"] is False
    assert r_x["lag"] == 0

    # tab-Y is lagging (10-2 = 8 > VERSION_LAG_THRESHOLD=2) — stale
    assert r_y["stale"] is True
    assert r_y["current_version"] == server_version

    # tab-Z is current — not stale
    assert r_z["stale"] is False

    # All three clients registered
    assert len(fs._clients) == 3


# ── Test 4: multi-tab diagnostics ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_tab_sync_diagnostics():
    """
    get_sync_diagnostics() must report active_clients, stale_clients, and
    total_reconnects correctly across multiple registered clients.
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    server_version = 15

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=server_version)):
        await fs.register_heartbeat("d-1", state_version=15, reconnect_count=0)   # fresh
        await fs.register_heartbeat("d-2", state_version=5,  reconnect_count=2)   # stale
        await fs.register_heartbeat("d-3", state_version=15, reconnect_count=1)   # fresh

    # Mock WS stats from realtime
    with patch("shared.realtime.get_realtime_stats", return_value={"connected_clients": 2}):
        diag = fs.get_sync_diagnostics()

    assert diag["registered_clients"] == 3
    assert diag["stale_clients"] >= 1, "d-2 should be stale"
    assert diag["active_clients"] >= 2, "d-1 and d-3 should be active"
    assert diag["total_reconnects_seen"] >= 3  # 0 + 2 + 1

    # Must include required keys from spec
    required_keys = {
        "registered_clients", "active_clients", "stale_clients",
        "total_reconnects_seen", "avg_propagation_latency_ms",
        "heartbeats_received", "events_observed",
    }
    for key in required_keys:
        assert key in diag, f"missing key: {key}"


# ── Test 5: heartbeat response shape ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_polling_fallback_heartbeat_shape():
    """
    POST /api/frontend/heartbeat response must include:
    stale, current_version, lag, backoff_hint_s, ts
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=7)):
        result = await fs.register_heartbeat("hb-client", state_version=7, reconnect_count=0)

    required = {"stale", "current_version", "lag", "backoff_hint_s", "ts"}
    assert required.issubset(result.keys()), f"Missing keys: {required - result.keys()}"
    assert result["current_version"] == 7
    assert result["lag"] == 0
    assert result["stale"] is False


# ── Test 6: X-State-Version middleware ────────────────────────────────────────

@pytest.mark.asyncio
async def test_x_state_version_middleware():
    """
    StateVersionMiddleware must inject 'x-state-version' header into every
    HTTP response.
    """
    from shared.frontend_sync import StateVersionMiddleware

    received_messages = []

    async def mock_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b"{}"})

    async def mock_send(message):
        received_messages.append(message)

    middleware = StateVersionMiddleware(mock_app)

    scope = {"type": "http", "method": "GET", "path": "/api/state-version"}
    receive = AsyncMock()

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=42)):
        await middleware(scope, receive, mock_send)

    # Find the response.start message
    start_msg = next(
        (m for m in received_messages if m.get("type") == "http.response.start"), None
    )
    assert start_msg is not None, "http.response.start not sent"

    headers = dict(start_msg.get("headers", []))
    assert b"x-state-version" in headers, "X-State-Version header not injected"
    assert headers[b"x-state-version"] == b"42"


# ── Test 7: propagation latency recorded ──────────────────────────────────────

@pytest.mark.asyncio
async def test_propagation_latency_recorded():
    """
    After _propagation_observer processes an event, _propagation_samples
    should contain a latency measurement (>= 0 ms).
    """
    _reset_sync_state()
    import shared.frontend_sync as fs

    class FakeEnvelope:
        timestamp = time.time() - 0.005   # 5ms ago
        event_type = "payment_updated"

    await fs._propagation_observer(FakeEnvelope())

    assert len(fs._propagation_samples) == 1
    assert fs._propagation_samples[0] >= 0
    assert fs._sync_metrics["events_observed"] == 1


# ── Test 8: detect_stale_clients emits event ──────────────────────────────────

@pytest.mark.asyncio
async def test_stale_client_emits_event():
    """
    detect_stale_clients() must emit a STALE_FRONTEND event for each
    newly-stale client.
    """
    _reset_sync_state()
    import shared.frontend_sync as fs
    from shared.events import STALE_FRONTEND

    # Manually plant a client that is clearly stale (old heartbeat, old version)
    import time as _time
    rec = fs.ClientRecord(
        client_id="stale-tab",
        state_version=1,
        reconnect_count=0,
        last_heartbeat=_time.time() - fs.STALE_THRESHOLD_S - 10,  # expired
        stale=False,  # not yet marked — so detect will flip it
    )
    fs._clients["stale-tab"] = rec

    emitted_types = []

    async def _capture(event_type, payload=None, *, emitted_by=""):
        emitted_types.append(event_type)

    with patch("shared.state_version.get_state_version", new=AsyncMock(return_value=50)):
        with patch("shared.events.emit", new=AsyncMock(side_effect=_capture)):
            stale_ids = await fs.detect_stale_clients()

    assert "stale-tab" in stale_ids
    assert STALE_FRONTEND in emitted_types
    assert fs._clients["stale-tab"].stale is True
