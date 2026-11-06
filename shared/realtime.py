"""
shared.realtime — WebSocket Realtime Broadcast (Phase 12C)
==========================================================

Provides a FastAPI WebSocket endpoint (`/ws/realtime`) that pushes domain
events to connected dashboard clients in real-time.

Architecture
------------
* A single ConnectionManager holds all active WebSocket connections.
* On startup, a handler is registered with shared.events to forward all
  domain events to every connected client.
* Clients reconnect automatically — no infinite-refresh loops.
* Falls back gracefully: if no WebSocket clients are connected, events are
  simply discarded (they also trigger REST poll via state-version counter).
* Ping/heartbeat every 30 s to detect dead connections.

Integration
-----------
1. Include the router in app/main.py:

       from shared.realtime import router as realtime_router
       app.include_router(realtime_router)

2. Start the event bridge in the lifespan hook:

       from shared.realtime import start_event_bridge
       start_event_bridge()

Dashboard JS client (lightweight polling fallback also works):
---------------------------------------------------------------
    const ws = new WebSocket(`wss://${location.host}/ws/realtime`);
    ws.onmessage = (e) => {
        const evt = JSON.parse(e.data);
        if (["payment_created","employee_updated","escort_assigned"].includes(evt.event_type)) {
            refreshCurrentTab();
        }
    };
    // Auto-reconnect on disconnect
    ws.onclose = () => setTimeout(() => connectWs(), 3000);
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from shared import events as _events

log = logging.getLogger("fazle.realtime")

router = APIRouter(tags=["realtime"])

# ── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    """Manages all active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._connected_at: dict[int, float] = {}     # id(ws) → unix timestamp

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)
        self._connected_at[id(ws)] = time.time()
        log.info("[realtime] client connected — total=%d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [c for c in self._connections if c is not ws]
        self._connected_at.pop(id(ws), None)
        log.info("[realtime] client disconnected — total=%d", len(self._connections))

    async def broadcast(self, message: dict) -> None:
        """Send a dict to all connected clients; silently drop dead connections."""
        if not self._connections:
            return
        payload = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(payload)
                else:
                    dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def ping_all(self) -> None:
        """Send a keep-alive ping to all clients."""
        await self.broadcast({"type": "ping", "ts": time.time()})

    @property
    def client_count(self) -> int:
        return len(self._connections)

    def get_stats(self) -> dict:
        now = time.time()
        return {
            "connected_clients": len(self._connections),
            "client_ages_s": [
                round(now - self._connected_at.get(id(ws), now), 1)
                for ws in self._connections
            ],
        }


_manager = ConnectionManager()
_heartbeat_task: Optional[asyncio.Task] = None


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/realtime")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket endpoint for dashboard real-time updates.

    Clients receive domain events as JSON objects:
        {"event_type": "payment_created", "payload": {...}, "timestamp": 1234567890.1}

    Clients should reconnect automatically on disconnect (3 s back-off).
    """
    await _manager.connect(ws)
    try:
        # Send initial state-version so client can decide whether to refresh
        from shared.state_version import get_state_version
        await ws.send_text(json.dumps({
            "type": "connected",
            "state_version": await get_state_version(),
            "ts": time.time(),
        }))
        # Keep the connection alive — wait for client messages (ping/pong)
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=60)
                if data == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
            except asyncio.TimeoutError:
                # No message for 60 s — check if still alive
                if ws.client_state != WebSocketState.CONNECTED:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("[realtime] ws exception: %s", exc)
    finally:
        _manager.disconnect(ws)


# ── Event bridge — connects shared.events to WebSocket broadcasts ─────────────

async def _forward_event(evt: _events.EventEnvelope) -> None:
    """Received from shared.events; broadcast to all WS clients."""
    await _manager.broadcast(evt.to_dict())


def start_event_bridge() -> None:
    """
    Register the forwarding handler with shared.events.
    Call this ONCE during app startup (lifespan).
    """
    _events.subscribe("*", _forward_event)
    log.info("[realtime] event bridge started — WS broadcast enabled")

    global _heartbeat_task
    loop = asyncio.get_event_loop()
    _heartbeat_task = loop.create_task(_heartbeat_loop(), name="realtime:heartbeat")


async def _heartbeat_loop() -> None:
    """Send a ping every 30 s to keep connections alive through proxies."""
    while True:
        await asyncio.sleep(30)
        if _manager.client_count > 0:
            await _manager.ping_all()


def get_realtime_stats() -> dict:
    """Return realtime connection stats for the diagnostics endpoint."""
    return _manager.get_stats()
