"""
Unit tests — migration 090 contact relation registry
(modules/contact_relations, its admin API, and the Step 3.9 hook in
modules/identity_brain).

These are deliberately DB-free: the DB-touching helpers are patched, so the
suite proves the *rules* (who may be classified as what, and what identity the
message pipeline then sees) rather than the SQL. Table creation is covered by
the migration itself.

The central invariant under test: this owner-editable registry must never be
able to hand out a privileged identity (admin / accountant / employee /
supervisor). It is a UI dropdown; privilege comes from .env or the employees
table only.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules import contact_relations as cr

pytestmark = pytest.mark.unit


# ── Vocabulary & validation (pure) ───────────────────────────────────────────

class TestVocabulary:
    def test_no_privileged_role_in_vocabulary(self):
        roles = {entry["role"] for entry in cr.RELATION_VOCABULARY}
        assert roles & cr.PRIVILEGED_ROLES == set(), (
            "the UI must never be able to grant a privileged role; "
            f"offending: {roles & cr.PRIVILEGED_ROLES}"
        )

    def test_every_entry_has_bengali_label(self):
        for entry in cr.RELATION_VOCABULARY:
            assert entry["bn"], entry
            assert entry["label"] and entry["code"]

    def test_codes_are_unique(self):
        codes = [e["code"] for e in cr.RELATION_VOCABULARY]
        assert len(codes) == len(set(codes))

    def test_labels_are_unique(self):
        labels = [e["label"].lower() for e in cr.RELATION_VOCABULARY]
        assert len(labels) == len(set(labels))

    def test_vocabulary_payload_is_json_safe_copy(self):
        got = cr.vocabulary()
        got[0]["label"] = "mutated"
        assert cr.RELATION_VOCABULARY[0]["label"] != "mutated"


class TestResolveRelation:
    @pytest.mark.parametrize("value", [
        "family", "Family", " FAMILY ", "law_enforcement", "Law Enforcement",
        "law-enforcement", "LAW ENFORCEMENT", "vip_client", "VIP Client",
    ])
    def test_accepts_code_label_and_variants(self, value):
        assert cr.resolve_relation(value) is not None

    @pytest.mark.parametrize("value", ["", "   ", None, 5, "not_a_relation", "admin"])
    def test_rejects_unknown(self, value):
        assert cr.resolve_relation(value) is None


class TestIdentityRole:
    def test_known_relations_map_to_existing_routing_roles(self):
        assert cr.identity_role_for("Family") == "family"
        assert cr.identity_role_for("Client") == "repeat_client"
        assert cr.identity_role_for("Vendor") == "vendor"
        assert cr.identity_role_for("Friend") == "friend_relative"

    @pytest.mark.parametrize("value", ["Journalist", "Threat", "Law Enforcement", "nonsense", "", None])
    def test_unmappable_relations_are_unknown(self, value):
        assert cr.identity_role_for(value) == "unknown"

    @pytest.mark.parametrize("value", ["admin", "superadmin", "accountant", "employee", "owner"])
    def test_privileged_strings_can_never_resolve(self, value):
        assert cr.identity_role_for(value) == "unknown"

    def test_no_vocabulary_role_is_privileged(self):
        for entry in cr.RELATION_VOCABULARY:
            assert cr.identity_role_for(entry["label"]) not in cr.PRIVILEGED_ROLES


class TestValidatePayload:
    def test_defaults_come_from_vocabulary(self):
        out = cr.validate_payload(relation="family")
        assert out["relation_name"] == "Family"
        assert out["trust_level"] == "trusted"
        assert out["auto_reply"] == "inherit"

    def test_explicit_values_win(self):
        out = cr.validate_payload(
            relation="journalist", trust_level="blocked", auto_reply="always_draft"
        )
        assert out["trust_level"] == "blocked"
        assert out["auto_reply"] == "always_draft"

    def test_blocked_trust_implies_never_reply(self):
        out = cr.validate_payload(relation="threat")
        assert out["trust_level"] == "blocked"
        assert out["auto_reply"] == "never_reply"

    def test_unknown_relation_raises(self):
        with pytest.raises(cr.ValidationError):
            cr.validate_payload(relation="best_friend")

    def test_bad_trust_raises(self):
        with pytest.raises(cr.ValidationError):
            cr.validate_payload(relation="family", trust_level="bestie")

    def test_bad_auto_reply_raises(self):
        with pytest.raises(cr.ValidationError):
            cr.validate_payload(relation="family", auto_reply="sometimes")

    def test_blank_display_name_becomes_none(self):
        out = cr.validate_payload(relation="friend", display_name="   ")
        assert out["display_name"] is None


# ── Fail-open read path ──────────────────────────────────────────────────────

class TestFailOpen:
    async def test_lookup_returns_none_when_db_is_broken(self, monkeypatch):
        import app.database as db

        async def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "fetch_one", _boom)
        assert await cr.lookup("8801712345678") is None

    async def test_resolution_returns_none_when_db_is_broken(self, monkeypatch):
        import app.database as db

        async def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "fetch_one", _boom)
        assert await cr.resolution("8801712345678") is None

    async def test_auto_reply_mode_defaults_to_inherit_on_failure(self, monkeypatch):
        import app.database as db

        async def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "fetch_one", _boom)
        assert await cr.auto_reply_mode("8801712345678") == "inherit"

    async def test_blocked_trust_degrades_to_never_reply(self, monkeypatch):
        async def _row(*a, **k):
            return {
                "relation_name": "Threat", "trust_level": "blocked",
                "auto_reply": "inherit", "display_name": None,
                "tone": "", "notes": "",
            }

        monkeypatch.setattr(cr, "lookup", _row)
        assert await cr.auto_reply_mode("8801712345678") == "never_reply"

    async def test_resolution_shapes_identity_role(self, monkeypatch):
        async def _row(*a, **k):
            return {
                "relation_name": "Family", "trust_level": "trusted",
                "auto_reply": "inherit", "display_name": "আম্মু",
                "tone": "informal", "notes": "",
            }

        monkeypatch.setattr(cr, "lookup", _row)
        out = await cr.resolution("8801712345678")
        assert out["identity_role"] == "family"
        assert out["display_name"] == "আম্মু"
        assert out["trust_level"] == "trusted"


# ── identity_brain Step 3.9 ─────────────────────────────────────────────────

class TestDetectIdentityStep39:
    """An owner-set relation must decide the identity — while still losing to
    every privileged source above it (admin/accountant/seed/employee)."""

    @staticmethod
    def _patch_lower_steps(monkeypatch, ib):
        async def _none(*a, **k):
            return None

        # identity_brain only imports fetch_one at module scope; patch exactly
        # what it uses so no other name has to exist.
        monkeypatch.setattr(ib, "fetch_one", _none)
        monkeypatch.setattr(ib, "_check_admin_settings", lambda p: None)
        monkeypatch.setattr(ib, "_check_accountant_settings", lambda p: None)
        monkeypatch.setattr(ib, "_lookup_cash_identity", _none)
        monkeypatch.setattr(ib, "_lookup_attendance_identity", _none)
        monkeypatch.setattr(ib, "_lookup_escort_roster", _none)
        monkeypatch.setattr(ib, "_matches_env_escort_phone", lambda p: False)

    async def test_relation_decides_identity_and_carries_policy(self, monkeypatch):
        import modules.identity_brain as ib

        self._patch_lower_steps(monkeypatch, ib)
        monkeypatch.setattr(ib, "_lookup_seed", lambda *a, **k: _await(None))
        monkeypatch.setattr(ib, "_lookup_employee", lambda *a, **k: _await(None))

        async def _rel(phone):
            return {
                "relation_name": "Family", "identity_role": "family",
                "trust_level": "trusted", "auto_reply": "always_draft",
                "display_name": "আম্মু", "tone": "informal", "notes": "",
            }

        monkeypatch.setattr(ib, "_lookup_contact_relation", _rel)

        out = await ib.detect_identity("8801712345678")
        assert out["role"] == "family"
        assert out["identity_role"] == "family"
        assert out["identity_source"] == "contact_relation"
        assert out["identity_confidence"] == 98
        assert out["display_name"] == "আম্মু"
        assert out["contact_relation"] == "Family"
        assert out["contact_trust_level"] == "trusted"
        assert out["contact_auto_reply"] == "always_draft"
        assert out["contact_tone"] == "informal"

    async def test_admin_still_outranks_the_registry(self, monkeypatch):
        import modules.identity_brain as ib

        self._patch_lower_steps(monkeypatch, ib)
        monkeypatch.setattr(ib, "_check_admin_settings", lambda p: {"admin": True})

        async def _rel(phone):
            return {
                "relation_name": "Threat", "identity_role": "unknown",
                "trust_level": "blocked", "auto_reply": "never_reply",
                "display_name": None, "tone": "", "notes": "",
            }

        monkeypatch.setattr(ib, "_lookup_contact_relation", _rel)

        out = await ib.detect_identity("8801712345678")
        assert out["role"] == "admin"

    async def test_no_relation_falls_through_to_unknown(self, monkeypatch):
        import modules.identity_brain as ib

        self._patch_lower_steps(monkeypatch, ib)
        monkeypatch.setattr(ib, "_lookup_seed", lambda *a, **k: _await(None))
        monkeypatch.setattr(ib, "_lookup_employee", lambda *a, **k: _await(None))
        monkeypatch.setattr(ib, "_lookup_contact_relation", lambda *a, **k: _await(None))
        monkeypatch.setattr(ib, "_lookup_contact", lambda *a, **k: _await(None))

        out = await ib.detect_identity("8801712345678")
        assert out["identity_role"] == "unknown"

    async def test_registry_error_is_swallowed(self, monkeypatch):
        import modules.identity_brain as ib

        self._patch_lower_steps(monkeypatch, ib)
        monkeypatch.setattr(ib, "_lookup_seed", lambda *a, **k: _await(None))
        monkeypatch.setattr(ib, "_lookup_employee", lambda *a, **k: _await(None))

        async def _boom(phone):
            raise ValueError("bad row")

        monkeypatch.setattr(ib, "_lookup_contact_relation", ib._lookup_contact_relation)
        monkeypatch.setattr(cr, "resolution", _boom)

        out = await ib.detect_identity("8801712345678")
        assert out["identity_role"] == "unknown"


async def _await(value):
    return value


# ── Reply gates ─────────────────────────────────────────────────────────────

class TestReplyGates:
    async def test_never_reply_forces_silent_skip(self, monkeypatch):
        from modules import message_router as mr

        async def _mode(phone, domain_code=""):
            return "never_reply"

        monkeypatch.setattr(cr, "auto_reply_mode", _mode)
        skip, reason = await mr._should_silent_skip("8801712345678")
        assert skip is True
        assert "never_reply" in reason

    async def test_always_draft_forces_draft(self, monkeypatch):
        from modules import bridge_poller as bp
        from modules import wa_chat_frontend as wcf

        async def _mode(phone, domain_code=""):
            return "always_draft"

        async def _false(*a, **k):
            return "false"

        monkeypatch.setattr(cr, "auto_reply_mode", _mode)
        monkeypatch.setattr(wcf, "get_runtime_setting", _false)
        skip = await bp._is_draft_always("8801712345678", "unknown", "")
        assert skip is True


# ── Admin API boundary ──────────────────────────────────────────────────────

@pytest.fixture()
def api_client(monkeypatch):
    from modules.contact_relations import routes as routes_mod

    async def _noop_audit(*a, **k):
        return None

    async def _fake_upsert(**kwargs):
        payload = cr.validate_payload(
            relation=kwargs["relation"],
            trust_level=kwargs.get("trust_level"),
            auto_reply=kwargs.get("auto_reply"),
            tone=kwargs.get("tone"),
            display_name=kwargs.get("display_name"),
            notes=kwargs.get("notes"),
        )
        return {"phone": kwargs["phone"], "domain_code": "", **payload,
                "is_active": kwargs.get("is_active", True)}

    monkeypatch.setattr(routes_mod, "_audit", _noop_audit)
    monkeypatch.setattr(routes_mod, "upsert", _fake_upsert)

    app = FastAPI()
    app.include_router(routes_mod.router)
    app.dependency_overrides[routes_mod._require_api_key] = lambda: None
    return TestClient(app)


class TestApiBoundary:
    def test_vocabulary_endpoint_exposes_no_privileged_role(self, api_client):
        r = api_client.get("/admin/contact-relations/vocabulary")
        assert r.status_code == 200
        body = r.json()
        roles = {item["role"] for item in body["items"]}
        assert not (roles & cr.PRIVILEGED_ROLES)
        assert set(body["trust_levels"]) == set(cr.TRUST_LEVELS)
        assert set(body["auto_reply_modes"]) == set(cr.AUTO_REPLY_MODES)

    def test_valid_relation_accepted(self, api_client):
        r = api_client.post(
            "/admin/contact-relations",
            json={"phone": "01844556677", "relation": "family"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["relation_name"] == "Family"
        assert body["ok"] is True

    @pytest.mark.parametrize("relation", ["admin", "accountant", "employee", "superuser", "best_friend", ""])
    def test_privileged_or_unknown_relation_rejected(self, api_client, relation):
        r = api_client.post(
            "/admin/contact-relations",
            json={"phone": "01844556677", "relation": relation},
        )
        assert r.status_code == 422, r.text

    def test_bad_trust_rejected(self, api_client):
        r = api_client.post(
            "/admin/contact-relations",
            json={"phone": "01844556677", "relation": "family", "trust_level": "bestie"},
        )
        assert r.status_code == 422

    def test_bad_auto_reply_rejected(self, api_client):
        r = api_client.post(
            "/admin/contact-relations",
            json={"phone": "01844556677", "relation": "family", "auto_reply": "maybe"},
        )
        assert r.status_code == 422

    def test_missing_phone_rejected(self, api_client):
        r = api_client.post(
            "/admin/contact-relations",
            json={"phone": "  ", "relation": "family"},
        )
        assert r.status_code == 422
