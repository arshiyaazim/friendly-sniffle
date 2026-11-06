"""
shared.events — Domain Event Bus (Phase 12D)
=============================================

Lightweight in-process event bus for coordinating state changes across
modules, workers, and frontend connections.

Design
------
* No broker required — events propagate in-process first (asyncio tasks).
* Realtime WebSocket broadcast is handled by shared.realtime, which
  subscribes to this bus.
* Events are fire-and-forget by default: emission never blocks the caller.
* Subscribers are async callbacks registered per event type.
* Wildcard subscription ("*") receives all events.

Domain event types
------------------
  payment_created       Employee payment recorded (new FPE transaction)
  payment_corrected     FPE transaction repaired / reversed (repair tool)
  payment_failed        Ingestion attempted but failed (parse error etc.)
  employee_updated      Employee record modified (name, phone, salary…)
  escort_assigned       Employee assigned to escort program
  escort_released       Employee released from escort program
  escort_completed      Escort program marked complete
  draft_created         Auto-reply draft generated
  draft_approved        Draft promoted to sent
  draft_cancelled       Draft expired or rejected
  stale_bridge_detected Bridge last-seen exceeds threshold
  lock_contention       Write router couldn't acquire a lock
  duplicate_write       Idempotency check caught a duplicate write attempt

USAGE
-----
    from shared.events import subscribe, emit, EventEnvelope

    # Subscribe (call at startup, e.g. in lifespan)
    async def on_payment(evt: EventEnvelope) -> None:
        print(evt.event_type, evt.payload)

    subscribe("payment_created", on_payment)
    subscribe("*", log_all_events)          # wildcard

    # Emit from any module (non-blocking)
    await emit("payment_created", {"txn_ref": "fpe-abc123", "employee_id": 42})

    # Await a flush (use in tests)
    await flush_pending()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("fazle.events")

# ── Public domain event types ─────────────────────────────────────────────────

PAYMENT_CREATED      = "payment_created"
PAYMENT_CORRECTED    = "payment_corrected"
PAYMENT_FAILED       = "payment_failed"
EMPLOYEE_UPDATED     = "employee_updated"
ESCORT_ASSIGNED      = "escort_assigned"
ESCORT_RELEASED      = "escort_released"
ESCORT_COMPLETED     = "escort_completed"
DRAFT_CREATED        = "draft_created"
DRAFT_APPROVED       = "draft_approved"
DRAFT_CANCELLED      = "draft_cancelled"
STALE_BRIDGE         = "stale_bridge_detected"
LOCK_CONTENTION      = "lock_contention"
DUPLICATE_WRITE      = "duplicate_write"

# Phase 13A — Runtime Gateway events
NODE_ONLINE          = "node_online"
NODE_OFFLINE         = "node_offline"
QUEUE_PRESSURE       = "queue_pressure"
STALE_FRONTEND       = "stale_frontend"
RECONNECTING         = "reconnecting"
WS_RECOVERED         = "ws_recovered"

# Phase 13C — Unified Frontend Synchronization events
PAYMENT_UPDATED      = "payment_updated"
ESCORT_UPDATED       = "escort_updated"
DRAFT_UPDATED        = "draft_updated"
TRANSACTION_REPAIRED = "transaction_repaired"

# Phase 13D — Multi-Bridge Orchestration events
BRIDGE_HEALTH_CHANGED    = "bridge_health_changed"    # any state transition
BRIDGE_FAILOVER          = "bridge_failover"          # preferred bridge skipped
BRIDGE_RECONNECTED       = "bridge_reconnected"       # outage→healthy, replay done
CROSS_BRIDGE_DUPLICATE   = "cross_bridge_duplicate"   # same msg deduplicated
DRAFT_APPROVAL_SENT      = "draft_approval_sent"      # system sent draft to admin self-chat
DRAFT_APPROVED_BY_ADMIN  = "draft_approved_by_admin"  # admin replied APPROVE
DRAFT_REJECTED_BY_ADMIN  = "draft_rejected_by_admin"  # admin replied REJECT
DRAFT_AUTO_CANCELLED     = "draft_auto_cancelled"     # admin manual reply → draft expired

# Phase 13E — Self-Healing Runtime events
SELF_HEAL_PANIC           = "self_heal_panic"           # pressure ≥ PANIC_THRESHOLD
SELF_HEAL_RECOVERED       = "self_heal_recovered"       # pressure ≤ PANIC_CLEAR
SELF_HEAL_WORKER_RESTARTED = "self_heal_worker_restarted"  # outbound worker restarted
SELF_HEAL_WS_RESTARTED    = "self_heal_ws_restarted"    # WebSocket heartbeat restarted
SELF_HEAL_ATTEMPTED       = "self_heal_attempted"       # generic recovery action taken


# ── Event envelope ────────────────────────────────────────────────────────────

@dataclass
class EventEnvelope:
    event_type: str
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    emitted_by: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "emitted_by": self.emitted_by,
        }


# ── Subscriber registry ───────────────────────────────────────────────────────

Handler = Callable[[EventEnvelope], Awaitable[None]]

_subscribers: Dict[str, List[Handler]] = {}   # event_type → [handlers]
_pending_tasks: List[asyncio.Task] = []        # background tasks (for flush)

# Metrics for diagnostics
_metrics: Dict[str, int] = {
    "emitted": 0,
    "handled": 0,
    "failed": 0,
}


def subscribe(event_type: str, handler: Handler) -> None:
    """
    Register `handler` to be called when `event_type` is emitted.

    Use event_type="*" to subscribe to all events (wildcard).
    Safe to call multiple times — duplicate handlers are deduplicated by identity.
    """
    bucket = _subscribers.setdefault(event_type, [])
    if handler not in bucket:
        bucket.append(handler)
        log.debug("[events] subscribed handler=%s to event=%s", handler.__name__, event_type)


def unsubscribe(event_type: str, handler: Handler) -> None:
    """Remove a previously registered handler."""
    if event_type in _subscribers:
        _subscribers[event_type] = [h for h in _subscribers[event_type] if h is not handler]


async def emit(
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    emitted_by: str = "unknown",
) -> None:
    """
    Emit a domain event — non-blocking.

    All matching handlers (and wildcard handlers) are scheduled as
    asyncio tasks so they never block the caller.

    Parameters
    ----------
    event_type : One of the domain constants above, or a custom string.
    payload    : JSON-serialisable dict of event data.
    emitted_by : Module/worker identifier for observability.
    """
    envelope = EventEnvelope(
        event_type=event_type,
        payload=payload or {},
        emitted_by=emitted_by,
    )
    _metrics["emitted"] += 1

    handlers: List[Handler] = []
    handlers.extend(_subscribers.get(event_type, []))
    handlers.extend(_subscribers.get("*", []))          # wildcard

    if not handlers:
        log.debug("[events] no subscribers for event_type=%s", event_type)
        return

    for handler in handlers:
        task = asyncio.create_task(
            _safe_call(handler, envelope),
            name=f"evt:{event_type}:{handler.__name__}",
        )
        _pending_tasks.append(task)
        task.add_done_callback(_pending_tasks.remove)


async def _safe_call(handler: Handler, envelope: EventEnvelope) -> None:
    """Call a handler, swallowing any exception so one bad handler can't crash others."""
    try:
        await handler(envelope)
        _metrics["handled"] += 1
    except Exception as exc:
        _metrics["failed"] += 1
        log.error(
            "[events] handler %s raised on event %s: %s",
            handler.__name__, envelope.event_type, exc,
        )


async def flush_pending(timeout: float = 5.0) -> None:
    """
    Wait for all in-flight event tasks to complete (use in tests).

    Parameters
    ----------
    timeout : Maximum seconds to wait before returning (default 5s).
    """
    if not _pending_tasks:
        return
    active = list(_pending_tasks)
    if active:
        await asyncio.wait(active, timeout=timeout)


def get_metrics() -> Dict[str, int]:
    """Return a snapshot of emit / handle / fail counters for diagnostics."""
    return dict(_metrics)


def get_subscriber_counts() -> Dict[str, int]:
    """Return the number of handlers registered per event type."""
    return {k: len(v) for k, v in _subscribers.items()}


def clear_all_subscribers() -> None:
    """Remove all subscribers — for use in test teardown only."""
    _subscribers.clear()
    log.warning("[events] all subscribers cleared (test mode?)")
