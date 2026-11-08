"""Bridge-store history reader (2026-11-06) — modules.bridge_poller._fetch_history_messages.

The live poller must keep behaving exactly as before (inbound only,
history-sync excluded, cursor-based). The importer needs the opposite: the
whole store, both directions, paged. These tests pin that contract against a
real (temporary) SQLite store shaped like a whatsmeow bridge store.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pytest

from modules.bridge_poller import _fetch_history_messages


def _make_store(tmp_path):
    messages_db = tmp_path / "messages.db"
    whatsapp_db = tmp_path / "whatsapp.db"

    con = sqlite3.connect(messages_db)
    con.execute(
        """CREATE TABLE messages (
               id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TEXT,
               media_type TEXT, filename TEXT, url TEXT,
               is_from_me INTEGER, from_history_sync INTEGER)"""
    )
    con.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            # inbound DM, replayed by whatsmeow history sync
            ("m1", "8801711111111@s.whatsapp.net", "8801711111111@s.whatsapp.net",
             "hello", "2026-10-01T10:00:00+00:00", None, None, None, 0, 1),
            # outbound (typed on the phone / sent by us) to the same person
            ("m2", "8801711111111@s.whatsapp.net", "8801999999999@s.whatsapp.net",
             "reply from me", "2026-10-01T10:05:00+00:00", None, None, None, 1, 1),
            # group chat — excluded unless explicitly requested
            ("m3", "120363000000000000@g.us", "8801711111111@s.whatsapp.net",
             "group msg", "2026-10-01T10:06:00+00:00", None, None, None, 0, 1),
            # live inbound DM
            ("m4", "8801722222222@s.whatsapp.net", "8801722222222@s.whatsapp.net",
             "live", "2026-10-02T09:00:00+00:00", None, None, None, 0, 0),
            # reaction — never a message body
            ("m5", "8801733333333@s.whatsapp.net", "8801733333333@s.whatsapp.net",
             "reaction", "2026-10-02T09:01:00+00:00", "reaction", None, None, 0, 0),
            # newsletter — excluded
            ("m6", "120363000000000001@newsletter", "120363000000000001@newsletter",
             "news", "2026-10-02T09:02:00+00:00", None, None, None, 0, 0),
        ],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(whatsapp_db)
    con.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT, pn TEXT)")
    con.execute("INSERT INTO whatsmeow_lid_map VALUES ('111', '8801711111111')")
    con.execute(
        "CREATE TABLE whatsmeow_contacts (their_jid TEXT, full_name TEXT, push_name TEXT, business_name TEXT)"
    )
    con.commit()
    con.close()
    return str(messages_db), str(whatsapp_db)


def test_both_directions_and_exclusions(tmp_path):
    messages_db, whatsapp_db = _make_store(tmp_path)
    rows, _next, done = _fetch_history_messages(messages_db, whatsapp_db, batch=200)

    assert [r["id"] for r in rows] == ["m1", "m2", "m4"]
    assert done is True

    inbound, outbound, live = rows
    assert inbound["direction"] == "inbound"
    assert inbound["phone"] == "8801711111111"

    # For an outbound row the counterparty is the CHAT, never our own sender.
    assert outbound["direction"] == "outbound"
    assert outbound["phone"] == "8801711111111"

    assert live["phone"] == "8801722222222"
    assert live["text"] == "live"


def test_inbound_only_mode(tmp_path):
    messages_db, whatsapp_db = _make_store(tmp_path)
    rows, _n, _d = _fetch_history_messages(messages_db, whatsapp_db, include_outbound=False)
    assert [r["id"] for r in rows] == ["m1", "m4"]


def test_outbound_only_mode_is_what_the_live_capture_loop_uses(tmp_path):
    messages_db, whatsapp_db = _make_store(tmp_path)
    rows, _n, _d = _fetch_history_messages(messages_db, whatsapp_db, direction="outbound")
    assert [r["id"] for r in rows] == ["m2"]
    assert rows[0]["direction"] == "outbound"


def test_since_and_until_filters(tmp_path):
    messages_db, whatsapp_db = _make_store(tmp_path)
    rows, _n, _d = _fetch_history_messages(
        messages_db, whatsapp_db, since_ts=datetime(2026, 10, 2, tzinfo=timezone.utc)
    )
    assert [r["id"] for r in rows] == ["m4"]

    rows, _n, _d = _fetch_history_messages(
        messages_db, whatsapp_db, until_ts=datetime(2026, 10, 1, 23, tzinfo=timezone.utc)
    )
    assert [r["id"] for r in rows] == ["m1", "m2"]


def test_paging_by_rowid_is_resumable(tmp_path):
    messages_db, whatsapp_db = _make_store(tmp_path)

    page1, next_rowid, done1 = _fetch_history_messages(messages_db, whatsapp_db, batch=2)
    assert [r["id"] for r in page1] == ["m1", "m2"]
    assert done1 is False
    assert next_rowid > 0

    page2, next_rowid2, done2 = _fetch_history_messages(
        messages_db, whatsapp_db, after_rowid=next_rowid, batch=2
    )
    assert [r["id"] for r in page2] == ["m4"]
    assert done2 is True

    # Re-reading from the last rowid yields nothing new — the import is
    # idempotent at the reader level, and message_hash dedupes at the writer.
    page3, _n, _d = _fetch_history_messages(messages_db, whatsapp_db, after_rowid=next_rowid2)
    assert page3 == []


def test_missing_store_degrades_to_empty(tmp_path):
    rows, next_rowid, done = _fetch_history_messages(
        str(tmp_path / "nope.db"), str(tmp_path / "nope-wa.db")
    )
    assert rows == []
    assert next_rowid == 0
    assert done is True
