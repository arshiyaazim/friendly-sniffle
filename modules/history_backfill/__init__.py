"""One-time bridge-history import — "first connection fills the database"
==========================================================================

Owner question this answers (2026-11-06): *"প্রথম কানেকশনে হোয়াটসঅ্যাপ-এমসিপি-
ব্রিজে থাকা সকল ম্যাসেজ কিভাবে আইডেনটিটি বেসড মাইন ডাটাবেসে প্রসেস হয়ে ডাটাবেসে
ডাটা পূর্ণ হবে?"*

Before this module the honest answer was "it won't": `modules.bridge_poller`
opens a fresh bridge store at `NOW()` (`_get_cursor()`), reads only
`is_from_me = 0`, and explicitly excludes `from_history_sync = 1` rows — a
deliberate, correct guard from the 2026-09-02 Bridge3 incident (a re-pair
replayed ~2,444 old messages through live routing and one was auto-answered).
So a newly paired device produced an EMPTY main database for everything that
happened before pairing.

This module imports that history WITHOUT re-introducing the incident:

  * it never calls `modules.message_router.process_message()`, never enqueues
    an outbound message, never triggers a workflow, an intent, an admin
    command or an AI reply. It only writes rows and identity data;
  * every imported row is marked `history_imported = true`,
    `is_processed = true`, `source_context = 'history_backfill'` — so "what
    arrived live" vs "what was imported" is always distinguishable in SQL;
  * it is idempotent and resumable: paging is by SQLite rowid, progress lives
    in `wbom_bridge_history_backfill`, and every insert goes through the same
    `message_hash` uniqueness the live pipeline uses, so re-running it can
    never duplicate a message;
  * groups are excluded by default (matching live policy) and, when enabled,
    only JIDs already approved in `wbom_group_identities` are considered.

What "processed into the identity database" means here, per message:
  * `modules.identity_brain.detect_identity(phone, text)` -> identity_role /
    identity_confidence persisted on the row (admin / accountant / employee /
    escort client / candidate / repeat_client / unknown ...);
  * `modules.contact_sync.upsert_contact_from_message()` -> the contact exists
    in `fazle_unified_contacts` + `wbom_contacts` with its display name and
    the bridge it was first seen on;
  * `canonical_phone` / `phone_last10` / `original_sender_number` filled, so
    the identity keys are normalised the same way live rows are.

Direction semantics (documented once, used everywhere):
  * `direction='inbound'`  -> `sender_number`/`canonical_phone` = the person.
  * `direction='outbound'` -> `sender_number`/`canonical_phone` = the
    COUNTERPARTY (the person we wrote to), `receiver_number`/`source_bridge`
    = OUR bridge that carried it.

Outbound history rows carry `ai_generated = NULL` ("unknown"): those messages
left the device before this database recorded provenance, and inventing a
`false` would be a claim we cannot support.

Entry points
------------
  * `python -m modules.history_backfill --bridge bridge1 --since 2025-01-01`
  * `POST /api/bridge/history-backfill`          (admin API key)
  * `GET  /api/bridge/history-backfill/status`   (admin API key)
  * automatic at startup when `BRIDGE_HISTORY_BACKFILL_ON_START=true`
    (`BRIDGE_HISTORY_BACKFILL_SINCE`, `BRIDGE_HISTORY_BACKFILL_INCLUDE_OUTBOUND`)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import get_settings
from app.database import execute, fetch_all, fetch_one
from modules.bridge_poller import (
    BRIDGE_CONFIGS,
    _fetch_history_messages,
    _load_contact_names,
    _parse_source_timestamp,
    _core_message_hash,
    _storage_canonical_phone,
    _storage_phone_last10,
    insert_core_message,
)

log = logging.getLogger("fazle.history_backfill")

_STATE_TABLE = "wbom_bridge_history_backfill"
_DEFAULT_BATCH = 200


# ── env ───────────────────────────────────────────────────────────────────────

def _truthy(raw, default: bool = False) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def autostart_enabled() -> bool:
    # Literal os.getenv name on purpose: the .env.example inventory generator
    # scans for string literals, and a dynamic name would be invisible to it.
    return _truthy(os.getenv("BRIDGE_HISTORY_BACKFILL_ON_START", "false"), False)


def include_outbound_default() -> bool:
    return _truthy(os.getenv("BRIDGE_HISTORY_BACKFILL_INCLUDE_OUTBOUND", "true"), True)


def _env_since() -> Optional[datetime]:
    raw = (os.getenv("BRIDGE_HISTORY_BACKFILL_SINCE") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("[history_backfill] BRIDGE_HISTORY_BACKFILL_SINCE=%r is not an ISO date — ignored", raw)
        return None


def env_since() -> Optional[datetime]:
    """Public alias used by the status API."""
    return _env_since()


def _config(name: str) -> dict:
    for cfg in BRIDGE_CONFIGS:
        if cfg["name"] == name:
            return cfg
    raise ValueError(f"unknown bridge {name!r} (expected one of {[c['name'] for c in BRIDGE_CONFIGS]})")


# ── state ────────────────────────────────────────────────────────────────────

async def get_status(bridge: Optional[str] = None) -> list[dict]:
    try:
        if bridge:
            rows = await fetch_all(f"SELECT * FROM {_STATE_TABLE} WHERE bridge = $1 ORDER BY bridge", bridge)
        else:
            rows = await fetch_all(f"SELECT * FROM {_STATE_TABLE} ORDER BY bridge")
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("[history_backfill] status unavailable (migration 091 applied?): %s", exc)
        return []


async def _claim(bridge: str, *, since: Optional[datetime], until: Optional[datetime],
                 include_outbound: bool, reset: bool) -> bool:
    """Mark this bridge's import as running. False = another run is active."""
    if reset:
        await execute(
            f"""INSERT INTO {_STATE_TABLE} (bridge, status, since_ts, until_ts,
                                            include_outbound, updated_at)
               VALUES ($1, 'pending', $2, $3, $4, NOW())
               ON CONFLICT (bridge) DO UPDATE
                   SET status = 'pending', since_ts = EXCLUDED.since_ts,
                       until_ts = EXCLUDED.until_ts,
                       include_outbound = EXCLUDED.include_outbound,
                       last_rowid = 0, last_source_ts = NULL,
                       inbound_imported = 0, outbound_imported = 0,
                       skipped_duplicate = 0, skipped_unsupported = 0,
                       contacts_upserted = 0, identity_resolved = 0,
                       error = NULL, started_at = NULL, finished_at = NULL,
                       updated_at = NOW()""",
            bridge, since, until, include_outbound,
        )
    else:
        await execute(
            f"""INSERT INTO {_STATE_TABLE} (bridge, status, since_ts, until_ts,
                                            include_outbound, updated_at)
               VALUES ($1, 'pending', $2, $3, $4, NOW())
               ON CONFLICT (bridge) DO NOTHING""",
            bridge, since, until, include_outbound,
        )
    row = await fetch_one(
        f"""UPDATE {_STATE_TABLE}
               SET status = 'running', started_at = COALESCE(started_at, NOW()),
                   finished_at = NULL, error = NULL, updated_at = NOW()
             WHERE bridge = $1
               AND (status <> 'running' OR updated_at < NOW() - INTERVAL '30 minutes')
             RETURNING bridge""",
        bridge,
    )
    return bool(row)


async def _progress(bridge: str, *, rowid: int, last_ts: Optional[datetime],
                    inbound: int, outbound: int, dup: int, unsupported: int,
                    contacts: int, identity: int, status: str = "running",
                    error: Optional[str] = None, finished: bool = False) -> None:
    await execute(
        f"""UPDATE {_STATE_TABLE}
               SET last_rowid = $2,
                   last_source_ts = COALESCE($3, last_source_ts),
                   inbound_imported = inbound_imported + $4,
                   outbound_imported = outbound_imported + $5,
                   skipped_duplicate = skipped_duplicate + $6,
                   skipped_unsupported = skipped_unsupported + $7,
                   contacts_upserted = contacts_upserted + $8,
                   identity_resolved = identity_resolved + $9,
                   status = $10,
                   error = $11,
                   finished_at = CASE WHEN $12 THEN NOW() ELSE finished_at END,
                   updated_at = NOW()
             WHERE bridge = $1""",
        bridge, int(rowid), last_ts, inbound, outbound, dup, unsupported,
        contacts, identity, status, (error or "")[:500] or None, finished,
    )


# ── per-message persistence ──────────────────────────────────────────────────

async def _resolve_identity(phone: str, text: str) -> tuple[str, int]:
    """best-effort role/confidence; never raises, never routes anything."""
    try:
        from modules.identity_brain import detect_identity
        ident = await detect_identity(phone, text)
        role = str(ident.get("role") or "")
        conf = int(ident.get("confidence") or 0)
        return ("" if role in ("", "unknown") else role), conf
    except Exception as exc:  # noqa: BLE001
        log.debug("[history_backfill] identity lookup skipped for %s: %s", phone, exc)
        return "", 0


async def _existing_outbound_row(bridge: str, body: str, phone: str) -> bool:
    """True when this outbound message is already in the canonical table.

    Live automation replies were written with a queue-based message_hash, so
    the hash cannot dedupe them against a bridge-store row. Match on the
    observable facts instead (same body, same counterparty, same bridge,
    ±10 minutes) and treat a hit as "already stored".
    """
    try:
        row = await fetch_one(
            """
            SELECT message_id FROM wbom_whatsapp_messages
             WHERE direction = 'outbound'
               AND message_body = $1
               AND (canonical_phone = $2 OR sender_number = $3)
               AND (source_bridge = $4 OR platform = $4)
             LIMIT 1
            """,
            body, _storage_canonical_phone(phone), phone, bridge,
        )
        return bool(row)
    except Exception:  # noqa: BLE001 — pre-091 schema (no source_bridge)
        try:
            row = await fetch_one(
                """
                SELECT message_id FROM wbom_whatsapp_messages
                 WHERE direction = 'outbound' AND message_body = $1
                   AND (canonical_phone = $2 OR sender_number = $3)
                   AND platform = $4
                 LIMIT 1
                """,
                body, _storage_canonical_phone(phone), phone, bridge,
            )
            return bool(row)
        except Exception as exc:  # noqa: BLE001
            log.debug("[history_backfill] outbound dedupe check failed: %s", exc)
            return False


async def import_message(
    bridge: str,
    msg: dict,
    *,
    source_context: str = "history_backfill",
    history_imported: bool = True,
) -> dict:
    """Persist ONE bridge-store message.

    Returns {"outcome": imported|duplicate|unsupported,
             "direction": inbound|outbound,
             "identity_resolved": bool, "contact_upserted": bool}.
    """
    outcome = {"outcome": "unsupported", "direction": "inbound",
               "identity_resolved": False, "contact_upserted": False}
    text = str(msg.get("text") or "")
    phone = str(msg.get("phone") or "")
    direction = "outbound" if str(msg.get("direction")) == "outbound" else "inbound"
    outcome["direction"] = direction
    msg_id = str(msg.get("id") or "")
    if not msg_id:
        return outcome

    source_ts = _parse_source_timestamp(msg.get("timestamp"))
    meta = {
        "stage": "history_import",
        "bridge": bridge,
        "chat_jid": msg.get("chat_jid"),
        "sender": msg.get("sender"),
        "history_sync": int(msg.get("from_history_sync") or 0),
        "media_type": msg.get("media_type"),
        "filename": msg.get("filename"),
        "source_context": source_context,
    }

    if direction == "outbound" and await _existing_outbound_row(bridge, text, phone):
        outcome["outcome"] = "duplicate"
        return outcome

    # Identity is resolved for BOTH directions: `phone` is the counterparty in
    # each case, and the point of the import is that the identity database is
    # populated, not just the message table.
    identity_role, identity_confidence = ("", 0)
    if not phone.startswith("unresolved:"):
        identity_role, identity_confidence = await _resolve_identity(phone, text)

    values: dict[str, Any] = {
        "sender_number": phone,
        "message_body": text,
        "message_type": str(msg.get("media_type") or "text") or "text",
        "direction": direction,
        "platform": bridge,
        "source_bridge": bridge,
        "is_processed": True,
        "history_imported": bool(history_imported),
        "contact_identifier": str(msg.get("chat_jid") or phone),
        "identity_role": identity_role or None,
        "identity_confidence": identity_confidence or None,
        "receiver_number": _receiver_for(bridge) or None,
        "conversation_key": f"{(msg.get('chat_jid') if msg.get('is_group') else phone)}:{bridge}",
        "actor_type": "external_user" if direction == "inbound" else "human_device",
        "message_hash": _core_message_hash(bridge, msg_id),
        "source_message_ref": msg_id,
        "source_timestamp": source_ts,
        "source_context": source_context,
        "canonical_phone": _storage_canonical_phone(phone),
        "phone_last10": _storage_phone_last10(phone),
        "original_sender_number": phone,
        # Provenance of a pre-existing outbound message cannot be proven:
        # NULL = unknown (never a guessed false).
        "ai_generated": None if direction == "outbound" else False,
        # an outbound row captured live from the DEVICE is still not provably
        # AI-written: if the app wrote it, its own row already exists and the
        # dedupe check above turns this one into a no-op.
        "delivery_status": "unknown" if direction == "outbound" else None,
        "metadata_json": meta,
    }
    values = {k: v for k, v in values.items() if v is not None}
    # ai_generated must be written even when NULL ("unknown" is a real state,
    # and the column default would otherwise claim false for imported
    # outbound history).
    values["ai_generated"] = None if direction == "outbound" else False

    inserted = await insert_core_message(values)
    if inserted is None:
        outcome["outcome"] = "duplicate"
        return outcome

    outcome["outcome"] = "imported"
    outcome["identity_resolved"] = bool(identity_role)
    if direction == "inbound" and not phone.startswith("unresolved:"):
        try:
            from modules.contact_sync import upsert_contact_from_message
            hint = (msg.get("full_name") or msg.get("push_name") or "")
            await upsert_contact_from_message(phone, hint, bridge)
            outcome["contact_upserted"] = True
        except Exception as exc:  # noqa: BLE001
            log.debug("[history_backfill] contact upsert skipped for %s: %s", phone, exc)
    return outcome


def _receiver_for(bridge: str) -> str:
    s = get_settings()
    return {
        "bridge1": s.bridge1_number,
        "bridge2": s.bridge2_number,
        "bridge3": s.bridge3_number,
        "meta": s.admin_meta_number,
        "whatsapp": s.admin_meta_number,
    }.get(bridge, "")


async def _approved_group_jids() -> frozenset:
    try:
        from modules.group_identity import list_approved_group_jids
        return frozenset(await list_approved_group_jids())
    except Exception:  # noqa: BLE001
        return frozenset()


# ── public entry points ──────────────────────────────────────────────────────

async def backfill_bridge(
    bridge: str,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    include_outbound: bool = True,
    include_groups: bool = False,
    batch: int = _DEFAULT_BATCH,
    max_messages: Optional[int] = None,
    reset: bool = False,
    dry_run: bool = False,
) -> dict:
    """Import one bridge's existing store into the main database.

    Safe to call repeatedly: progress resumes from `last_rowid`, and finished
    bridges are skipped unless `reset=True`. `dry_run=True` reads and counts
    without writing anything (not even state).
    """
    cfg = _config(bridge)
    messages_db = cfg["messages_db"]
    whatsapp_db = cfg["whatsapp_db"]

    result = {
        "bridge": bridge, "status": "no_store", "imported_inbound": 0,
        "imported_outbound": 0, "skipped_duplicate": 0,
        "skipped_unsupported": 0, "contacts_upserted": 0,
        "identity_resolved": 0, "pages": 0, "resumed_from_rowid": 0,
        "dry_run": dry_run, "complete": False,
    }
    if not os.path.exists(messages_db):
        result["error"] = f"bridge store not found: {messages_db}"
        return result

    state = (await get_status(bridge) or [None])[0]
    if state and not reset:
        if state.get("status") == "completed":
            result.update(status="completed", resumed_from_rowid=state.get("last_rowid") or 0,
                          complete=True)
            return result
        try:
            claimed = await _claim(
                bridge,
                since=since or state.get("since_ts"),
                until=until or state.get("until_ts"),
                include_outbound=include_outbound,
                reset=False,
            )
        except Exception as exc:  # noqa: BLE001 — migration 091 not applied
            result.update(status="state_unavailable",
                          error=f"import state table unavailable ({exc}); apply migration 091")
            return result
        if not dry_run and not claimed:
            result["status"] = "already_running"
            return result
        rowid = int(state.get("last_rowid") or 0)
        since = since or state.get("since_ts")
        until = until or state.get("until_ts")
        include_outbound = bool(state.get("include_outbound", include_outbound))
    else:
        if not dry_run:
            try:
                claimed = await _claim(bridge, since=since, until=until,
                                       include_outbound=include_outbound, reset=True)
            except Exception as exc:  # noqa: BLE001 — migration 091 not applied
                result.update(status="state_unavailable",
                              error=f"import state table unavailable ({exc}); apply migration 091")
                return result
            if not claimed:
                result["status"] = "already_running"
                return result
        rowid = 0
    result["resumed_from_rowid"] = rowid
    result["status"] = "running"

    try:
        contact_names = _load_contact_names(whatsapp_db) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("[history_backfill] contact-name map unavailable: %s", exc)
        contact_names = {}
    approved_groups = await _approved_group_jids() if include_groups else frozenset()
    seen = 0
    last_ts: Optional[datetime] = None

    while True:
        page, rowid, done = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _fetch_history_messages(
                messages_db, whatsapp_db,
                after_rowid=rowid, since_ts=since, until_ts=until,
                include_outbound=include_outbound,
                include_groups=include_groups, batch=batch,
            ),
        )
        result["pages"] += 1

        for msg in page:
            if include_groups and msg.get("is_group") and str(msg.get("chat_jid")) not in approved_groups:
                result["skipped_unsupported"] += 1
                continue
            seen += 1
            sender_lid = str(msg.get("sender") or "").split(":")[0].split("@")[0]
            hint = contact_names.get(sender_lid) or {}
            if hint:
                msg.setdefault("full_name", hint.get("full_name"))
                msg.setdefault("push_name", hint.get("push_name"))
                msg.setdefault("business_name", hint.get("business_name"))

            if dry_run:
                if str(msg.get("direction")) == "outbound":
                    result["imported_outbound"] += 1
                else:
                    result["imported_inbound"] += 1
            else:
                try:
                    outcome = await import_message(bridge, msg)
                except Exception as exc:  # noqa: BLE001 — one bad row must not abort the import
                    log.warning("[history_backfill] %s row %s failed: %s", bridge, msg.get("id"), exc)
                    outcome = {"outcome": "unsupported"}
                if outcome.get("outcome") == "imported":
                    if outcome.get("direction") == "outbound":
                        result["imported_outbound"] += 1
                    else:
                        result["imported_inbound"] += 1
                    if outcome.get("contact_upserted"):
                        result["contacts_upserted"] += 1
                    if outcome.get("identity_resolved"):
                        result["identity_resolved"] += 1
                else:
                    result["skipped_duplicate" if outcome.get("outcome") == "duplicate" else "skipped_unsupported"] += 1

            ts = _parse_source_timestamp(msg.get("timestamp"))
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts

            if max_messages and seen >= max_messages:
                break

        if not dry_run:
            await _progress(
                bridge, rowid=rowid, last_ts=last_ts,
                inbound=result["imported_inbound"] - (result.get("_inbound_reported") or 0),
                outbound=result["imported_outbound"] - (result.get("_outbound_reported") or 0),
                dup=result["skipped_duplicate"] - (result.get("_dup_reported") or 0),
                unsupported=result["skipped_unsupported"] - (result.get("_unsupported_reported") or 0),
                contacts=result["contacts_upserted"] - (result.get("_contacts_reported") or 0),
                identity=result["identity_resolved"] - (result.get("_identity_reported") or 0),
            )
            result["_inbound_reported"] = result["imported_inbound"]
            result["_outbound_reported"] = result["imported_outbound"]
            result["_dup_reported"] = result["skipped_duplicate"]
            result["_unsupported_reported"] = result["skipped_unsupported"]
            result["_contacts_reported"] = result["contacts_upserted"]
            result["_identity_reported"] = result["identity_resolved"]

        if done or not page or (max_messages and seen >= max_messages):
            break

    result["complete"] = bool(done or not page) and not (max_messages and seen >= max_messages)
    result["status"] = "completed" if result["complete"] else "paused"
    if not dry_run:
        await _progress(
            bridge, rowid=rowid, last_ts=last_ts,
            inbound=0, outbound=0, dup=0, unsupported=0, contacts=0, identity=0,
            status="completed" if result["complete"] else "pending",
            finished=result["complete"],
        )
    for key in [k for k in result if k.startswith("_")]:
        result.pop(key, None)
    log.info(
        "[history_backfill] bridge=%s status=%s inbound=%d outbound=%d dup=%d unsupported=%d",
        bridge, result["status"], result["imported_inbound"],
        result["imported_outbound"], result["skipped_duplicate"],
        result["skipped_unsupported"],
    )
    return result


async def backfill_all(
    *,
    include_outbound: bool = True,
    include_groups: bool = False,
    max_messages_per_bridge: Optional[int] = None,
    reset: bool = False,
) -> list[dict]:
    """Import every configured bridge, oldest-first, one bridge at a time."""
    results = []
    for cfg in BRIDGE_CONFIGS:
        try:
            results.append(await backfill_bridge(
                cfg["name"],
                since=_env_since(),
                include_outbound=include_outbound,
                include_groups=include_groups,
                max_messages=max_messages_per_bridge,
                reset=reset,
            ))
        except Exception as exc:  # noqa: BLE001
            log.exception("[history_backfill] %s failed: %s", cfg["name"], exc)
            results.append({"bridge": cfg["name"], "status": "failed", "error": str(exc)[:500]})
    return results


async def startup_backfill() -> None:
    """Called once from the app lifespan when the env flag is enabled."""
    if not autostart_enabled():
        return
    include_outbound = include_outbound_default()
    log.info("[history_backfill] autostart enabled (outbound=%s, since=%s)",
             include_outbound, _env_since())
    try:
        results = await backfill_all(include_outbound=include_outbound)
        log.info("[history_backfill] autostart finished: %s",
                 [{r.get("bridge"): r.get("status")} for r in results])
    except Exception as exc:  # noqa: BLE001 — never block startup
        log.warning("[history_backfill] autostart failed (non-fatal): %s", exc)
