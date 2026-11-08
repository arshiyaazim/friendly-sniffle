"""Canonical outbound persistence (2026-11-06).

Covers the three gaps this work closed:

  1. `_save_message()` used to leave `canonical_phone` / `phone_last10` /
     `original_sender_number` empty on every outbound row it wrote.
  2. Nothing recorded WHICH bridge carried a message (`source_bridge`) or
     whether delivery was accepted (`delivery_status`, `external_id`).
  3. Nothing recorded whether an LLM wrote the body (`ai_generated` +
     provider/model/task).

Plus schema tolerance: the same writer must work on a database that has not
run migration 091 yet.
"""
from __future__ import annotations

import pytest

from modules import ai_provenance, bridge_poller


BASE_COLUMNS = frozenset({
    "message_id", "sender_number", "message_body", "message_type", "direction",
    "platform", "is_processed", "contact_identifier", "identity_role",
    "identity_confidence", "workflow_triggered", "receiver_number",
    "conversation_key", "intent_detected", "extracted_text", "actor_type",
    "actor_id", "message_hash", "metadata_json", "received_at",
})
MIGRATION_091_COLUMNS = BASE_COLUMNS | {
    "canonical_phone", "phone_last10", "original_sender_number", "source_bridge",
    "ai_generated", "ai_provider", "ai_model", "ai_task", "outbound_queue_id",
    "delivery_status", "external_id", "history_imported",
}


class _Capture:
    def __init__(self):
        self.calls = []

    async def fetch_one(self, sql, *args):
        self.calls.append(("fetch_one", " ".join(sql.split()), args))
        if "INSERT" in sql or "UPDATE" in sql:
            return {"message_id": 4242}
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", " ".join(sql.split()), args))
        return "UPDATE 1"

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture()
def capture(monkeypatch):
    cap = _Capture()
    bridge_poller.reset_core_column_cache()
    monkeypatch.setattr(bridge_poller, "fetch_one", cap.fetch_one)
    monkeypatch.setattr(bridge_poller, "execute", cap.execute)
    ai_provenance.reset_for_test()
    yield cap
    ai_provenance.reset_for_test()


def _patch_columns(monkeypatch, cols):
    import modules.identity_resolver as ir

    async def _cols(_table):
        return cols

    monkeypatch.setattr(ir, "available_columns", _cols)
    bridge_poller.reset_core_column_cache()


ME = "8801958122300"
CUSTOMER = "8801711111111"


class TestSaveMessage:
    async def test_outbound_row_gets_phone_columns_bridge_and_ai_flag(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)
        ai_provenance.set_task("reply")
        ai_provenance.record_success("ollama", "hermes3:3b")

        await bridge_poller._save_message(
            "bridge2", CUSTOMER, "assalamu alaikum", "outbound",
            actor_type="automation", outbound_queue_id=77,
        )

        _kind, sql, args = capture.last
        assert sql.startswith("INSERT INTO wbom_whatsapp_messages")
        for col in ("canonical_phone", "phone_last10", "original_sender_number",
                    "source_bridge", "ai_generated", "ai_provider", "ai_model",
                    "ai_task", "outbound_queue_id"):
            assert col in sql, col
        idx = sql.split("(")[1].split(")")[0].replace(" ", "").split(",")
        row = dict(zip(idx, args))
        assert row["sender_number"] == CUSTOMER
        assert row["canonical_phone"] == "8801711111111"
        assert row["phone_last10"] == "1711111111"
        assert row["source_bridge"] == "bridge2"
        assert row["ai_generated"] is True
        assert row["ai_provider"] == "ollama"
        assert row["ai_model"] == "hermes3:3b"
        assert row["outbound_queue_id"] == 77

    async def test_human_device_row_is_never_ai(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)
        ai_provenance.set_task("reply")
        ai_provenance.record_success("ollama", "hermes3:3b")

        await bridge_poller._save_message(
            "bridge2", ME, "typed on the phone", "outbound", actor_type="human_device",
        )
        _kind, sql, args = capture.last
        cols = sql.split("(")[1].split(")")[0].replace(" ", "").split(",")
        row = dict(zip(cols, args))
        assert row["ai_generated"] is False

    async def test_rule_based_automation_is_false(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)
        await bridge_poller._save_message(
            "bridge1", CUSTOMER, "ok", "outbound", actor_type="automation",
        )
        _kind, sql, args = capture.last
        cols = sql.split("(")[1].split(")")[0].replace(" ", "").split(",")
        row = dict(zip(cols, args))
        assert row["ai_generated"] is False
        # no provider/model means the columns are simply not written (NULL)
        assert "ai_provider" not in row

    async def test_works_on_pre_091_schema(self, capture, monkeypatch):
        """No migration 091: unknown columns are dropped, the INSERT still runs."""
        _patch_columns(monkeypatch, BASE_COLUMNS)
        await bridge_poller._save_message(
            "bridge1", CUSTOMER, "hi", "outbound", actor_type="automation",
            outbound_queue_id=5,
        )
        _kind, sql, _args = capture.last
        assert sql.startswith("INSERT INTO wbom_whatsapp_messages")
        assert "canonical_phone" not in sql
        assert "source_bridge" not in sql
        assert "ai_generated" not in sql
        assert "sender_number" in sql


class TestPersistOutboundMessage:
    async def test_upgrades_the_queue_linked_row(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)
        mid = await bridge_poller.persist_outbound_message(
            source_bridge="bridge2", recipient=CUSTOMER, text="queued reply",
            outbound_queue_id=99, external_id="bridge2:8801711111111",
            ai_generated=True, ai_provider="groq", ai_model="allam-2-7b",
            ai_task="reply",
        )
        assert mid == 4242
        kind, sql, args = capture.last
        assert kind == "fetch_one"
        assert sql.startswith("UPDATE wbom_whatsapp_messages")
        assert "outbound_queue_id = $1" in sql
        assert "delivery_status = $3" in sql
        assert args[0] == 99
        assert args[1] == "bridge2"
        assert args[2] == "sent"

    async def test_inserts_when_no_row_is_linked(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)

        # No queue-linked row, no pre-saved row -> INSERT path.
        async def _nothing(sql, *args):
            capture.calls.append(("fetch_one", " ".join(sql.split()), args))
            if "INSERT" in sql:
                return {"message_id": 555}
            return None

        monkeypatch.setattr(bridge_poller, "fetch_one", _nothing)

        mid = await bridge_poller.persist_outbound_message(
            source_bridge="bridge3", recipient="01712345678", text="draft approved",
            outbound_queue_id=12, ai_generated="unknown",
        )
        assert mid == 555
        _kind, sql, args = capture.last
        assert sql.startswith("INSERT INTO wbom_whatsapp_messages")
        cols = sql.split("(")[1].split(")")[0].replace(" ", "").split(",")
        row = dict(zip(cols, args))
        assert row["direction"] == "outbound"
        assert row["source_bridge"] == "bridge3"
        assert row["canonical_phone"] == "8801712345678"   # local form normalised
        assert row["delivery_status"] == "sent"
        assert row["outbound_queue_id"] == 12
        assert row["ai_generated"] is None                # unknown, not guessed false
        assert row["message_hash"]                          # deterministic idempotency

    async def test_failover_records_the_bridge_that_actually_sent(self, capture, monkeypatch):
        _patch_columns(monkeypatch, MIGRATION_091_COLUMNS)
        await bridge_poller.persist_outbound_message(
            source_bridge="bridge1", recipient=CUSTOMER, text="x",
            outbound_queue_id=3, failover_from="bridge2",
        )
        _kind, _sql, args = capture.last
        meta = args[-1]
        assert "bridge2" in str(meta)   # requested bridge kept in metadata
