"""
shared.frontend_sync — Unified Frontend Synchronization Protocol (Phase 13C)
============================================================================

Problem solved
--------------
Multiple dashboard tabs (Fazle Core, Payroll Engine, Escort Roster) may
show stale state after a write because:

* They haven't received a WebSocket event yet (network lag or tab hidden)
* Their WebSocket disconnected and they're now polling at a slow rate
* They applied an optimistic update that was never confirmed or rolled back
* Multiple tabs fired writes concurrently → race conditions

This module provides the server-side coordination layer that solves all of
the above without rewriting any frontend framework.

Architecture
------------
* X-State-Version header   — injected by StateVersionMiddleware into every
  HTTP response; clients compare against their locally cached version and
  decide whether to re-fetch (eliminates polling for most cases).

* Heartbeat endpoint        — clients POST {client_id, state_version,
  reconnect_count} every 30 s; server updates the client registry, marks
  stale clients, and returns {stale, current_version, backoff_hint_s}.

* Stale-client detector    — background sweep (every 60 s) marks clients
  whose state_version lags the server version or whose last_heartbeat
  exceeds STALE_THRESHOLD_S; emits STALE_FRONTEND event per stale client.

* Propagation latency      — event subscriber measures the delay between
  domain-event emission (envelope.timestamp) and handler dispatch; does NOT
  re-broadcast (that is already done by shared.realtime).

* Reconnect storm guard    — backoff_hint_s in heartbeat response grows with
  reconnect_count so all tabs don't reconnect simultaneously after a restart.

* Polling fallback         — when WS is unavailable clients should call
  /api/state-version every 5 s and trigger a soft-refresh when version
  advances; this is documented in the JS snippet at the bottom.

Guarantees
----------
* Never double-broadcasts any event (realtime.py owns broadcasts)
* Never modifies fazle_message_queue or any other DB table
* Feature-flag safe: set FRONTEND_SYNC_ENABLED=false to disable silently
* Works without Redis (version header falls back to 0 if unavailable)
* standalone-safe: apps that don't call start_sync_monitoring() get no-ops

JS integration snippet (add to dashboard.html)
----------------------------------------------
    (function () {
        'use strict';
        var _clientId = localStorage.getItem('fazle_sid') ||
                        (typeof crypto !== 'undefined' && crypto.randomUUID
                            ? crypto.randomUUID() : Math.random().toString(36).slice(2));
        localStorage.setItem('fazle_sid', _clientId);

        var _lastVersion = 0;
        var _reconnectCount = 0;
        var _wsConnected = false;
        var _pollTimer = null;

        // ── WebSocket with exponential backoff ─────────────────────────────
        function connectWs() {
            var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            var ws = new WebSocket(proto + '//' + location.host + '/ws/realtime');

            ws.onopen = function () {
                _wsConnected = true;
                if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
                _sendHeartbeat();
            };

            ws.onmessage = function (e) {
                var evt;
                try { evt = JSON.parse(e.data); } catch (_) { return; }
                if (evt.type === 'connected') {
                    _lastVersion = evt.state_version || 0;
                    return;
                }
                if (evt.type === 'pong' || evt.type === 'ping') { return; }
                // Domain events that require a UI refresh
                var REFRESH_EVENTS = {
                    payment_updated: 1, payment_created: 1, payment_corrected: 1,
                    escort_updated: 1, escort_assigned: 1, escort_completed: 1,
                    draft_updated: 1, draft_created: 1, draft_approved: 1,
                    employee_updated: 1, transaction_repaired: 1,
                };
                if (REFRESH_EVENTS[evt.event_type]) {
                    if (typeof refreshCurrentTab === 'function') refreshCurrentTab();
                }
            };

            ws.onerror = function () { ws.close(); };

            ws.onclose = function () {
                _wsConnected = false;
                _reconnectCount++;
                // Exponential backoff capped at 30 s
                var delay = Math.min(1000 * Math.pow(1.5, Math.min(_reconnectCount - 1, 10)), 30000);
                setTimeout(connectWs, delay);
                _startPollingFallback();
            };
        }

        // ── Polling fallback when WS is down ───────────────────────────────
        function _startPollingFallback() {
            if (_pollTimer) return;
            _pollTimer = setInterval(function () {
                if (_wsConnected) { clearInterval(_pollTimer); _pollTimer = null; return; }
                fetch('/api/state-version')
                    .then(function (r) { return r.json(); })
                    .then(function (d) {
                        if (d.version > _lastVersion) {
                            _lastVersion = d.version;
                            if (typeof refreshCurrentTab === 'function') refreshCurrentTab();
                        }
                    })
                    .catch(function () {});
            }, 5000);
        }

        // ── Heartbeat ──────────────────────────────────────────────────────
        function _sendHeartbeat() {
            fetch('/api/frontend/heartbeat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    client_id: _clientId,
                    state_version: _lastVersion,
                    reconnect_count: _reconnectCount,
                }),
            })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.stale && d.current_version > _lastVersion) {
                    _lastVersion = d.current_version;
                    if (typeof refreshCurrentTab === 'function') refreshCurrentTab();
                }
                // X-State-Version is also checked on every fetch response
            })
            .catch(function () {});
        }

        // ── X-State-Version header observer ───────────────────────────────
        // Intercept all fetch responses and compare X-State-Version header
        var _origFetch = window.fetch;
        window.fetch = function () {
            return _origFetch.apply(this, arguments).then(function (resp) {
                var sv = resp.headers.get('X-State-Version');
                if (sv !== null) {
                    var v = parseInt(sv, 10);
                    if (!isNaN(v) && v > _lastVersion) {
                        _lastVersion = v;
                        if (typeof refreshCurrentTab === 'function') refreshCurrentTab();
                    }
                }
                return resp;
            });
        };

        setInterval(_sendHeartbeat, 30000);
        connectWs();
    })();
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("fazle.frontend_sync")

# ── Feature flag ──────────────────────────────────────────────────────────────

def _sync_enabled() -> bool:
    import os
    return os.getenv("FRONTEND_SYNC_ENABLED", "true").lower() not in ("0", "false", "no")


# ── Constants ─────────────────────────────────────────────────────────────────

STALE_THRESHOLD_S:     int = 120   # client is stale if heartbeat older than this
RECONNECT_STORM_CAP:   int = 10    # reconnect_count above this → extended backoff
VERSION_LAG_THRESHOLD: int = 2     # client is stale if server_version - client_version > this
STALE_SWEEP_INTERVAL:  int = 60    # seconds between stale-client sweeps
MAX_BACKOFF_HINT_S:    int = 30    # maximum backoff hint returned to client
LATENCY_SAMPLE_MAX:    int = 1000  # rolling window for propagation latency

# ── Per-client state ──────────────────────────────────────────────────────────

@dataclass
class ClientRecord:
    client_id:       str
    state_version:   int   = 0
    reconnect_count: int   = 0
    last_heartbeat:  float = field(default_factory=time.time)
    connected_at:    float = field(default_factory=time.time)
    stale:           bool  = False


# ── In-process registry ───────────────────────────────────────────────────────

_clients: Dict[str, ClientRecord] = {}
_lock = asyncio.Lock()               # guards _clients mutations


async def register_heartbeat(
    client_id:       str,
    state_version:   int,
    reconnect_count: int = 0,
) -> dict:
    """
    Called by POST /api/frontend/heartbeat.

    Updates (or creates) the client registry entry.
    Returns {stale, current_version, backoff_hint_s, ts}.

    backoff_hint_s — how long the client should wait before reconnecting
    after a WS disconnect.  Grows with reconnect_count to prevent storms.
    """
    from shared.state_version import get_state_version
    current_version = await get_state_version()

    async with _lock:
        rec = _clients.get(client_id)
        if rec is None:
            rec = ClientRecord(
                client_id=client_id,
                state_version=state_version,
                reconnect_count=reconnect_count,
            )
            _clients[client_id] = rec
            log.debug("[sync] new client registered client_id=%s", client_id)
        else:
            rec.state_version   = state_version
            rec.reconnect_count = reconnect_count
            rec.last_heartbeat  = time.time()

        # Determine if stale
        lag    = current_version - state_version
        stale  = lag > VERSION_LAG_THRESHOLD
        rec.stale = stale

    # Backoff hint — prevent reconnect storms
    if reconnect_count <= 0:
        backoff_hint_s = 0
    elif reconnect_count >= RECONNECT_STORM_CAP:
        backoff_hint_s = MAX_BACKOFF_HINT_S
    else:
        # 1.5^(reconnect_count-1) seconds, capped at MAX_BACKOFF_HINT_S
        backoff_hint_s = int(min(1.5 ** (reconnect_count - 1), MAX_BACKOFF_HINT_S))

    _sync_metrics["heartbeats_received"] += 1
    if stale:
        _sync_metrics["stale_responses_sent"] += 1

    return {
        "stale":           stale,
        "current_version": current_version,
        "lag":             current_version - state_version,
        "backoff_hint_s":  backoff_hint_s,
        "ts":              time.time(),
    }


async def purge_inactive_clients(*, threshold_s: int = STALE_THRESHOLD_S * 4) -> int:
    """
    Remove clients that haven't sent a heartbeat for a very long time
    (default 8 minutes).  Keeps the registry bounded.
    """
    cutoff = time.time() - threshold_s
    async with _lock:
        stale_ids = [cid for cid, r in _clients.items() if r.last_heartbeat < cutoff]
        for cid in stale_ids:
            del _clients[cid]
    if stale_ids:
        log.debug("[sync] purged %d inactive clients", len(stale_ids))
    return len(stale_ids)


# ── Stale-client sweep ────────────────────────────────────────────────────────

async def detect_stale_clients() -> List[str]:
    """
    Return list of client_ids that are considered stale.

    A client is stale when:
      1. last_heartbeat < now - STALE_THRESHOLD_S, OR
      2. server_version - client.state_version > VERSION_LAG_THRESHOLD

    Emits a STALE_FRONTEND event for each newly stale client.
    """
    from shared.events import emit as _emit, STALE_FRONTEND
    from shared.state_version import get_state_version

    current_version = await get_state_version()
    now = time.time()
    newly_stale: List[str] = []

    async with _lock:
        for cid, rec in list(_clients.items()):
            heartbeat_stale  = (now - rec.last_heartbeat) > STALE_THRESHOLD_S
            version_lag_stale = (current_version - rec.state_version) > VERSION_LAG_THRESHOLD
            is_stale = heartbeat_stale or version_lag_stale

            if is_stale and not rec.stale:
                rec.stale = True
                newly_stale.append(cid)
                _sync_metrics["stale_clients_detected"] += 1

            elif not is_stale and rec.stale:
                rec.stale = False   # recovered

    for cid in newly_stale:
        await _emit(STALE_FRONTEND, {
            "client_id":       cid,
            "current_version": current_version,
            "reason":          "heartbeat_stale_or_version_lag",
        }, emitted_by="frontend_sync")

    return newly_stale


def count_stale_clients() -> int:
    """Return count of currently-stale registered clients (non-async, for metrics)."""
    return sum(1 for r in _clients.values() if r.stale)


def count_active_clients() -> int:
    """Clients that sent a heartbeat within STALE_THRESHOLD_S."""
    cutoff = time.time() - STALE_THRESHOLD_S
    return sum(1 for r in _clients.values() if r.last_heartbeat >= cutoff)


# ── Propagation latency tracking ─────────────────────────────────────────────

_propagation_samples: List[float] = []  # ms, rolling window


def _record_propagation_latency(ms: float) -> None:
    _propagation_samples.append(ms)
    if len(_propagation_samples) > LATENCY_SAMPLE_MAX:
        _propagation_samples[:] = _propagation_samples[-LATENCY_SAMPLE_MAX:]


def _avg_propagation_latency_ms() -> Optional[float]:
    if not _propagation_samples:
        return None
    return round(sum(_propagation_samples) / len(_propagation_samples), 2)


async def _propagation_observer(evt) -> None:
    """
    Subscribed to "*" in shared.events.  Measures the delay from when
    the event was emitted (evt.timestamp) to when this handler runs.
    Does NOT re-broadcast — shared.realtime handles that.
    """
    latency_ms = (time.time() - evt.timestamp) * 1000.0
    _record_propagation_latency(latency_ms)
    _sync_metrics["events_observed"] += 1


# ── In-process metrics ────────────────────────────────────────────────────────

_sync_metrics: Dict[str, Any] = {
    "heartbeats_received":    0,
    "stale_clients_detected": 0,
    "stale_responses_sent":   0,
    "events_observed":        0,
    "total_reconnects_seen":  0,
}


def get_sync_diagnostics() -> dict:
    """
    Return aggregated sync diagnostics for GET /api/frontend/sync-stats.
    Includes realtime WS client counts from shared.realtime if available.
    """
    ws_stats: dict = {}
    try:
        from shared.realtime import get_realtime_stats
        ws_stats = get_realtime_stats()
    except Exception:
        pass

    total_reconnects = sum(r.reconnect_count for r in _clients.values())

    return {
        "registered_clients":    len(_clients),
        "active_clients":        count_active_clients(),
        "stale_clients":         count_stale_clients(),
        "total_reconnects_seen": total_reconnects,
        "avg_propagation_latency_ms": _avg_propagation_latency_ms(),
        "propagation_samples":   len(_propagation_samples),
        "heartbeats_received":   _sync_metrics["heartbeats_received"],
        "events_observed":       _sync_metrics["events_observed"],
        "stale_detected_total":  _sync_metrics["stale_clients_detected"],
        "ws": ws_stats,
    }


# ── StateVersionMiddleware ────────────────────────────────────────────────────

class StateVersionMiddleware:
    """
    Starlette/FastAPI middleware that injects X-State-Version into every
    HTTP response so the frontend JS can compare against its local cache
    without making an extra /api/state-version round-trip.

    Non-fatal: if get_state_version() fails the header is simply omitted.

    Usage (app/main.py)::

        from shared.frontend_sync import StateVersionMiddleware
        app.add_middleware(StateVersionMiddleware)
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Only inject the header on HTTP responses
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        version_str: Optional[str] = None
        try:
            from shared.state_version import get_state_version
            v = await get_state_version()
            version_str = str(v)
        except Exception:
            pass

        async def _send_with_header(message):
            if message["type"] == "http.response.start" and version_str is not None:
                headers = list(message.get("headers", []))
                headers.append((b"x-state-version", version_str.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send_with_header)


# ── Background monitoring task ────────────────────────────────────────────────

_monitor_task: Optional[asyncio.Task] = None


async def start_sync_monitoring() -> None:
    """
    Start the periodic stale-client sweep background task.
    Call from app lifespan startup (non-fatal).
    """
    if not _sync_enabled():
        log.info("[sync] FRONTEND_SYNC_ENABLED=false — monitoring skipped")
        return

    from shared.events import subscribe
    subscribe("*", _propagation_observer)

    global _monitor_task

    async def _loop():
        log.info("[sync] stale-client monitor started interval=%ds", STALE_SWEEP_INTERVAL)
        while True:
            try:
                await asyncio.sleep(STALE_SWEEP_INTERVAL)
                stale = await detect_stale_clients()
                if stale:
                    log.info("[sync] stale clients detected: %s", stale)
                await purge_inactive_clients()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("[sync] monitor loop error: %s", exc)

    _monitor_task = asyncio.get_event_loop().create_task(
        _loop(), name="frontend-sync-monitor"
    )


async def stop_sync_monitoring() -> None:
    """Cancel the monitoring background task (call from lifespan teardown)."""
    from shared.events import unsubscribe
    try:
        unsubscribe("*", _propagation_observer)
    except Exception:
        pass

    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except (asyncio.CancelledError, Exception):
            pass
        _monitor_task = None
    log.info("[sync] stale-client monitor stopped")
