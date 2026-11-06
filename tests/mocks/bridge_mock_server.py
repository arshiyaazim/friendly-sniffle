"""
Mock bridge server — mimics the bridge1 / bridge2 WhatsApp relay API.

Start:
  uvicorn tests.mocks.bridge_mock_server:app --port 8201

Or via Docker (see docker-compose.test.yml).

Endpoints:
  POST /send         — Accept outgoing message, record it
  GET  /messages     — Return all recorded messages (for test assertions)
  POST /clear        — Clear message log
  GET  /health       — Health check
"""
from __future__ import annotations

import os
from typing import Any
from fastapi import FastAPI, Body, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock Bridge Server")

# In-memory message log (thread-safe enough for test use)
_messages: list[dict[str, Any]] = []


class SendRequest(BaseModel):
    to: str
    message: str
    type: str = "text"


@app.get("/health")
async def health():
    return {"status": "ok", "bridge": "mock", "message_count": len(_messages)}


@app.post("/send")
async def send_message(body: SendRequest):
    _messages.append({
        "to": body.to,
        "message": body.message,
        "type": body.type,
    })
    return {"status": "sent", "message_id": f"mock-{len(_messages):05d}"}


@app.get("/messages")
async def get_messages():
    """Return all recorded outgoing messages for assertions."""
    return {"messages": _messages, "count": len(_messages)}


@app.post("/clear")
async def clear_messages():
    """Reset the message log between tests."""
    _messages.clear()
    return {"status": "cleared"}


@app.post("/webhook")
async def receive_webhook(payload: dict = Body(...)):
    """Accept simulated inbound messages (for testing inbound flows)."""
    return {"status": "received"}
