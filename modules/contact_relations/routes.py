"""
Contact Relations — owner-managed identity/relation API.

GET    /admin/contact-relations              → list (filter: phone, relation, domain)
GET    /admin/contact-relations/vocabulary   → the choices the UI may offer
GET    /admin/contact-relations/unclassified → phone-book contacts with no relation yet
POST   /admin/contact-relations              → create/update one relation
DELETE /admin/contact-relations/{phone}      → deactivate (soft delete)

Every write is validated by ``modules.contact_relations.validate_payload``
(which structurally forbids privileged roles) and attributed in
``fazle_admin_audit`` with channel ``contact_relations``.

Mirrors modules/contact_roles/routes.py conventions: ``X-Internal-Key`` auth via
shared.auth_deps.require_api_key, phone normalisation before write, and
all-variant matching on read.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from shared.auth_deps import require_api_key as _require_api_key

from . import (
    AUTO_REPLY_MODES,
    TRUST_LEVELS,
    ValidationError,
    count_unclassified,
    deactivate,
    list_relations,
    list_unclassified,
    upsert,
    vocabulary,
)

log = logging.getLogger("fazle.contact_relations.api")
router = APIRouter(prefix="/admin/contact-relations", tags=["contact-relations"])


class RelationBody(BaseModel):
    phone: str
    relation: str
    trust_level: Optional[str] = None
    auto_reply: Optional[str] = None
    tone: Optional[str] = None
    display_name: Optional[str] = None
    notes: Optional[str] = None
    domain_code: str = ""
    is_active: bool = True
    actor: str = "admin_api"


async def _audit(command: str, args: dict, allowed: bool, summary: str, actor: str) -> None:
    """Best-effort admin audit trail — never fails the request."""
    try:
        from app.database import execute

        await execute(
            """
            INSERT INTO fazle_admin_audit
                (actor_label, channel, command, args, allowed, result_summary)
            VALUES ($1, 'contact_relations', $2, $3, $4, $5)
            """,
            (actor or "")[:80],
            command[:120],
            json.dumps(args, ensure_ascii=False, default=str),
            allowed,
            summary[:2000],
        )
    except Exception as exc:  # pragma: no cover - audit must not break writes
        log.debug("[contact_relations] audit skipped: %s", exc)


@router.get("/vocabulary", dependencies=[Depends(_require_api_key)])
async def get_vocabulary():
    """Choices for the UI dropdowns (code/label/role/default trust/বাংলা label)."""
    return {
        "items": vocabulary(),
        "trust_levels": list(TRUST_LEVELS),
        "auto_reply_modes": list(AUTO_REPLY_MODES),
    }


@router.get("/unclassified", dependencies=[Depends(_require_api_key)])
async def get_unclassified(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Phone-book contacts the owner has not classified yet.

    This is the 'শ্রেণিবিহীন কন্টাক্ট' backlog screen: everyone saved in the
    owner's phone book who never got an explicit relation. Sorted by most recent
    inbound message so the people who actually write come first.
    """
    rows = await list_unclassified(limit=limit, offset=offset)
    total = await count_unclassified()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("last_message_at") is not None:
            d["last_message_at"] = d["last_message_at"].isoformat()
        items.append(d)
    return {"items": items, "count": len(items), "total": total, "limit": limit, "offset": offset}


@router.get("", dependencies=[Depends(_require_api_key)])
async def get_relations(
    phone: str = Query(""),
    relation: str = Query(""),
    domain_code: str = Query(""),
    active_only: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows = await list_relations(
        phone=phone, relation=relation, domain_code=domain_code,
        active_only=active_only, limit=limit, offset=offset,
    )
    items = []
    for r in rows:
        d = dict(r)
        for key in ("created_at", "updated_at"):
            if d.get(key) is not None:
                d[key] = d[key].isoformat()
        items.append(d)
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post("", dependencies=[Depends(_require_api_key)])
async def post_relation(body: RelationBody):
    """Create or update the relation for one phone (idempotent upsert)."""
    if not (body.phone or "").strip():
        raise HTTPException(status_code=422, detail="phone is required")
    try:
        result = await upsert(
            phone=body.phone,
            relation=body.relation,
            trust_level=body.trust_level,
            auto_reply=body.auto_reply,
            tone=body.tone,
            display_name=body.display_name,
            notes=body.notes,
            domain_code=body.domain_code,
            actor=body.actor,
            is_active=body.is_active,
        )
    except ValidationError as exc:
        await _audit("relation.upsert", body.model_dump(), False, str(exc), body.actor)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await _audit(
        "relation.upsert", body.model_dump(), True,
        f"{result['phone']} → {result['relation_name']} "
        f"(trust={result['trust_level']}, auto_reply={result['auto_reply']})",
        body.actor,
    )
    return {"ok": True, **result}


@router.delete("/{phone}", dependencies=[Depends(_require_api_key)])
async def delete_relation(
    phone: str,
    domain_code: str = Query(""),
    actor: str = Query("admin_api"),
):
    """Soft-delete the relation for a phone (is_active=false)."""
    try:
        touched = await deactivate(phone, domain_code=domain_code, actor=actor)
    except Exception as exc:
        log.error("[contact_relations] delete failed for %s: %s", phone, exc)
        raise HTTPException(status_code=500, detail="could not deactivate relation") from exc
    if not touched:
        raise HTTPException(status_code=404, detail=f"No active relation for phone={phone}")
    await _audit("relation.deactivate", {"phone": phone, "domain_code": domain_code},
                 True, f"{touched} row(s) deactivated", actor)
    return {"ok": True, "phone": phone, "deactivated": touched}
