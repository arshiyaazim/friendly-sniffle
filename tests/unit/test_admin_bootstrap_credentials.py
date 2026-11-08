"""First-boot admin credentials — migration 092 + rbac.ensure_bootstrap_credentials.

Why these tests exist
---------------------
A fresh install had no username/password login at all:

  * `ensure_bootstrap_admins()` created admin rows from ADMIN_NUMBERS without
    any `username` / `password_hash`;
  * `POST /api/admin/login` in `password` mode therefore always failed;
  * the only usable credential was the server-wide INTERNAL_API_KEY.

The bootstrap added here must be *idempotent and non-destructive*: it may create
a first credential, and it must never overwrite one that already exists. Every
test below pins one of those guarantees. No database is required — the
`fetch_*` / `execute` helpers are replaced with a scripted fake.
"""
from __future__ import annotations

import re

import pytest
from fastapi import HTTPException

from modules import rbac


# ── fake database ────────────────────────────────────────────────────────────
class FakeDB:
    """Scripted stand-in for app.database.fetch_one/fetch_val/execute."""

    def __init__(
        self,
        *,
        has_credential: bool = False,
        credential_lookup_raises: bool = False,
        superadmin_id: int | None = 7,
        already_superadmin: bool = True,
        username_holder: int | None = None,
        pre_092_schema: bool = False,
        role_exists: bool = True,
    ) -> None:
        self.has_credential = has_credential
        self.credential_lookup_raises = credential_lookup_raises
        self.superadmin_id = superadmin_id
        self.already_superadmin = already_superadmin
        self.username_holder = username_holder
        self.pre_092_schema = pre_092_schema
        self.role_exists = role_exists
        self.executes: list[tuple[str, tuple]] = []
        self.created_admin = False

    # fetch_one — only `_any_credential_exists` uses the bare fetch_one SELECT
    async def fetch_one(self, sql, *args, **kwargs):
        flat = " ".join(sql.split())
        if "password_hash IS NOT NULL" in flat:
            if self.credential_lookup_raises:
                raise RuntimeError("connection reset")
            return {"ok": 1} if self.has_credential else None
        return None

    async def fetch_val(self, sql, *args, **kwargs):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT a.id FROM fazle_admins"):
            return self.superadmin_id
        if flat.startswith("SELECT id FROM fazle_admins WHERE status='active'"):
            return self.superadmin_id
        if "FROM fazle_admin_roles WHERE admin_id=$1 AND role_name='superadmin'" in flat:
            return 1 if self.already_superadmin else None
        if "LOWER(username)=$1 AND id <> $2" in flat:
            return self.username_holder
        if "FROM fazle_roles" in flat:
            return 1 if self.role_exists else None
        if flat.startswith("INSERT INTO fazle_admins"):
            self.created_admin = True
            return 42
        return None

    async def execute(self, sql, *args, **kwargs):
        flat = " ".join(sql.split())
        if (
            self.pre_092_schema
            and "must_change_password" in flat
            and not any("must_change_password" not in s for s, _ in self.executes)
        ):
            raise RuntimeError('column "must_change_password" does not exist')
        self.executes.append((flat, args))
        return "UPDATE 1"


@pytest.fixture
def fake_db(monkeypatch):
    def _install(**kwargs) -> FakeDB:
        db = FakeDB(**kwargs)
        monkeypatch.setattr(rbac, "fetch_one", db.fetch_one, raising=True)
        monkeypatch.setattr(rbac, "fetch_val", db.fetch_val, raising=True)
        monkeypatch.setattr(rbac, "execute", db.execute, raising=True)
        return db

    for key in ("BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_PHONE"):
        monkeypatch.delenv(key, raising=False)
    return _install


# ── environment handling ─────────────────────────────────────────────────────
def test_env_defaults(monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    assert rbac.bootstrap_credential_env() == (rbac.DEFAULT_BOOTSTRAP_USERNAME, "")
    assert rbac.DEFAULT_BOOTSTRAP_USERNAME == "admin"


def test_env_is_normalised(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "  Owner.Zahid  ")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "  s3cret-pass  ")
    assert rbac.bootstrap_credential_env() == ("owner.zahid", "s3cret-pass")


def test_blank_username_falls_back(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "   ")
    assert rbac.bootstrap_credential_env()[0] == "admin"


def test_generated_password_is_readable_and_strong():
    pw = rbac.generate_bootstrap_password()
    assert len(pw) == 18
    assert re.fullmatch(r"[A-Za-z2-9]+", pw)
    assert not set(pw) & set("0O1lI")
    assert rbac.generate_bootstrap_password() != pw


# ── ensure_bootstrap_credentials ─────────────────────────────────────────────
async def test_existing_credential_is_never_touched(fake_db):
    db = fake_db(has_credential=True)
    out = await rbac.ensure_bootstrap_credentials()
    assert out["status"] == "already_configured"
    assert out["generated_password"] is None
    assert db.executes == []


async def test_lookup_failure_fails_closed(fake_db):
    db = fake_db(credential_lookup_raises=True)
    out = await rbac.ensure_bootstrap_credentials()
    assert out["status"] == "already_configured"
    assert db.executes == []


async def test_creates_credential_from_env(fake_db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "owner")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "env-provided-pw")
    db = fake_db(superadmin_id=7)

    out = await rbac.ensure_bootstrap_credentials()

    assert out["status"] == "created"
    assert out["username"] == "owner"
    assert out["admin_id"] == 7
    assert out["generated_password"] is None      # nothing secret to print
    assert len(db.executes) == 1
    sql, args = db.executes[0]
    assert "must_change_password=true" in sql
    assert "credential_set_at=NOW()" in sql
    username, password_hash, admin_id = args
    assert username == "owner"
    assert password_hash.startswith("pbkdf2_sha256$")
    assert rbac.verify_password("env-provided-pw", password_hash)
    assert admin_id == 7


async def test_generates_and_returns_password_when_env_empty(fake_db):
    db = fake_db(superadmin_id=7)

    out = await rbac.ensure_bootstrap_credentials()

    assert out["status"] == "created"
    generated = out["generated_password"]
    assert generated and len(generated) == 18
    _, args = db.executes[0]
    assert rbac.verify_password(generated, args[1])


async def test_short_env_password_is_replaced(fake_db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "abc")   # below the 6-char floor
    db = fake_db()
    out = await rbac.ensure_bootstrap_credentials()
    assert out["generated_password"] and len(out["generated_password"]) == 18
    assert rbac.verify_password(out["generated_password"], db.executes[0][1][1])


async def test_username_collision_uses_suffix(fake_db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "env-provided-pw")
    db = fake_db(superadmin_id=7, username_holder=3)   # some other row owns "admin"

    out = await rbac.ensure_bootstrap_credentials()

    assert out["username"] == "admin7"
    assert db.executes[0][1][0] == "admin7"


async def test_creates_owner_row_when_no_admin_exists(fake_db):
    db = fake_db(superadmin_id=None, has_credential=False)
    out = await rbac.ensure_bootstrap_credentials()
    assert out["status"] == "created"
    assert db.created_admin is True
    assert out["admin_id"] == 42
    assert out["username"] == "admin"


async def test_owner_row_without_role_table(fake_db):
    db = fake_db(superadmin_id=None, role_exists=False)
    out = await rbac.ensure_bootstrap_credentials()
    assert out["status"] == "created"
    # no role grant attempted, no exception
    assert all("fazle_admin_roles" not in sql for sql, _ in db.executes)


async def test_pre_092_schema_falls_back(fake_db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "env-provided-pw")
    db = fake_db(superadmin_id=7, pre_092_schema=True)

    out = await rbac.ensure_bootstrap_credentials()

    assert out["status"] == "created"
    # first UPDATE (with the new columns) failed, the fallback one went through
    assert len(db.executes) == 1
    sql, args = db.executes[0]
    assert "must_change_password" not in sql
    assert rbac.verify_password("env-provided-pw", args[1])


async def test_never_raises_on_unexpected_error(fake_db, monkeypatch):
    db = fake_db(superadmin_id=7)

    async def boom(*a, **kw):
        raise RuntimeError("permission denied for table fazle_admins")

    monkeypatch.setattr(rbac, "execute", boom, raising=True)
    out = await rbac.ensure_bootstrap_credentials()
    assert out["status"] == "error"
    assert "permission denied" in out["error"]


async def test_grants_superadmin_to_the_bootstrap_account(fake_db, monkeypatch):
    """A fresh install must be an owner, not just a login: migration 093 seeds the
    role catalogue and the bootstrap grants it once."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "env-provided-pw")
    db = fake_db(superadmin_id=9, already_superadmin=False)

    out = await rbac.ensure_bootstrap_credentials()

    assert out["status"] == "created"
    grants = [sql for sql, _args in db.executes if "fazle_admin_roles" in sql]
    assert len(grants) == 1
    assert "'superadmin','bootstrap_credentials'" in grants[0]


async def test_no_duplicate_superadmin_grant(fake_db, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "env-provided-pw")
    db = fake_db(superadmin_id=9, already_superadmin=True)
    await rbac.ensure_bootstrap_credentials()
    assert not any("fazle_admin_roles" in sql for sql, _args in db.executes)


# ── set_own_password ─────────────────────────────────────────────────────────
async def test_set_own_password_clears_the_flag(fake_db):
    db = fake_db()
    await rbac.set_own_password(11, "brand-new-pw")
    sql, args = db.executes[0]
    assert "must_change_password=false" in sql
    assert rbac.verify_password("brand-new-pw", args[0])
    assert args[1] == 11


# ── POST /api/admin/change-password ──────────────────────────────────────────
@pytest.fixture
def change_pw(monkeypatch):
    from modules import admin_transactions

    state = {"changed": None, "record": None}

    async def fake_get_by_api_key(key):
        return {"id": 4, "username": "owner", "phone": "8801", "status": "active"}

    async def fake_get_by_username(username):
        return {
            "id": 4,
            "username": "owner",
            "password_hash": rbac.hash_password("old-password"),
        }

    async def fake_set(admin_id, new_password):
        state["changed"] = (admin_id, new_password)

    monkeypatch.setattr(rbac, "get_admin_by_api_key", fake_get_by_api_key, raising=True)
    monkeypatch.setattr(rbac, "get_admin_by_username", fake_get_by_username, raising=True)
    monkeypatch.setattr(rbac, "set_own_password", fake_set, raising=True)
    return admin_transactions.change_own_password, state


async def test_change_password_happy_path(change_pw):
    fn, state = change_pw
    out = await fn({"current_password": "old-password", "new_password": "new-password"}, key="fk_x")
    assert out["ok"] is True
    assert out["must_change_password"] is False
    assert state["changed"] == (4, "new-password")


async def test_change_password_rejects_wrong_current(change_pw):
    fn, state = change_pw
    with pytest.raises(HTTPException) as exc:
        await fn({"current_password": "wrong", "new_password": "new-password"}, key="fk_x")
    assert exc.value.status_code == 401
    assert state["changed"] is None


async def test_change_password_rejects_short_password(change_pw):
    fn, state = change_pw
    with pytest.raises(HTTPException) as exc:
        await fn({"current_password": "old-password", "new_password": "short"}, key="fk_x")
    assert exc.value.status_code == 400
    assert state["changed"] is None


async def test_change_password_rejects_server_key(change_pw, monkeypatch):
    fn, state = change_pw

    async def no_account(key):
        return None

    monkeypatch.setattr(rbac, "get_admin_by_api_key", no_account, raising=True)
    with pytest.raises(HTTPException) as exc:
        await fn({"current_password": "x", "new_password": "new-password"}, key="INTERNAL")
    assert exc.value.status_code == 400
    assert "server API key" in exc.value.detail
    assert state["changed"] is None


async def test_change_password_requires_both_fields(change_pw):
    fn, _ = change_pw
    with pytest.raises(HTTPException) as exc:
        await fn({"new_password": "new-password"}, key="fk_x")
    assert exc.value.status_code == 400


# ── login throttle ───────────────────────────────────────────────────────────
@pytest.fixture
def throttle(monkeypatch):
    from modules import admin_transactions

    admin_transactions._LOGIN_FAILURES.clear()
    yield admin_transactions
    admin_transactions._LOGIN_FAILURES.clear()


async def test_login_throttles_after_repeated_failures(throttle, monkeypatch):
    fn = throttle.admin_login

    async def no_such_admin(username):
        return None

    monkeypatch.setattr(rbac, "get_admin_by_username", no_such_admin, raising=True)

    body = {"mode": "password", "username": "owner", "password": "guess"}
    for _ in range(throttle.LOGIN_MAX_FAILURES):
        with pytest.raises(HTTPException) as exc:
            await fn(dict(body))
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await fn(dict(body))
    assert exc.value.status_code == 429
    assert "Retry-After" in (exc.value.headers or {})


async def test_login_success_clears_failures(throttle, monkeypatch):
    fn = throttle.admin_login

    async def admin(username):
        return {
            "id": 1,
            "status": "active",
            "username": "owner",
            "password_hash": rbac.hash_password("right-password"),
            "must_change_password": True,
        }

    async def fake_execute(*a, **kw):
        return "UPDATE 1"

    async def fake_actor(token):
        return {"admin_id": 1, "roles": ["owner"], "phone": "8801", "name": "Owner"}

    monkeypatch.setattr(rbac, "get_admin_by_username", admin, raising=True)
    monkeypatch.setattr(throttle, "execute", fake_execute, raising=True)
    monkeypatch.setattr(throttle, "_get_actor_context", fake_actor, raising=True)

    with pytest.raises(HTTPException):
        await fn({"mode": "password", "username": "owner", "password": "wrong"})
    assert throttle.login_retry_after(throttle._throttle_key("owner", None)) == 0

    out = await fn({"mode": "password", "username": "owner", "password": "right-password"})
    assert out["auth_mode"] == "password"
    assert out["must_change_password"] is True
    assert out["credential"]["value"].startswith("fk_")
    assert throttle.login_retry_after(throttle._throttle_key("owner", None)) == 0
    assert throttle._LOGIN_FAILURES == {}


# ── wiring / documentation drift ─────────────────────────────────────────────
def test_route_is_registered():
    from modules import admin_transactions

    paths = {r.path for r in admin_transactions.router.routes}
    assert "/api/admin/login" in paths
    assert "/api/admin/change-password" in paths


def test_login_response_advertises_must_change_password():
    """The dashboard relies on this field to show the change-password pane."""
    import inspect
    from modules import admin_transactions

    src = inspect.getsource(admin_transactions.admin_login)
    assert '"must_change_password"' in src


def test_dashboard_has_username_password_form():
    from pathlib import Path

    html = Path("app/static/dashboard.html").read_text(encoding="utf-8")
    assert 'id="userInput"' in html
    assert 'id="passInput"' in html
    assert 'mode: "password"' in html
    assert "/api/admin/change-password" in html


def test_env_example_and_installer_document_the_bootstrap():
    from pathlib import Path

    env = Path(".env.example").read_text(encoding="utf-8")
    install = Path("install.sh").read_text(encoding="utf-8")
    assert "BOOTSTRAP_ADMIN_USERNAME" in env
    assert "BOOTSTRAP_ADMIN_PASSWORD" in env
    assert "BOOTSTRAP_ADMIN_PASSWORD" in install
    assert "db/migrate.py" in install
    assert "APP_PORT" in install
