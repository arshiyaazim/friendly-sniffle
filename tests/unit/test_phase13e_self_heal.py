"""
Phase 13E — Self-Healing Runtime tests
=======================================

Tests:
  1. test_simulated_bridge_outage    — bridge in outage → healer detects pressure,
                                        creates audit entry, probes bridges
  2. test_simulated_db_delay         — pending_count raises TimeoutError → check
                                        returns 0.0 (fail-safe, no exception)
  3. test_simulated_queue_storm      — pending_count=250 → pressure=1.0,
                                        QUEUE_PRESSURE event emitted, audit entry
  4. test_websocket_crash_recovery   — heartbeat task dead → healer restarts it,
                                        ws_broadcaster recovery count incremented
  5. test_stale_lock_cleanup         — cleanup_expired_locks returns 15 → pressure>0,
                                        audit entry created

All tests use in-process state only — no live DB or bridge required.
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

def _fresh_healer():
    """Create a SelfHealer with clean in-process state (no live deps needed)."""
    from shared.self_heal import SelfHealer
    return SelfHealer()


# ── Test 1: Simulated bridge outage ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_simulated_bridge_outage():
    """
    When bridge_orchestrator reports a bridge in outage state, _check_bridge_health()
    must return pressure > 0.0 and create a 'bridge_outage' audit entry.
    """
    healer = _fresh_healer()

    mock_diag = {
        "bridges": {
            "bridge1": {"state": "healthy", "avg_lag_ms": 120.0},
            "bridge2": {"state": "outage",  "avg_lag_ms": 0.0},
        },
        "orchestrator": {},
    }

    mock_orch = MagicMock()
    mock_orch.get_diagnostics.return_value = mock_diag

    import shared.bridge_orchestrator as _bo_mod

    with patch.object(_bo_mod, "get_orchestrator", return_value=mock_orch), \
         patch.object(_bo_mod, "probe_all_bridges", new=AsyncMock()):
        pressure = await healer._check_bridge_health()

    assert pressure > 0.0, f"Expected pressure > 0 for outage bridge, got {pressure}"
    assert any(
        a["action"] == "bridge_outage" for a in healer._audit_log
    ), "Expected 'bridge_outage' audit entry"


# ── Test 2: Simulated DB delay ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simulated_db_delay():
    """
    When pending_count raises asyncio.TimeoutError (simulating a slow DB),
    _check_queue_stall() must return 0.0 and NOT raise.
    Fail-safe: no pressure on DB errors.
    """
    healer = _fresh_healer()

    import modules.outbound as _out_mod

    with patch.object(
        _out_mod, "pending_count", new=AsyncMock(side_effect=asyncio.TimeoutError("db slow"))
    ), patch("shared.events.emit", new=AsyncMock()):
        pressure = await healer._check_queue_stall()

    assert pressure == 0.0, "DB timeout must result in 0.0 pressure (fail-safe)"
    # No audit entry expected — the fail-safe path is silent
    assert not any(
        a["action"] in ("queue_stall", "queue_storm") for a in healer._audit_log
    )


# ── Test 3: Simulated queue storm ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simulated_queue_storm():
    """
    When outbound pending_count returns 250 (> QUEUE_STORM_THRESHOLD=200),
    _check_queue_stall() must return pressure=1.0 and emit QUEUE_PRESSURE event.
    """
    healer = _fresh_healer()

    import modules.outbound as _out_mod
    from shared.events import QUEUE_PRESSURE

    with patch.object(
        _out_mod, "pending_count", new=AsyncMock(return_value=250)
    ), patch("shared.events.emit", new=AsyncMock()) as mock_emit:
        pressure = await healer._check_queue_stall()

    assert pressure == 1.0, f"Expected pressure=1.0 for storm, got {pressure}"
    assert any(
        a["action"] == "queue_storm" for a in healer._audit_log
    ), "Expected 'queue_storm' audit entry"

    mock_emit.assert_called_once()
    call_event = mock_emit.call_args[0][0]
    assert call_event == QUEUE_PRESSURE, f"Expected QUEUE_PRESSURE event, got {call_event!r}"
    call_payload = mock_emit.call_args[0][1]
    assert call_payload.get("level") == "storm"
    assert call_payload.get("pending") == 250


# ── Test 4: WebSocket crash recovery ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_websocket_crash_recovery():
    """
    When the realtime heartbeat task is done (crashed), _check_websocket() must:
    - Return pressure=1.0
    - Create an audit entry with action='ws_failure'
    - Increment ws_broadcaster recovery count to 1
    - Create a new heartbeat task
    """
    healer = _fresh_healer()

    import shared.realtime as _rt_mod

    # Simulate a dead heartbeat task
    dead_task = MagicMock()
    dead_task.done.return_value = True

    # Minimal async heartbeat stub so create_task doesn't spin forever
    async def _fake_heartbeat_loop():
        pass  # returns immediately in tests

    with patch.object(_rt_mod, "_heartbeat_task", dead_task, create=True), \
         patch.object(_rt_mod, "_heartbeat_loop", _fake_heartbeat_loop), \
         patch("shared.events.emit", new=AsyncMock()):
        pressure = await healer._check_websocket()

    assert pressure == 1.0, "Dead heartbeat task must produce pressure=1.0"
    assert any(
        a["action"] == "ws_failure" for a in healer._audit_log
    ), "Expected 'ws_failure' audit entry"
    assert healer._recovery_counts.get("ws_broadcaster", 0) == 1, (
        "Expected ws_broadcaster recovery count == 1"
    )


# ── Test 5: Stale lock cleanup ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_lock_cleanup():
    """
    When cleanup_expired_locks returns 15 (≥ LOCK_STALE_WARN=10),
    _check_stale_locks() must return pressure > 0.0 and create an audit entry.
    """
    healer = _fresh_healer()

    import shared.locks as _locks_mod

    with patch.object(
        _locks_mod, "cleanup_expired_locks", new=AsyncMock(return_value=15)
    ):
        pressure = await healer._check_stale_locks()

    assert pressure > 0.0, f"Expected pressure > 0 for 15 stale locks, got {pressure}"
    assert any(
        a["action"] == "stale_locks" for a in healer._audit_log
    ), "Expected 'stale_locks' audit entry"


# ── Bonus: pressure computation ───────────────────────────────────────────────

def test_compute_pressure_clamped():
    """_compute_pressure must clamp output to [0.0, 1.0]."""
    from shared.self_heal import _compute_pressure

    # All signals at max → should clamp to 1.0 (weights sum to < 1.0 but still capped)
    signals = {k: 1.0 for k in ("bridge_outage", "dead_worker", "queue_stall",
                                  "ws_failure", "retry_storm", "stale_locks")}
    score = _compute_pressure(signals)
    assert 0.0 <= score <= 1.0

    # All zero → 0.0
    assert _compute_pressure({k: 0.0 for k in signals}) == 0.0
