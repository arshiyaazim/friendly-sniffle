"""
Fazle Core — Shared Canonical Field Alias Registry
====================================================

PURPOSE
-------
Different modules, frontends and WhatsApp messages use different names
for the same concept. This registry maps all known aliases to one
canonical field name so modules can understand each other without
any DB column renames or schema changes.

USAGE
-----
    from shared.aliases import normalize_field, resolve_value, canonical_name

    # Find canonical name for an incoming key:
    normalize_field("emp_name")          # → "employee_name"
    normalize_field("primary_phone")     # → "employee_mobile"

    # Extract a value from a dict that may use any alias:
    resolve_value(row, "employee_name")  # → row["full_name"] or row["name"] etc.

    # Check canonical name for display:
    canonical_name("bkash_number")       # → "payment_phone"

RULES
-----
* Do NOT rename DB columns.
* Do NOT use this to bypass existing schema.
* This is a PURE PYTHON translation layer — no DB dependency.
* When in doubt, add an alias rather than changing existing code.
"""

from __future__ import annotations

from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Canonical field → list of known aliases
# Order within each alias list does NOT matter — all are equally valid.
# ─────────────────────────────────────────────────────────────────────────────

FIELD_ALIASES: dict[str, list[str]] = {

    # ── Employee Identity ─────────────────────────────────────────────────────
    "employee_name": [
        "name", "full_name", "emp_name", "escort_name",
        "contact_name", "extracted_name", "employee",
        "name_raw", "raw_name",
    ],
    "employee_mobile": [
        "mobile", "phone", "whatsapp", "escort_mobile",
        "contact_phone", "primary_phone", "payout_phone",
        "payment_mobile", "payment_number", "mobile_number",
        "sender_phone", "sender_number", "from_number",
    ],
    "employee_id": [
        "emp_id", "escort_employee_id", "fpe_employee_id",
        "matched_employee_id", "worker_id",
    ],
    "employee_code": [
        "emp_code", "code", "id_code", "staff_code",
    ],
    "department": [
        "dept", "section", "unit",
    ],

    # ── Vessel & Escort ───────────────────────────────────────────────────────
    "mother_vessel": [
        "mv", "m.v.", "এমভি", "vessel", "ship", "mother_ship",
    ],
    "lighter_vessel": [
        "lighter", "lt", "lighter_name", "boat",
    ],
    "master_mobile": [
        "master_phone", "master_number", "master", "capt_mobile",
    ],
    "destination": [
        "release_point", "release_location", "dest", "port",
        "anchorage", "landing_point",
    ],
    "berth": [
        "berth_number", "berth_no", "berth_num", "spot",
    ],

    # ── Dates & Shifts ────────────────────────────────────────────────────────
    "start_date": [
        "program_date", "assignment_date", "date_from",
        "board_date", "joining_date",
    ],
    "end_date": [
        "completion_date", "release_date", "date_to",
        "off_date", "finished_date",
    ],
    "start_shift": [
        "shift", "duty_shift", "opening_shift", "first_shift",
    ],
    "end_shift": [
        "closing_shift", "last_shift", "release_shift",
    ],

    # ── Finance ───────────────────────────────────────────────────────────────
    "amount": [
        "total", "total_payment", "salary", "net_salary",
        "approved_amount", "paid_amount", "txn_amount",
        "payment_amount",
    ],
    "payment_method": [
        "method", "payout_method", "pay_via", "channel",
        "txn_method",
    ],
    "payment_phone": [
        "bkash_number", "nagad_number", "payout_phone",
        "payment_number", "pay_phone",
    ],
    "accounting_period": [
        "period", "month", "pay_month", "salary_month",
        "txn_month", "payroll_period",
    ],
    "conveyance": [
        "conveyance_amount", "travel_allowance", "ta",
        "transport_allowance",
    ],

    # ── Message / Contact ─────────────────────────────────────────────────────
    "message_text": [
        "message_body", "raw_content", "text", "reply_text",
        "content", "body", "msg_text",
    ],
    "platform": [
        "source", "source_bridge", "bridge", "channel",
        "whatsapp_source",
    ],
    "message_direction": [
        "direction", "is_from_me", "msg_direction",
    ],
    "message_timestamp": [
        "timestamp", "timestamp_wa", "sent_at", "received_at",
        "created_at", "event_ts",
    ],

    # ── Recruitment ───────────────────────────────────────────────────────────
    "recruitment_step": [
        "step", "collection_step", "current_step", "stage_step",
    ],
    "recruitment_status": [
        "status", "funnel_stage", "pipeline_stage",
        "application_status",
    ],
    "applicant_area": [
        "area", "home_area", "location", "address",
        "উপজেলা", "থানা",
    ],
    "job_preference": [
        "job_type", "preferred_job", "job_interest",
        "role_interest",
    ],

    # ── Escort Program Status ─────────────────────────────────────────────────
    "escort_status": [
        "status", "program_status", "roster_status",
        "assignment_status",
    ],
    "roster_pay_status": [
        "pay_status", "payment_status", "salary_status",
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# Build reverse lookup once at import time
# ─────────────────────────────────────────────────────────────────────────────
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in FIELD_ALIASES.items():
    _ALIAS_TO_CANONICAL[_canon] = _canon        # canonical maps to itself
    for _alias in _aliases:
        # First registration wins — avoids overwriting on alias collisions.
        _ALIAS_TO_CANONICAL.setdefault(_alias, _canon)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def normalize_field(raw_key: str) -> str:
    """
    Return the canonical field name for any known alias.
    Returns the original key unchanged if not found.

    Examples
    --------
    >>> normalize_field("full_name")
    'employee_name'
    >>> normalize_field("bkash_number")
    'payment_phone'
    >>> normalize_field("totally_unknown_field")
    'totally_unknown_field'
    """
    return _ALIAS_TO_CANONICAL.get(raw_key.lower().strip(), raw_key)


def canonical_name(raw_key: str) -> str:
    """Alias for normalize_field — more readable in display contexts."""
    return normalize_field(raw_key)


def resolve_value(data: dict, canonical_key: str, default: Any = None) -> Any:
    """
    Extract a value from a dict that may use any alias for the canonical key.

    Lookup order:
      1. Exact canonical key
      2. All known aliases for this canonical key (in list order)
      3. Returns `default` if none found

    Examples
    --------
    >>> resolve_value({"full_name": "Karim"}, "employee_name")
    'Karim'
    >>> resolve_value({"bkash_number": "01712345678"}, "payment_phone")
    '01712345678'
    >>> resolve_value({}, "employee_name", default="Unknown")
    'Unknown'
    """
    # Try canonical key first (fastest path)
    if canonical_key in data:
        return data[canonical_key]
    # Try all known aliases
    for alias in FIELD_ALIASES.get(canonical_key, []):
        if alias in data:
            return data[alias]
    return default


def normalize_dict(data: dict) -> dict:
    """
    Return a new dict with all keys converted to their canonical names.
    Non-aliased keys are kept as-is. On alias collision the first key wins.

    Useful for normalizing parsed WhatsApp message payloads before storage.

    Example
    -------
    >>> normalize_dict({"full_name": "Karim", "bkash_number": "017..."})
    {'employee_name': 'Karim', 'payment_phone': '017...'}
    """
    out: dict = {}
    for k, v in data.items():
        canon = normalize_field(k)
        out.setdefault(canon, v)
    return out


def all_aliases(canonical_key: str) -> list[str]:
    """
    Return all known names (canonical + aliases) for a given canonical key.
    Returns [canonical_key] if not in registry.

    Useful for building SQL WHERE clauses that tolerate schema variation.
    """
    return [canonical_key] + list(FIELD_ALIASES.get(canonical_key, []))
