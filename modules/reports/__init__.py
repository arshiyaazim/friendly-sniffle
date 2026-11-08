"""modules.reports — the read-only reporting layer.

RESTORED 2026-11-06
-------------------
This module has always been imported by ``app/main.py`` (``/reports``),
``modules/admin_commands`` (``report …`` + ``STATUS``) and
``modules/scheduler`` (three daily/twice-daily jobs), but it had **never been
committed to git**: the repository's ``.gitignore`` carried an unanchored
``reports/`` rule (meant for the generated ``<repo>/reports`` output folder)
which also swallowed this source package. A clean checkout therefore died on
``from modules import reports`` at import time — the application could not
start at all.

This file reconstructs the module against the only two trustworthy specs that
survived the loss: the call sites above, and ``tests/unit/
test_reports_stage_g18_g21.py`` / ``tests/unit/test_fpe_employee_resolution_status.py``.

Design rules (all pre-existing, restated so they survive the restoration):

* **Read-only.** No builder writes business data. ``_b_system_observation`` is
  even asserted by test to contain no write statement at all — monitoring
  reports findings, it never fixes them.
* **No raw message bodies.** Reports carry counts, ids and references, never
  ``wbom_whatsapp_messages.message_body`` content, because reports travel over
  WhatsApp to the owner's phone.
* **Bounded output.** Every list is ``LIMIT``-ed; ``render_text`` truncates to
  ``max_rows`` with an explicit "… and N more" line.
* **Fail soft.** Each builder is wrapped by ``run_report``; a failing report
  returns an error payload and is recorded in ``fazle_report_runs`` rather than
  taking the caller down.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.database import execute, fetch_all, fetch_one, fetch_val

log = logging.getLogger("fazle.reports")

# ── Cache ─────────────────────────────────────────────────────────────────────
# Short-lived in-process cache only. Reports are cheap but the scheduler and an
# admin can ask for the same one within the same minute; never persisted.
_CACHE_TTL_S = 120
_CACHE: dict[str, tuple[float, dict]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _jsonable(rows: list[dict]) -> list[dict]:
    return [{k: _iso(v) for k, v in dict(r).items()} for r in rows]


# ── Individual builders ───────────────────────────────────────────────────────

async def _b_daily_summary(**kwargs) -> dict:
    """One day of traffic + queue pressure, keyed by bridge and direction."""
    day_raw = kwargs.get("date")
    try:
        day = date.fromisoformat(str(day_raw)) if day_raw else _now().date()
    except ValueError:
        day = _now().date()

    rows = await fetch_all(
        """
        SELECT platform,
               direction,
               COUNT(*)                                        AS messages,
               COUNT(DISTINCT sender_number)                   AS distinct_senders
          FROM wbom_whatsapp_messages
         WHERE received_at >= $1::date
           AND received_at <  ($1::date + INTERVAL '1 day')
         GROUP BY platform, direction
         ORDER BY platform, direction
        """,
        day,
    )
    pending_drafts = await fetch_val(
        "SELECT COUNT(*) FROM fazle_draft_replies WHERE COALESCE(status,'pending') = 'pending'"
    ) or 0
    pending_payments = await fetch_val(
        "SELECT COUNT(*) FROM fazle_payment_drafts WHERE status = 'pending'"
    ) or 0
    cash_today = await fetch_val(
        """
        SELECT COALESCE(SUM(amount), 0)
          FROM fpe_cash_transactions
         WHERE txn_date = $1::date
           AND COALESCE(transaction_status, 'final') <> 'rejected'
           AND deleted_at IS NULL
        """,
        day,
    ) or 0
    cash_pending = await fetch_val(
        "SELECT COUNT(*) FROM fpe_cash_transactions "
        "WHERE transaction_status = 'pending' AND deleted_at IS NULL"
    ) or 0

    inbound = sum(int(r["messages"]) for r in rows if r.get("direction") == "inbound")
    outbound = sum(int(r["messages"]) for r in rows if r.get("direction") == "outbound")
    return {
        "report": "daily_summary",
        "summary": {
            "date": day.isoformat(),
            "inbound_messages": inbound,
            "outbound_messages": outbound,
            "pending_drafts": int(pending_drafts),
            "pending_payment_drafts": int(pending_payments),
            "cash_posted_today": float(cash_today),
            "cash_pending": int(cash_pending),
        },
        "rows": _jsonable(rows),
        "columns": ["platform", "direction", "messages", "distinct_senders"],
    }


async def _b_monthly_payroll(**kwargs) -> dict:
    """Per-employee payouts recorded in ``fpe_cash_transactions`` for a month."""
    today = _now().date()
    try:
        year = int(kwargs.get("year") or today.year)
        month = int(kwargs.get("month") or today.month)
        if not 1 <= month <= 12:
            raise ValueError
    except (TypeError, ValueError):
        year, month = today.year, today.month

    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)

    rows = await fetch_all(
        """
        SELECT COALESCE(employee_id, 0)              AS employee_id,
               COALESCE(NULLIF(employee_name_raw, ''), '(unnamed)') AS employee_name,
               COUNT(*)                              AS payouts,
               COALESCE(SUM(amount), 0)              AS total_amount
          FROM fpe_cash_transactions
         WHERE txn_date >= $1 AND txn_date < $2
           AND COALESCE(transaction_status, 'final') <> 'rejected'
           AND deleted_at IS NULL
         GROUP BY 1, 2
         ORDER BY total_amount DESC
         LIMIT 200
        """,
        first,
        nxt,
    )
    total = sum(float(r["total_amount"] or 0) for r in rows)
    return {
        "report": "monthly_payroll",
        "summary": {
            "period": f"{year:04d}-{month:02d}",
            "employees_paid": len(rows),
            "total_amount": round(total, 2),
        },
        "rows": _jsonable(rows),
        "columns": ["employee_id", "employee_name", "payouts", "total_amount"],
    }


async def _b_cash_position(**kwargs) -> dict:
    """Day-by-day cash movement for the last N days."""
    try:
        days = max(1, min(365, int(kwargs.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7

    rows = await fetch_all(
        """
        SELECT txn_date,
               COUNT(*)                     AS transactions,
               COALESCE(SUM(amount), 0)     AS total_amount
          FROM fpe_cash_transactions
         WHERE txn_date >= (CURRENT_DATE - $1::int)
           AND COALESCE(transaction_status, 'final') <> 'rejected'
           AND deleted_at IS NULL
         GROUP BY txn_date
         ORDER BY txn_date DESC
        """,
        days,
    )
    pending = await fetch_val(
        """
        SELECT COUNT(*) FROM fpe_cash_transactions
         WHERE transaction_status = 'pending' AND deleted_at IS NULL
        """
    ) or 0
    return {
        "report": "cash_position",
        "summary": {
            "window_days": days,
            "total_amount": round(sum(float(r["total_amount"] or 0) for r in rows), 2),
            "pending_transactions": int(pending),
        },
        "rows": _jsonable(rows),
        "columns": ["txn_date", "transactions", "total_amount"],
    }


async def _b_payment_reconciliation(**kwargs) -> dict:
    """Staging rows vs posted cash transactions over a window."""
    try:
        days = max(1, min(365, int(kwargs.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7

    rows = await fetch_all(
        """
        SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS n
          FROM wbom_staging_payments
         WHERE created_at >= NOW() - ($1::int * INTERVAL '1 day')
         GROUP BY 1
         ORDER BY n DESC
        """,
        days,
    )
    posted = await fetch_val(
        """
        SELECT COUNT(*) FROM fpe_cash_transactions
         WHERE created_at >= NOW() - ($1::int * INTERVAL '1 day')
        """,
        days,
    ) or 0
    pending = await fetch_val(
        "SELECT COUNT(*) FROM fazle_payment_drafts WHERE status = 'pending'"
    ) or 0
    return {
        "report": "payment_reconciliation",
        "summary": {
            "window_days": days,
            "staging_rows": sum(int(r["n"]) for r in rows),
            "cash_transactions_created": int(posted),
            "pending_payment_drafts": int(pending),
        },
        "rows": _jsonable(rows),
        "columns": ["status", "n"],
    }


async def _b_escort_utilization(**kwargs) -> dict:
    """Escort programs that fall inside a date range, grouped by status."""
    end = kwargs.get("end") or _now().date().isoformat()
    start = kwargs.get("start") or (_now().date() - timedelta(days=30)).isoformat()
    try:
        start_d = date.fromisoformat(str(start))
        end_d = date.fromisoformat(str(end))
    except ValueError:
        start_d = _now().date() - timedelta(days=30)
        end_d = _now().date()

    rows = await fetch_all(
        """
        SELECT COALESCE(status, 'unknown') AS status,
               COUNT(*)                    AS programs,
               COUNT(DISTINCT escort_employee_id) AS escorts
          FROM wbom_escort_programs
         WHERE COALESCE(program_date, start_date) BETWEEN $1::date AND $2::date
         GROUP BY 1
         ORDER BY programs DESC
        """,
        start_d,
        end_d,
    )
    return {
        "report": "escort_utilization",
        "summary": {
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "total_programs": sum(int(r["programs"]) for r in rows),
            "active_programs": sum(
                int(r["programs"]) for r in rows
                if str(r.get("status", "")).lower() in ("confirmed", "running", "active")
            ),
        },
        "rows": _jsonable(rows),
        "columns": ["status", "programs", "escorts"],
    }


# ── Business communication summary (Stage G18) ────────────────────────────────
# The approval queue is the point of this report: the owner reads it on a phone
# and replies with a one-word command, so every line must be self-contained and
# use the exact "<thing> <id> — <category> — APPROVE / EDIT / REJECT" shape.

# business_reference kinds modules.business_action_execution can actually
# re-validate before executing (Stage F2/H5.13). Anything else is flagged
# [LEGACY] -- presented for visibility, never as equally actionable.
_EXECUTABLE_REFERENCE_KINDS = frozenset({"cash_txn", "draft", "conversation", "escort_program"})


def _reference_is_executable(business_reference: Optional[str]) -> bool:
    if not business_reference or ":" not in str(business_reference):
        return False
    kind, _, ref_id = str(business_reference).partition(":")
    return bool(ref_id) and kind in _EXECUTABLE_REFERENCE_KINDS


async def _b_business_communication_summary(**kwargs) -> dict:
    """Bounded business-communication overview + the owner's approval queue.

    Deliberately carries counts and ids only: this report is delivered over
    WhatsApp, so no message body may ever appear in it (asserted by test).
    """
    try:
        hours = max(1, min(24 * 30, int(kwargs.get("hours") or 24)))
    except (TypeError, ValueError):
        hours = 24

    domain_counts = await fetch_all(
        """
        SELECT COALESCE(identity_role, 'unknown') AS category,
               COUNT(DISTINCT sender_number)      AS contacts
          FROM wbom_whatsapp_messages
         WHERE received_at >= NOW() - ($1::int * INTERVAL '1 hour')
           AND direction = 'inbound'
         GROUP BY 1
         ORDER BY contacts DESC
        """,
        hours,
    )
    pending_drafts = await fetch_all(
        """
        SELECT id, recipient, created_at
          FROM fazle_draft_replies
         WHERE COALESCE(status, 'pending') = 'pending'
         ORDER BY created_at
         LIMIT 50
        """
    )
    client_pending = await fetch_all(
        """
        SELECT d.id, d.recipient, d.created_at
          FROM fazle_draft_replies d
         WHERE COALESCE(d.status, 'pending') = 'pending'
           AND (COALESCE(d.intent, '') ILIKE '%client%'
                OR COALESCE(d.intent, '') ILIKE '%escort%')
         ORDER BY d.created_at
         LIMIT 25
        """
    )
    office_pending = await fetch_all(
        """
        SELECT d.id, d.recipient, d.created_at
          FROM fazle_draft_replies d
         WHERE COALESCE(d.status, 'pending') = 'pending'
           AND (COALESCE(d.intent, '') ILIKE '%office%'
                OR COALESCE(d.intent, '') ILIKE '%internal%'
                OR COALESCE(d.intent, '') ILIKE '%operation%')
         ORDER BY d.created_at
         LIMIT 25
        """
    )
    attendance_pending_drafts = await fetch_all(
        """
        SELECT d.id, d.recipient, d.created_at
          FROM fazle_draft_replies d
         WHERE COALESCE(d.status, 'pending') = 'pending'
           AND COALESCE(d.intent, '') ILIKE '%attendance%'
         ORDER BY d.created_at
         LIMIT 25
        """
    )
    pending_business_actions = await fetch_all(
        """
        SELECT id, domain, action_type, business_reference, created_at
          FROM hermes_action_approvals
         WHERE status = 'pending'
         ORDER BY created_at
         LIMIT 25
        """
    )

    complaints = await fetch_val(
        """
        SELECT COUNT(*) FROM wbom_whatsapp_messages
         WHERE received_at >= NOW() - ($1::int * INTERVAL '1 hour')
           AND COALESCE(intent_detected, '') ILIKE '%complaint%'
        """,
        hours,
    ) or 0
    office_group_messages = await fetch_val(
        """
        SELECT COUNT(*) FROM wbom_whatsapp_messages
         WHERE received_at >= NOW() - ($1::int * INTERVAL '1 hour')
           AND COALESCE((metadata_json->>'is_group')::boolean, false) = true
        """,
        hours,
    ) or 0
    replies_sent = await fetch_val(
        """
        SELECT COUNT(*) FROM wbom_whatsapp_messages
         WHERE received_at >= NOW() - ($1::int * INTERVAL '1 hour')
           AND direction = 'outbound'
        """,
        hours,
    ) or 0
    pending_drafts_total = await fetch_val(
        "SELECT COUNT(*) FROM fazle_draft_replies WHERE COALESCE(status,'pending') = 'pending'"
    ) or 0
    attendance_needs_action = await fetch_val(
        """
        SELECT COUNT(*) FROM wbom_attendance
         WHERE attendance_date = CURRENT_DATE
           AND COALESCE(status, '') NOT IN ('present', 'approved', 'done')
        """
    ) or 0
    cash_needs_action = await fetch_val(
        "SELECT COUNT(*) FROM fpe_cash_transactions "
        "WHERE transaction_status = 'pending' AND deleted_at IS NULL"
    ) or 0
    escort_needs_action = await fetch_val(
        """
        SELECT COUNT(*) FROM wbom_escort_programs
         WHERE COALESCE(status, '') IN ('pending', 'awaiting_confirmation')
        """
    ) or 0
    financial_ops_failed_recent = await fetch_val(
        """
        SELECT COUNT(*) FROM fazle_admin_audit
         WHERE created_at >= NOW() - ($1::int * INTERVAL '1 hour')
           AND command ILIKE 'financial%'
           AND allowed = false
        """,
        hours,
    ) or 0

    # Pending cash transactions that can no longer be tied to a person are the
    # single most misleading number in the whole overview, so they are listed
    # explicitly instead of being folded into cash_needs_action.
    cash_orphans: list[dict] = []
    try:
        from modules.fazle_payroll_engine import review_linkage_repair

        cash_orphans = await review_linkage_repair.classify_all_pending_cash_transactions()
    except Exception as exc:  # never let one helper break the report
        log.debug("[reports] cash linkage classification unavailable: %s", exc)

    by_category = {str(r.get("category") or "unknown"): int(r.get("contacts") or 0)
                   for r in domain_counts}

    approval_queue: list[str] = []
    for row in client_pending:
        approval_queue.append(
            f"Draft {row.get('id')} — Client — APPROVE / EDIT / REJECT"
        )
    for row in office_pending:
        approval_queue.append(
            f"Draft {row.get('id')} — Office/Internal — APPROVE / EDIT / REJECT"
        )
    for row in attendance_pending_drafts:
        approval_queue.append(
            f"Draft {row.get('id')} — Attendance — APPROVE / EDIT / REJECT"
        )

    legacy_proposals = 0
    for action in pending_business_actions:
        ref = action.get("business_reference")
        if _reference_is_executable(ref):
            approval_queue.append(
                f"Proposal {action.get('id')} — {action.get('domain') or 'business'} — "
                f"APPROVE / REJECT ({ref})"
            )
        else:
            legacy_proposals += 1
            approval_queue.append(
                f"Proposal {action.get('id')} — {action.get('domain') or 'business'} — "
                f"[LEGACY] not auto-executable ({ref})"
            )

    reviewable_cash = [
        row for row in cash_orphans
        if str(row.get("classification")) in ("VALID_PENDING_REVIEWABLE", "VALID_PENDING_DIRECT")
    ]
    for row in reviewable_cash[:10]:
        approval_queue.append(
            f"Cash txn {row.get('txn_id')} — {row.get('employee_name_raw') or 'unnamed'} — "
            f"৳{row.get('amount')} — {row.get('txn_ref')} — CASH APPROVE / REJECT"
        )
    orphaned_cash = len(cash_orphans) - len(reviewable_cash)

    return {
        "report": "business_communication_summary",
        "summary": {
            "window_hours": hours,
            "by_category": by_category,
            "complaints": int(complaints),
            "office_group_messages": int(office_group_messages),
            "replies_sent": int(replies_sent),
            "pending_drafts_total": int(pending_drafts_total),
            "client_pending_drafts": len(client_pending),
            "office_internal_pending_drafts": len(office_pending),
            "attendance_pending_drafts": len(attendance_pending_drafts),
            "pending_business_proposals": len(pending_business_actions),
            "pending_business_proposals_legacy_unexecutable": legacy_proposals,
            "attendance_needs_action": int(attendance_needs_action),
            "cash_needs_action": int(cash_needs_action),
            "cash_pending_reviewable": len(reviewable_cash),
            "cash_pending_orphaned": orphaned_cash,
            "escort_needs_action": int(escort_needs_action),
            "financial_ops_failed_recent": int(financial_ops_failed_recent),
            "approval_queue_count": len(approval_queue),
        },
        "approval_queue": approval_queue,
        "rows": _jsonable(pending_drafts),
        "columns": ["id", "recipient", "created_at"],
    }


# ── System observation (Stage G21) ───────────────────────────────────────────
# READ/REPORT ONLY by contract: the source of this function must contain no
# write statement and no call that could apply a fix. It surfaces evidence and
# leaves every action to the owner.
_STALE_JOB_AFTER_S = 24 * 60 * 60
_TRANSIENT_PROBES = frozenset({"cpu"})  # ambient load spikes are not incidents


async def _b_system_observation(**kwargs) -> dict:
    """Health, job freshness, incidents and financial anomalies -- read only."""
    job_rows = await fetch_all(
        """
        SELECT job_name,
               last_status,
               last_run_at,
               run_count,
               EXTRACT(EPOCH FROM (NOW() - last_run_at)) AS seconds_since_last_run
          FROM fazle_scheduled_jobs
         ORDER BY last_run_at ASC NULLS FIRST
         LIMIT 200
        """
    )
    incident_rows = await fetch_all(
        """
        SELECT severity, source, title, COUNT(*) AS n
          FROM agent.incidents
         WHERE resolved_at IS NULL
           AND title <> 'ollama_model_evicted'
         GROUP BY severity, source, title
         ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,
                  title
         LIMIT 50
        """
    )
    old_cash_rows = await fetch_all(
        """
        SELECT id, txn_ref, employee_name_raw, amount,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 AS age_days
          FROM fpe_cash_transactions
         WHERE transaction_status = 'pending'
           AND deleted_at IS NULL
           AND created_at < NOW() - INTERVAL '3 days'
         ORDER BY created_at
         LIMIT 25
        """
    )
    duplicate_cash_rows = await fetch_all(
        """
        SELECT employee_id, amount, txn_date, COUNT(*) AS n, array_agg(txn_ref) AS txn_refs
          FROM fpe_cash_transactions
         WHERE transaction_status = 'pending'
           AND deleted_at IS NULL
         GROUP BY employee_id, amount, txn_date
        HAVING COUNT(*) > 1
         ORDER BY COUNT(*) DESC
         LIMIT 25
        """
    )

    health: dict = {}
    try:
        from app.main import _build_health

        health = await _build_health()
    except Exception as exc:
        log.warning("[reports] health probe unavailable: %s", exc)
        health = {"status": "unknown", "probes": {}}

    registered_jobs: list[str] = []
    try:
        from modules.scheduler import list_job_names

        registered_jobs = list_job_names() or []
    except Exception as exc:
        log.debug("[reports] job registry unavailable: %s", exc)

    probes = health.get("probes") or {}
    degraded = [name for name, probe in probes.items()
                if str((probe or {}).get("status", "ok")).lower() not in ("ok", "healthy", "pass")]
    action_required_probes = [n for n in degraded if n not in _TRANSIENT_PROBES]

    stale_jobs: list[dict] = []
    obsolete_jobs: list[dict] = []
    for row in job_rows:
        try:
            age = float(row.get("seconds_since_last_run") or 0)
        except (TypeError, ValueError):
            age = 0.0
        item = {
            "job_name": row.get("job_name"),
            "last_status": row.get("last_status"),
            "last_run_at": _iso(row.get("last_run_at")),
            "run_count": row.get("run_count"),
            "seconds_since_last_run": age,
        }
        if registered_jobs and row.get("job_name") not in registered_jobs:
            obsolete_jobs.append(item)
        elif age > _STALE_JOB_AFTER_S:
            stale_jobs.append(item)

    critical_incidents = [r for r in incident_rows
                          if str(r.get("severity", "")).lower() == "critical"]

    financial_anomalies: list[dict] = []
    for row in old_cash_rows:
        financial_anomalies.append({
            "kind": "old_pending_cash_txn",
            "txn_ref": row.get("txn_ref"),
            "employee_name_raw": row.get("employee_name_raw"),
            "amount": row.get("amount"),
            "age_days": round(float(row.get("age_days") or 0), 1),
        })
    for row in duplicate_cash_rows:
        financial_anomalies.append({
            "kind": "duplicate_looking_pending_cash",
            "employee_id": row.get("employee_id"),
            "amount": row.get("amount"),
            "txn_date": _iso(row.get("txn_date")),
            "n": row.get("n"),
            "txn_refs": row.get("txn_refs") or [],
        })

    provenance = {"pending_total": 0, "reviewable_or_direct": 0, "alert": False}
    try:
        from modules.fazle_payroll_engine import review_linkage_repair

        classified = await review_linkage_repair.classify_all_pending_cash_transactions()
        reviewable = [r for r in classified
                      if str(r.get("classification")) in ("VALID_PENDING_REVIEWABLE", "VALID_PENDING_DIRECT")]
        provenance = {
            "pending_total": len(classified),
            "reviewable_or_direct": len(reviewable),
            "alert": bool(classified) and not reviewable,
        }
    except Exception as exc:
        log.debug("[reports] provenance classification unavailable: %s", exc)

    if critical_incidents:
        priority = "CRITICAL"
    elif action_required_probes or stale_jobs or provenance["alert"]:
        priority = "ACTION_REQUIRED"
    elif degraded:
        priority = "TRANSIENT"
    else:
        priority = "HEALTHY"

    return {
        "report": "system_observation",
        "summary": {
            "overall_status": health.get("status", "unknown"),
            "degraded_probe_count": len(degraded),
            "degraded_probes": degraded,
            "action_required_probes": action_required_probes,
            "stale_job_count": len(stale_jobs),
            "obsolete_job_count": len(obsolete_jobs),
            "open_incident_count": sum(int(r.get("n") or 0) for r in incident_rows),
            "critical_incident_count": sum(int(r.get("n") or 0) for r in critical_incidents),
            "financial_anomaly_count": len(financial_anomalies),
            "financial_provenance_health": provenance,
            "priority": priority,
        },
        "probes": probes,
        "degraded": degraded,
        "job_runs": job_rows,
        "stale_jobs": stale_jobs,
        "obsolete_jobs": obsolete_jobs,
        "incidents": _jsonable(incident_rows),
        "financial_anomalies": financial_anomalies,
        "rows": _jsonable(stale_jobs),
        "columns": ["job_name", "last_status", "last_run_at", "run_count", "seconds_since_last_run"],
    }


# ── FPE employee resolution health ───────────────────────────────────────────

async def _b_unresolved_fpe_employees(**kwargs) -> dict:
    """Buckets of payroll-engine employees whose identity is still unresolved."""
    rows = await fetch_all(
        """
        SELECT id,
               COALESCE(full_name, '')   AS full_name,
               COALESCE(primary_phone, '') AS primary_phone,
               COALESCE(resolution_status, 'unresolved') AS resolution_status,
               wbom_employee_id
          FROM fpe_employees
         WHERE COALESCE(resolution_status, 'unresolved') <> 'resolved'
           AND COALESCE(status, 'active') <> 'merged'
         ORDER BY id
         LIMIT 100
        """
    )
    unresolved_unlinked = sum(
        1 for r in rows
        if r.get("wbom_employee_id") is None
        and str(r.get("resolution_status")) == "unresolved"
    )
    linked_but_mislabeled = sum(
        1 for r in rows
        if r.get("wbom_employee_id") is not None
        and str(r.get("resolution_status")) != "resolved"
    )
    inactive = sum(1 for r in rows if str(r.get("resolution_status")) == "inactive")
    return {
        "report": "unresolved_fpe_employees",
        "summary": {
            "total_unresolved": len(rows),
            "unresolved_unlinked_count": unresolved_unlinked,
            "linked_but_mislabeled_count": linked_but_mislabeled,
            "inactive_count": inactive,
        },
        "rows": _jsonable(rows),
        "columns": ["id", "full_name", "primary_phone", "resolution_status", "wbom_employee_id"],
    }


# ── Registry ─────────────────────────────────────────────────────────────────

_BUILDERS: dict[str, Callable[..., Any]] = {
    "daily_summary": _b_daily_summary,
    "monthly_payroll": _b_monthly_payroll,
    "cash_position": _b_cash_position,
    "payment_reconciliation": _b_payment_reconciliation,
    "escort_utilization": _b_escort_utilization,
    "business_communication_summary": _b_business_communication_summary,
    "system_observation": _b_system_observation,
    "unresolved_fpe_employees": _b_unresolved_fpe_employees,
}


def list_reports() -> list[str]:
    """Names accepted by run_report()/GET /reports/{name}."""
    return sorted(_BUILDERS)


async def run_report(
    name: str,
    args: Optional[dict] = None,
    *,
    requested_by: str = "",
    use_cache: bool = True,
) -> dict:
    """Run one report and return its JSON payload.

    Raises ``KeyError`` for an unknown name (the API maps that to 404). Any
    other failure is recorded in ``fazle_report_runs`` and re-raised, so a
    broken report never looks like an empty one.
    """
    if name not in _BUILDERS:
        raise KeyError(f"unknown report {name!r}; available: {', '.join(list_reports())}")

    args = dict(args or {})
    cache_key = f"{name}|{json.dumps(args, sort_keys=True, default=str)}"
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
            return hit[1]

    started = time.perf_counter()
    status = "ok"
    error: Optional[str] = None
    payload: dict = {}
    try:
        payload = await _BUILDERS[name](**args)
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            await execute(
                """
                INSERT INTO fazle_report_runs
                    (report_name, args_json, requested_by, status, duration_ms, row_count, error)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
                """,
                name,
                json.dumps(args, default=str),
                (requested_by or "")[:80],
                status,
                duration_ms,
                len(payload.get("rows") or []),
                error,
            )
        except Exception as exc:  # audit trail must never break a report
            log.debug("[reports] run log skipped for %s: %s", name, exc)

    payload.setdefault("report", name)
    payload["args"] = args
    payload["generated_at"] = _now().isoformat()
    payload["generated_by"] = requested_by or "system"
    payload.setdefault("summary", {})
    payload.setdefault("rows", [])
    payload.setdefault("columns", list(payload["rows"][0].keys()) if payload["rows"] else [])

    if use_cache:
        _CACHE[cache_key] = (time.time(), payload)
        if len(_CACHE) > 64:  # keep the process bounded
            for key in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:32]:
                _CACHE.pop(key, None)
    return payload


# ── Rendering ────────────────────────────────────────────────────────────────

def render_csv(payload: dict) -> str:
    """CSV of ``payload['rows']``. Never includes message bodies (none are built)."""
    rows = payload.get("rows") or []
    columns = payload.get("columns") or (list(rows[0].keys()) if rows else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
    return buf.getvalue()


def _fmt_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "হ্যাঁ" if value else "না"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) if value else "—"
    return str(value)


def render_text(payload: dict, max_rows: int = 25) -> str:
    """Human/WhatsApp-readable rendering of a report payload."""
    name = payload.get("report") or "report"
    lines: list[str] = [f"📊 রিপোর্ট: {name}"]

    args = payload.get("args") or {}
    if args:
        lines.append("আর্গুমেন্ট: " + ", ".join(f"{k}={v}" for k, v in args.items()))
    if payload.get("generated_at"):
        lines.append(f"সময়: {str(payload['generated_at'])[:19].replace('T', ' ')} UTC")
    lines.append("")

    summary = payload.get("summary") or {}
    if summary:
        lines.append("— সারসংক্ষেপ —")
        for key, value in summary.items():
            lines.append(f"  {key}: {_fmt_value(value)}")
        lines.append("")

    anomalies = payload.get("financial_anomalies") or []
    if anomalies or summary.get("financial_anomaly_count"):
        lines.append("— Financial anomalies —")
        for item in anomalies[:max_rows]:
            lines.append(
                f"  • {item.get('kind')}: {item.get('txn_ref') or item.get('txn_refs') or ''}"
                f" (age {item.get('age_days', '—')}d)"
            )
        if not anomalies:
            lines.append("  （বিস্তারিত unavailable）")
        lines.append("")

    queue = payload.get("approval_queue") or []
    if queue or summary.get("approval_queue_count"):
        lines.append("— অনুমোদনের অপেক্ষায় —")
        for item in queue[:max_rows]:
            lines.append(f"  • {item}")
        if len(queue) > max_rows:
            lines.append(f"  … আরও {len(queue) - max_rows}টি")
        lines.append("")

    stale = payload.get("stale_jobs") or []
    if stale:
        lines.append("— পুরোনো হয়ে যাওয়া জব —")
        for item in stale[:max_rows]:
            lines.append(f"  • {item.get('job_name')} — শেষ রান: {item.get('last_run_at')}")
        lines.append("")

    rows = payload.get("rows") or []
    if rows:
        lines.append(f"— তথ্য (প্রথম {min(len(rows), max_rows)}টি) —")
        for row in rows[:max_rows]:
            lines.append("  " + " | ".join(f"{k}={_fmt_value(v)}" for k, v in row.items()))
        if len(rows) > max_rows:
            lines.append(f"  … আরও {len(rows) - max_rows}টি")
    elif not summary:
        lines.append("কোনো তথ্য নেই।")

    return "\n".join(lines)


# ── Owner overview (reused by the STATUS command) ────────────────────────────

async def build_owner_overview(hours: int = 24) -> dict:
    """'What needs my approval right now?' — built from the summary builder only."""
    report = await _b_business_communication_summary(hours=hours)
    summary = report.get("summary") or {}
    return {
        "window_hours": hours,
        "queue": report.get("approval_queue") or [],
        "pending_total": summary.get("approval_queue_count", 0),
        "drafts": summary.get("pending_drafts_total", 0),
        "client_drafts": summary.get("client_pending_drafts", 0),
        "office_drafts": summary.get("office_internal_pending_drafts", 0),
        "cash_pending": summary.get("cash_needs_action", 0),
        "attendance_pending": summary.get("attendance_needs_action", 0),
        "escort_pending": summary.get("escort_needs_action", 0),
        "generated_at": _now().isoformat(),
    }


def render_owner_overview(overview: dict) -> str:
    lines = [
        "📊 এখন যা আপনার অনুমোদন চায়",
        f"মোট অপেক্ষমাণ: {overview.get('pending_total', 0)}",
        f"ড্রাফট: {overview.get('drafts', 0)} "
        f"(ক্লায়েন্ট {overview.get('client_drafts', 0)} | "
        f"অফিস {overview.get('office_drafts', 0)})",
        f"ক্যাশ: {overview.get('cash_pending', 0)} | "
        f"উপস্থিতি: {overview.get('attendance_pending', 0)} | "
        f"এসকর্ট: {overview.get('escort_pending', 0)}",
    ]
    queue = overview.get("queue") or []
    if queue:
        lines.append("")
        lines.extend(f"  • {item}" for item in queue[:15])
        if len(queue) > 15:
            lines.append(f"  … আরও {len(queue) - 15}টি")
    else:
        lines.append("")
        lines.append("✅ এই মুহূর্তে কিছুই অপেক্ষমাণ নেই।")
    return "\n".join(lines)


# ── Scheduled jobs ───────────────────────────────────────────────────────────
# Jobs only REPORT. They build a payload, render it and hand it to the outbound
# queue (which keeps its own sending gates); they never mutate business data.

async def _deliver(title: str, text: str, purpose: str, idempotency_key: str) -> bool:
    """Queue one report message for the owner. Returns True when queued."""
    try:
        from modules.message_router import get_primary_admin
        from modules import outbound

        admin = get_primary_admin()
        if not admin:
            log.warning("[reports] %s: no admin number configured, not delivered", purpose)
            return False
        qid = await outbound.enqueue(
            admin,
            f"{title}\n\n{text}",
            source_bridge="bridge1",
            fallback_channel="bridge2",
            purpose=purpose,
            idempotency_key=idempotency_key,
        )
        return bool(qid)
    except Exception as exc:
        log.warning("[reports] %s delivery failed: %s", purpose, exc)
        return False


async def job_daily_admin_digest() -> dict:
    """Batch 17 — one daily digest of the previous day's operations."""
    payload = await run_report("daily_summary", {}, requested_by="scheduler",
                               use_cache=False)
    text = render_text(payload, max_rows=15)
    key = f"daily-admin-digest-{payload['summary'].get('date')}"
    delivered = await _deliver("🗓️ দৈনিক সারসংক্ষেপ", text, "daily-admin-digest", key)
    return {
        "status": "ok",
        "inbound": payload["summary"].get("inbound_messages", 0),
        "outbound": payload["summary"].get("outbound_messages", 0),
        "alerted": delivered,
    }


async def job_business_summary_report() -> dict:
    """Stage G18 — twice-daily business communication summary + approval queue."""
    payload = await run_report("business_communication_summary", {"hours": 12},
                               requested_by="scheduler", use_cache=False)
    summary = payload.get("summary") or {}
    text = render_text(payload, max_rows=30)
    key = f"business-summary-{_now().strftime('%Y%m%d-%H')}"
    delivered = await _deliver("📋 ব্যবসায়িক সারসংক্ষেপ", text, "business-summary-report", key)
    return {
        "status": "ok",
        "pending": summary.get("approval_queue_count", 0),
        "alerted": delivered,
    }


async def job_system_observation_report() -> dict:
    """Stage G21 — twice-daily health/job/anomaly observation, report only."""
    payload = await run_report("system_observation", {}, requested_by="scheduler",
                               use_cache=False)
    summary = payload.get("summary") or {}
    priority = summary.get("priority", "UNKNOWN")
    delivered = False
    # Only priority levels a human must look at are pushed; a HEALTHY run is
    # still recorded in fazle_report_runs (run_report does that) but does not
    # interrupt the owner.
    if priority in ("CRITICAL", "ACTION_REQUIRED"):
        text = render_text(payload, max_rows=30)
        key = f"system-observation-{_now().strftime('%Y%m%d-%H')}"
        delivered = await _deliver(f"🩺 সিস্টেম পর্যবেক্ষণ — {priority}", text,
                                   "system-observation-report", key)
    return {
        "status": "ok",
        "priority": priority,
        "anomalies": summary.get("financial_anomaly_count", 0),
        "alerted": delivered,
    }


__all__ = [
    "list_reports",
    "run_report",
    "render_csv",
    "render_text",
    "build_owner_overview",
    "render_owner_overview",
    "job_daily_admin_digest",
    "job_business_summary_report",
    "job_system_observation_report",
]
