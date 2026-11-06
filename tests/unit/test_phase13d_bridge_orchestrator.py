"""
Phase 13D — Multi-Bridge Orchestration tests
============================================

Tests:
  1. test_duplicate_inbound_across_bridges   — same message from bridge1 + bridge2
                                               → second call returns True (skip)
  2. test_reconnect_replay                   — bridge in outage state; messages queued;
                                               bridge recovers → queued messages replayed
  3. test_admin_approval_routing             — send_to_admin sends to admin self-JID
                                               via bridge2 (admin authority)
  4. test_draft_auto_cancel                  — admin manual reply detected via
                                               should_generate_draft → draft auto-cancelled
  5. test_bridge_failover                    — bridge2 outage → send routes to bridge1

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

# ── Fixtures ─────────────────────────────────────────────────────────────────

def _fresh_orchestrator():
    """Create a fresh BridgeOrchestrator with all in-process state cleared."""
    # Patch get_settings to avoid requiring a live .env file
    from unittest.mock import MagicMock
    mock_settings = MagicMock()
    mock_settings.bridge1_number = "8801958122300"
    mock_settings.bridge2_number = "8801880446111"
    mock_settings.bridge1_url = "http://localhost:8080"
    mock_settings.bridge2_url = "http://localhost:8081"

    with patch("shared.bridge_orchestrator.get_settings", return_value=mock_settings):
        from shared.bridge_orchestrator import BridgeOrchestrator
        orch = BridgeOrchestrator()
    return orch


# ── Test 1: Cross-bridge duplicate detection ──────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_inbound_across_bridges():
    """
    When the same WhatsApp message arrives from bridge1 AND bridge2,
    the second registration must be detected as a duplicate and return True.
    The first registration must return False (new message, process it).
    """
    orch = _fresh_orchestrator()
    sender_jid = "880123456789@s.whatsapp.net"
    content = "I need escort duty confirmation"
    msg_ts = time.time()  # same timestamp bucket

    with patch("shared.bridge_orchestrator.emit", new=AsyncMock()):
        # First registration — bridge1 sees the message
        is_dup_1 = await orch.register_message(
            bridge_name="bridge1",
            sender_jid=sender_jid,
            content=content,
            msg_ts=msg_ts,
        )
        # Second registration — bridge2 sees the same message
        is_dup_2 = await orch.register_message(
            bridge_name="bridge2",
            sender_jid=sender_jid,
            content=content,
            msg_ts=msg_ts,
        )

    assert is_dup_1 is False, "First registration should NOT be a duplicate"
    assert is_dup_2 is True, "Second registration from different bridge should be duplicate"
    assert orch._total_dedup_hits == 1
    assert orch._health["bridge2"].dedup_rejected == 1
    # bridge1 message_count = 1, bridge2 message_count = 1 (both incremented)
    assert orch._health["bridge1"].message_count == 1
    assert orch._health["bridge2"].message_count == 1


# ── Test 2: Reconnect replay ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconnect_replay():
    """
    When bridge2 is in 'outage' state and send_with_failover routes to bridge1,
    the failed send is queued for retry.
    When the bridge transitions from outage → healthy, queued messages are replayed.
    """
    orch = _fresh_orchestrator()

    # Force bridge2 into outage state
    orch._health["bridge2"].state = "outage"
    orch._health["bridge2"].last_healthy = time.time() - 200.0

    mock_client1 = AsyncMock()
    mock_client1.send_strict = AsyncMock(return_value=None)   # bridge1 succeeds
    mock_client2 = AsyncMock()
    mock_client2.send_strict = AsyncMock(side_effect=Exception("bridge2 down"))

    with patch("shared.bridge_orchestrator.get_bridge1", return_value=mock_client1), \
         patch("shared.bridge_orchestrator.get_bridge2", return_value=mock_client2), \
         patch("shared.bridge_orchestrator.emit", new=AsyncMock()):

        # Send — should failover to bridge1 since bridge2 is in outage
        success, used = await orch.send_with_failover(
            "880111@s.whatsapp.net",
            "Test message",
            preferred_bridge="bridge2",
        )

    assert success is True
    assert used == "bridge1"

    # Now test reconnect replay: queue an item directly for bridge2
    from shared.bridge_orchestrator import _RetryItem
    orch._retry_queue.append(
        _RetryItem(
            bridge_name="bridge2",
            jid="880222@s.whatsapp.net",
            text="Queued message",
            attempt=0,
            next_retry_at=time.time() - 1,
        )
    )

    # bridge2 comes back online
    orch._health["bridge2"].state = "outage"

    with patch("shared.bridge_orchestrator.get_bridge2", return_value=mock_client1), \
         patch("shared.bridge_orchestrator.emit", new=AsyncMock()) as mock_emit:
        await orch._drain_retry_for_bridge("bridge2")

    # Retry queue should be empty after successful replay
    assert len(orch._retry_queue) == 0
    # BRIDGE_RECONNECTED event emitted with replay count
    mock_emit.assert_called_once()
    call_args = mock_emit.call_args
    assert call_args[0][0] == "bridge_reconnected"
    assert call_args[0][1]["replayed"] == 1


# ── Test 3: Admin approval routing ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_approval_routing():
    """
    send_to_admin must deliver to the admin self-JID (8801880446111@s.whatsapp.net)
    via bridge2 (admin authority), and emit DRAFT_APPROVAL_SENT event.
    """
    from shared.bridge_orchestrator import ADMIN_SELF_JID, ADMIN_BRIDGE_NAME

    orch = _fresh_orchestrator()

    mock_bridge2 = AsyncMock()
    mock_bridge2.send_strict = AsyncMock(return_value=None)  # success

    captured_sends = []

    async def capture_send(jid, text):
        captured_sends.append({"jid": jid, "text": text})

    mock_bridge2.send_strict = capture_send

    captured_events = []

    async def capture_emit(event_type, payload=None, *, emitted_by="unknown"):
        captured_events.append({"type": event_type, "payload": payload or {}})

    with patch("shared.bridge_orchestrator.get_bridge2", return_value=mock_bridge2), \
         patch("shared.bridge_orchestrator.emit", side_effect=capture_emit):
        result = await orch.send_to_admin(
            "DRAFT APPROVAL REQUIRED: escort payment for Ahmed — reply APPROVE/REJECT",
            context={"draft_id": 42},
        )

    assert result is True, "send_to_admin should return True on success"
    assert len(captured_sends) == 1
    assert captured_sends[0]["jid"] == ADMIN_SELF_JID
    assert "DRAFT APPROVAL" in captured_sends[0]["text"]

    # DRAFT_APPROVAL_SENT event should have been emitted
    approval_events = [e for e in captured_events if e["type"] == "draft_approval_sent"]
    assert len(approval_events) == 1
    assert approval_events[0]["payload"]["bridge_used"] == ADMIN_BRIDGE_NAME
    assert approval_events[0]["payload"]["draft_id"] == 42


# ── Test 4: Draft auto-cancel on manual reply ─────────────────────────────────

@pytest.mark.asyncio
async def test_draft_auto_cancel():
    """
    If admin already replied manually to a chat after the inbound message,
    shared.draft.should_generate_draft returns False, preventing a new draft.
    The orchestrator's is_historical helper also blocks historical messages.
    """
    # Import first so the module namespace is established
    import shared.draft as draft_mod
    from datetime import datetime, timezone

    inbound_ts = datetime.now(timezone.utc)

    # Patch fetch_val on the already-imported module namespace so the
    # already-replied guard returns 1 (admin has replied)
    with patch.object(draft_mod, "fetch_val", new=AsyncMock(return_value=1)), \
         patch.object(draft_mod, "fetch_one", new=AsyncMock(return_value=None)):
        result = await draft_mod.should_generate_draft(
            "880123@s.whatsapp.net",
            inbound_ts,
        )

    assert result is False, "should_generate_draft must return False when admin already replied"

    # Also test orchestrator's is_historical for old messages
    orch = _fresh_orchestrator()
    old_ts = time.time() - 400.0   # 400s ago → historical (cutoff is 300s)
    new_ts = time.time() - 60.0    # 60s ago → NOT historical

    assert orch.is_historical(old_ts) is True, "400s old message should be historical"
    assert orch.is_historical(new_ts) is False, "60s old message should NOT be historical"


# ── Test 5: Bridge failover ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bridge_failover():
    """
    When preferred bridge2 raises BridgeSendError, send_with_failover must
    silently fallback to bridge1 and emit a BRIDGE_FAILOVER event.
    """
    from app.bridge import BridgeSendError

    orch = _fresh_orchestrator()

    mock_bridge2 = AsyncMock()
    mock_bridge2.send_strict = AsyncMock(
        side_effect=BridgeSendError("bridge2 send failed")
    )
    mock_bridge1 = AsyncMock()
    mock_bridge1.send_strict = AsyncMock(return_value=None)  # bridge1 succeeds

    captured_events = []

    async def capture_emit(event_type, payload=None, *, emitted_by="unknown"):
        captured_events.append({"type": event_type, "payload": payload or {}})

    with patch("shared.bridge_orchestrator.get_bridge2", return_value=mock_bridge2), \
         patch("shared.bridge_orchestrator.get_bridge1", return_value=mock_bridge1), \
         patch("shared.bridge_orchestrator.emit", side_effect=capture_emit):
        success, used = await orch.send_with_failover(
            "880777@s.whatsapp.net",
            "Urgent escort assignment",
            preferred_bridge="bridge2",
        )

    assert success is True, "Failover to bridge1 should succeed"
    assert used == "bridge1", "bridge1 should be the fallback bridge"

    # BRIDGE_FAILOVER event emitted with correct fields
    failover_events = [e for e in captured_events if e["type"] == "bridge_failover"]
    assert len(failover_events) == 1
    assert failover_events[0]["payload"]["preferred"] == "bridge2"
    assert failover_events[0]["payload"]["used"] == "bridge1"

    # bridge1 send_strict was called with correct JID
    mock_bridge1.send_strict.assert_called_once()
    call_jid = mock_bridge1.send_strict.call_args[0][0]
    assert call_jid == "880777@s.whatsapp.net"
