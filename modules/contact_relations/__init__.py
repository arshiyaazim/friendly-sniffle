"""
Owner-managed contact relation registry.

The single place a non-coder admin can say *who a WhatsApp number is* — family,
friend, vendor, journalist, threat — and how the app should behave towards it
(tone, trust, auto-reply). Backed by ``fazle_contact_relations`` (migration 090).

Design rules
------------
1. **DB is the source of truth for relationships.** Nothing here is read from
   Python constants at runtime; the vocabulary below only defines what a user is
   *allowed* to choose, it never decides who someone is.
2. **The UI is the only input.** Every write goes through
   ``modules.contact_relations.routes`` (``/admin/contact-relations``) and is
   attributed in ``fazle_admin_audit``.
3. **This registry can never grant privileges.** ``admin``, ``superadmin``,
   ``accountant``, ``employee`` and ``supervisor`` are structurally excluded —
   they come from ``ADMIN_NUMBERS``/``ACCOUNTANT_PHONE`` (.env) or the
   employees table, never from a dropdown. ``PRIVILEGED_ROLES`` below is
   asserted against the vocabulary at import time, and ``identity_role_for()``
   downgrades anything unexpected to ``unknown`` rather than trusting the DB.
4. **Fail open, silently.** Every database helper here catches exceptions and
   returns ``None``/a safe default, so a missing table or a broken row can never
   stop message processing. Routing then falls through to the pre-existing
   identity chain exactly as before this feature existed.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("fazle.contact_relations")


# ── Vocabulary ────────────────────────────────────────────────────────────────

class ValidationError(ValueError):
    """Raised for a payload the owner cannot legitimately submit."""


TRUST_LEVELS: tuple[str, ...] = ("trusted", "normal", "caution", "blocked")
AUTO_REPLY_MODES: tuple[str, ...] = ("inherit", "always_draft", "never_reply")

#: Roles this registry must never be able to assert. Privilege is granted only
#: by .env allowlists or by a verified employees row — and ``identity_brain``
#: resolves those *before* it ever consults this table (Step 3.9 runs after
#: admin/accountant/seed/employee/roster).
PRIVILEGED_ROLES: frozenset[str] = frozenset(
    {"admin", "superadmin", "accountant", "employee", "supervisor", "owner"}
)

#: code, stored label, identity role, default trust.
#: ``identity_role`` deliberately reuses roles identity_brain already knows
#: (and that already have an entry in ``_ROLE_PRIORITY``) instead of inventing
#: new routing roles — an unknown routing role would fall out of both Route A
#: and Route B. Relations with no safe existing role resolve to ``unknown``
#: (Route B: Ollama-only, never the KB/business machinery).
RELATION_VOCABULARY: tuple[dict[str, str], ...] = (
    {"code": "client",           "label": "Client",           "role": "repeat_client",       "trust": "normal",  "bn": "ক্লায়েন্ট"},
    {"code": "vip_client",       "label": "VIP Client",       "role": "vip_client",          "trust": "trusted", "bn": "ভিআইপি ক্লায়েন্ট"},
    {"code": "escort_client",    "label": "Escort Client",    "role": "client_escort_buyer", "trust": "normal",  "bn": "এসকর্ট ক্লায়েন্ট"},
    {"code": "vendor",           "label": "Vendor",           "role": "vendor",              "trust": "normal",  "bn": "সরবরাহকারী"},
    {"code": "contractor",       "label": "Contractor",       "role": "vendor",              "trust": "normal",  "bn": "ঠিকাদার"},
    {"code": "family",           "label": "Family",           "role": "family",              "trust": "trusted", "bn": "পরিবার"},
    {"code": "relative",         "label": "Relative",         "role": "family",              "trust": "trusted", "bn": "আত্মীয়"},
    {"code": "friend",           "label": "Friend",           "role": "friend_relative",     "trust": "trusted", "bn": "বন্ধু"},
    {"code": "personal_business","label": "Personal Business","role": "personal_business",   "trust": "normal",  "bn": "ব্যক্তিগত কাজ"},
    {"code": "government",       "label": "Government",       "role": "government_contact",  "trust": "caution", "bn": "সরকারি অফিস"},
    {"code": "political",        "label": "Political",        "role": "political_contact",   "trust": "caution", "bn": "রাজনৈতিক"},
    {"code": "journalist",       "label": "Journalist",       "role": "unknown",             "trust": "caution", "bn": "সাংবাদিক"},
    {"code": "law_enforcement",  "label": "Law Enforcement",  "role": "unknown",             "trust": "caution", "bn": "পুলিশ/প্রশাসন"},
    {"code": "threat",           "label": "Threat",           "role": "unknown",             "trust": "blocked", "bn": "হুমকি/চাঁদাবাজ"},
    {"code": "unknown",          "label": "Unknown",          "role": "unknown",             "trust": "normal",  "bn": "অজানা"},
)

_BY_CODE: dict[str, dict[str, str]] = {r["code"]: r for r in RELATION_VOCABULARY}
_BY_LABEL: dict[str, dict[str, str]] = {r["label"].lower(): r for r in RELATION_VOCABULARY}

# Import-time guarantee: a future edit to the vocabulary cannot accidentally
# hand out a privileged role through the UI.
_assert_privileged = {r["role"] for r in RELATION_VOCABULARY} & PRIVILEGED_ROLES
if _assert_privileged:  # pragma: no cover - guards a future edit, not runtime input
    raise RuntimeError(
        f"contact relation vocabulary must not grant privileged roles: {sorted(_assert_privileged)}"
    )


def vocabulary() -> list[dict[str, str]]:
    """Public, JSON-safe vocabulary for the UI dropdowns."""
    return [dict(r) for r in RELATION_VOCABULARY]


def resolve_relation(value: Any) -> Optional[dict[str, str]]:
    """Accept a code ('law_enforcement'), a label ('Law Enforcement') or any
    case/whitespace variant, and return the canonical vocabulary entry."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    key = raw.lower().replace("-", "_").replace(" ", "_")
    if key in _BY_CODE:
        return _BY_CODE[key]
    spaced = raw.lower().replace("_", " ")
    if spaced in _BY_LABEL:
        return _BY_LABEL[spaced]
    # last resort: collapsed comparison ('VIPClient', 'law-enforcement')
    collapsed = key.replace("_", "")
    for code, entry in _BY_CODE.items():
        if code.replace("_", "") == collapsed:
            return entry
    return None


def identity_role_for(relation: Any) -> str:
    """Map a stored relation to a routing role, defaulting to 'unknown'.

    Deliberately total and defensive: an unmapped or corrupted value becomes
    'unknown' rather than leaking into a privileged role.
    """
    entry = resolve_relation(relation)
    role = (entry or {}).get("role") or "unknown"
    if role in PRIVILEGED_ROLES:  # belt-and-braces; tree can never produce these
        return "unknown"
    return role


def normalize_trust(value: Any, default: str = "normal") -> str:
    v = str(value or "").strip().lower()
    return v if v in TRUST_LEVELS else default


def normalize_auto_reply(value: Any, default: str = "inherit") -> str:
    v = str(value or "").strip().lower()
    return v if v in AUTO_REPLY_MODES else default


def validate_payload(
    *,
    relation: Any,
    trust_level: Any = None,
    auto_reply: Any = None,
    tone: Any = None,
    display_name: Any = None,
    notes: Any = None,
) -> dict[str, Any]:
    """Validate/normalise an owner-submitted relation payload.

    Raises ValidationError (→ HTTP 422) for an unknown relation or an
    out-of-range trust/auto-reply value; every other field is optional.
    """
    entry = resolve_relation(relation)
    if entry is None:
        allowed = ", ".join(r["label"] for r in RELATION_VOCABULARY)
        raise ValidationError(f"unknown relation {relation!r}; allowed: {allowed}")

    if trust_level not in (None, ""):
        trust = str(trust_level).strip().lower()
        if trust not in TRUST_LEVELS:
            raise ValidationError(
                f"trust_level must be one of {', '.join(TRUST_LEVELS)}; got {trust_level!r}"
            )
    else:
        trust = entry["trust"]

    if auto_reply not in (None, ""):
        mode = str(auto_reply).strip().lower()
        if mode not in AUTO_REPLY_MODES:
            raise ValidationError(
                f"auto_reply must be one of {', '.join(AUTO_REPLY_MODES)}; got {auto_reply!r}"
            )
    else:
        mode = "inherit"

    # 'blocked' trust always means "never auto-reply" unless the owner picked
    # something even stricter explicitly (never_reply is the strictest).
    if trust == "blocked" and mode == "inherit":
        mode = "never_reply"

    name_override = display_name.strip() if isinstance(display_name, str) else ""
    return {
        "relation_name": entry["label"],
        "relation_code": entry["code"],
        "identity_role": entry["role"],
        "trust_level": trust,
        "auto_reply": mode,
        "tone": tone.strip() if isinstance(tone, str) else "",
        # '' means "no override" → the API stores NULL.
        "display_name": name_override or None,
        "notes": notes.strip() if isinstance(notes, str) else "",
    }


# ── Phone helpers ─────────────────────────────────────────────────────────────

def _phone_variants(phone: str) -> list[str]:
    try:
        from modules.number_identity import normalize_phone as _variants
        variants = _variants(phone) or []
    except Exception:
        variants = []
    if phone and phone not in variants:
        variants.append(phone)
    return variants


def _canonical(phone: str) -> str:
    try:
        from modules.phone_normalizer import normalize_phone as _norm
        return _norm(phone) or phone
    except Exception:
        return phone


# ── Read path (used by identity_brain / bridge_poller / message_router) ───────

async def lookup(phone: str, domain_code: str = "") -> Optional[dict[str, Any]]:
    """Return the active registry row for ``phone``, or None.

    Matches every stored phone variant so a row written before canonicalisation
    still resolves (same class of bug that bit ``fazle_contact_roles`` — see the
    2026-08-03 note in modules/contact_roles/routes.py).
    """
    variants = _phone_variants(phone)
    if not variants:
        return None
    try:
        from app.database import fetch_one

        row = await fetch_one(
            """
            SELECT id, phone, domain_code, relation_name, trust_level, auto_reply,
                   tone, display_name, notes, is_active, updated_at
              FROM fazle_contact_relations
             WHERE phone = ANY($1::text[])
               AND domain_code = COALESCE($2, '')
               AND is_active = TRUE
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            variants,
            domain_code or "",
        )
    except Exception as exc:  # fail open — identity must never depend on this
        log.debug("[contact_relations] lookup failed for %s: %s", phone, exc)
        return None
    return dict(row) if row else None


async def resolution(phone: str, domain_code: str = "") -> Optional[dict[str, Any]]:
    """Lookup + derived identity role, shaped for ``identity_brain``."""
    row = await lookup(phone, domain_code)
    if not row:
        return None
    return {
        "relation_name": row.get("relation_name"),
        "identity_role": identity_role_for(row.get("relation_name")),
        "trust_level": normalize_trust(row.get("trust_level")),
        "auto_reply": normalize_auto_reply(row.get("auto_reply")),
        "display_name": row.get("display_name"),
        "tone": row.get("tone") or "",
        "notes": row.get("notes") or "",
    }


async def auto_reply_mode(phone: str, domain_code: str = "") -> str:
    """'inherit' | 'always_draft' | 'never_reply' for this phone (fail open)."""
    row = await lookup(phone, domain_code)
    if not row:
        return "inherit"
    mode = normalize_auto_reply(row.get("auto_reply"))
    if normalize_trust(row.get("trust_level")) == "blocked" and mode == "inherit":
        return "never_reply"
    return mode


# ── Write path (used only by the admin API) ───────────────────────────────────

async def upsert(
    *,
    phone: str,
    relation: Any,
    trust_level: Any = None,
    auto_reply: Any = None,
    tone: Any = None,
    display_name: Any = None,
    notes: Any = None,
    domain_code: str = "",
    actor: str = "",
    is_active: bool = True,
) -> dict[str, Any]:
    """Insert/update one row. Validates first; never writes a privileged role."""
    from app.database import execute

    payload = validate_payload(
        relation=relation, trust_level=trust_level, auto_reply=auto_reply,
        tone=tone, display_name=display_name, notes=notes,
    )
    canon = _canonical(phone)
    if not canon:
        raise ValidationError(f"invalid phone: {phone!r}")

    await execute(
        """
        INSERT INTO fazle_contact_relations
            (phone, domain_code, relation_name, trust_level, auto_reply, tone,
             display_name, notes, is_active, created_by, updated_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
        ON CONFLICT (phone, domain_code) DO UPDATE SET
            relation_name = EXCLUDED.relation_name,
            trust_level   = EXCLUDED.trust_level,
            auto_reply    = EXCLUDED.auto_reply,
            tone          = EXCLUDED.tone,
            display_name  = EXCLUDED.display_name,
            notes         = EXCLUDED.notes,
            is_active     = EXCLUDED.is_active,
            updated_by    = EXCLUDED.updated_by,
            updated_at    = NOW()
        """,
        canon,
        domain_code or "",
        payload["relation_name"],
        payload["trust_level"],
        payload["auto_reply"],
        payload["tone"],
        payload["display_name"],
        payload["notes"],
        is_active,
        actor or "",
    )

    # Mirror onto wbom_contacts.relation_type_id so the *existing* read paths
    # (message_router.get_contact_context → 'Relation: …' in the AI prompt)
    # agree with this registry instead of contradicting it. Best effort: the
    # registry row is authoritative even if this fails.
    await _mirror_relation_type(canon, payload["relation_name"])

    log.info(
        "[contact_relations] upsert phone=%s relation=%s trust=%s auto_reply=%s actor=%s",
        canon, payload["relation_name"], payload["trust_level"], payload["auto_reply"], actor or "-",
    )
    return {"phone": canon, "domain_code": domain_code or "", **payload, "is_active": is_active}


async def _mirror_relation_type(phone: str, relation_name: str) -> None:
    try:
        from app.database import execute

        await execute(
            """
            UPDATE wbom_contacts c
               SET relation_type_id = t.relation_type_id,
                   updated_at       = NOW()
              FROM wbom_relation_types t
             WHERE c.whatsapp_number = $1
               AND lower(t.relation_name) = lower($2)
            """,
            phone,
            relation_name,
        )
    except Exception as exc:
        log.debug("[contact_relations] relation_type mirror skipped for %s: %s", phone, exc)


async def deactivate(phone: str, domain_code: str = "", actor: str = "") -> int:
    """Soft-delete: is_active=false. Returns the number of rows touched."""
    from app.database import execute

    variants = _phone_variants(phone)
    if not variants:
        return 0
    result = await execute(
        """
        UPDATE fazle_contact_relations
           SET is_active = FALSE, updated_by = $2, updated_at = NOW()
         WHERE phone = ANY($1::text[])
           AND domain_code = COALESCE($3, '')
           AND is_active = TRUE
        """,
        variants,
        actor or "",
        domain_code or "",
    )
    log.info("[contact_relations] deactivate phone=%s actor=%s result=%s", phone, actor or "-", result)
    return _rows_affected(result)


def _rows_affected(result: Any) -> int:
    """asyncpg returns 'UPDATE 1'; some wrappers return an int."""
    if isinstance(result, int):
        return result
    try:
        return int(str(result).split()[-1])
    except Exception:
        return 0


# ── Admin listings ────────────────────────────────────────────────────────────

async def list_relations(
    *, phone: str = "", relation: str = "", domain_code: str = "",
    active_only: bool = True, limit: int = 200, offset: int = 0,
) -> list[dict[str, Any]]:
    from app.database import fetch_all

    where: list[str] = []
    params: list[Any] = []

    if phone:
        params.append(_phone_variants(phone))
        where.append(f"phone = ANY(${len(params)}::text[])")
    if relation:
        entry = resolve_relation(relation)
        params.append(entry["label"] if entry else relation)
        where.append(f"lower(relation_name) = lower(${len(params)})")
    if domain_code != "":
        params.append(domain_code)
        where.append(f"domain_code = ${len(params)}")
    if active_only:
        where.append("is_active = TRUE")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    rows = await fetch_all(
        f"""
        SELECT id, phone, domain_code, relation_name, trust_level, auto_reply,
               tone, display_name, notes, is_active, created_by, updated_by,
               created_at, updated_at
          FROM fazle_contact_relations
          {clause}
         ORDER BY updated_at DESC
         LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def list_unclassified(*, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Phone-book contacts that have no active registry row yet.

    This is the backlog screen: everyone the owner saved on their phone but
    never told the app about. Ordered by most recent inbound message so the
    people who actually write to the business come first.
    """
    from app.database import fetch_all

    rows = await fetch_all(
        """
        SELECT c.whatsapp_number                       AS phone,
               c.display_name,
               t.relation_name                         AS current_relation,
               (SELECT MAX(m.received_at)
                  FROM wbom_whatsapp_messages m
                 WHERE m.sender_number = c.whatsapp_number) AS last_message_at,
               EXISTS (SELECT 1 FROM fazle_contact_roles fcr
                        WHERE fcr.is_active
                          AND right(regexp_replace(fcr.phone, '\\D', '', 'g'), 10)
                            = right(regexp_replace(c.whatsapp_number, '\\D', '', 'g'), 10)
                      )                        AS has_seed_role
          FROM wbom_contacts c
          LEFT JOIN wbom_relation_types t ON t.relation_type_id = c.relation_type_id
         WHERE COALESCE(c.platform, 'whatsapp') = 'whatsapp'
           AND NOT EXISTS (
                SELECT 1 FROM fazle_contact_relations r
                 WHERE r.phone = c.whatsapp_number AND r.is_active)
         ORDER BY last_message_at DESC NULLS LAST, c.contact_id DESC
         LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    return [dict(r) for r in rows]


async def count_unclassified() -> int:
    from app.database import fetch_val

    return await fetch_val(
        """
        SELECT COUNT(*)
          FROM wbom_contacts c
         WHERE COALESCE(c.platform, 'whatsapp') = 'whatsapp'
           AND NOT EXISTS (
                SELECT 1 FROM fazle_contact_relations r
                 WHERE r.phone = c.whatsapp_number AND r.is_active)
        """
    ) or 0
