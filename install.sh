#!/bin/bash
# Fazle Core — installation & start script (VPS / systemd hosts)
#
#   bash install.sh              # full install: env → schema → unit → start
#   bash install.sh --preflight  # checks only: no packages, no unit, no restart
#                                # (it still creates .env on a first run)
#
# What it does, in order:
#   1. venv + requirements.txt
#   2. .env: create from .env.example on first run with a freshly generated
#      INTERNAL_API_KEY and BOOTSTRAP_ADMIN_PASSWORD (an existing .env is never
#      modified, and is forced to mode 600)
#   3. preflight: DATABASE_URL present, database reachable, pg_trgm available,
#      APP_PORT not already taken by another service (e.g. an existing app or a
#      WhatsApp bridge — this app is a SHARED neighbour, it does not replace them)
#   4. schema: scripts/apply_schema.py applies payroll → escort → db/migrations,
#      tracking every file in schema_migrations. Exit code 3 means the database
#      still needs objects that only exist in the original production database;
#      the installer then refuses to continue unless ALLOW_INCOMPLETE_SCHEMA=1
#   5. unit: fazle-core.service rendered for this checkout, installed, enabled
#   6. verification: the restart gate must pass before the script reports success
#
# Overridable: APP_DIR (defaults to this script's directory), RUN_USER
# (defaults to $SUDO_USER, else the current user), PYTHON_BIN, UNIT_NAME,
# APP_PORT, DATABASE_URL, SKIP_DB_CHECKS=1, ALLOW_INCOMPLETE_SCHEMA=1.
#
# For anything beyond a single-host install, follow MASTER_PRODUCTION_PLAN.md.

set -euo pipefail

PREFLIGHT_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --preflight) PREFLIGHT_ONLY=1 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
UNIT_NAME="${UNIT_NAME:-fazle-core}"
VENV_DIR="$APP_DIR/venv"
ENV_FILE="$APP_DIR/.env"

say()  { printf '%s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 2; }

say "=== Fazle Core Install ==="
say "  app dir : $APP_DIR"
say "  run user: $RUN_USER"

[ -f "$APP_DIR/requirements.txt" ] || die "$APP_DIR/requirements.txt not found — is APP_DIR correct?"

# ── 1. Python environment ───────────────────────────────────────────────────
if [ "$PREFLIGHT_ONLY" -eq 0 ]; then
  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created ($VENV_DIR)"
  fi
  "$VENV_DIR/bin/pip" install -q --upgrade pip
  "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  ok "packages installed"
else
  [ -x "$VENV_DIR/bin/python" ] || warn "venv missing ($VENV_DIR) — preflight cannot check the database"
fi

# ── 2. .env (configuration + secrets) ───────────────────────────────────────
rand_hex() { openssl rand -hex 32 2>/dev/null || "$PYTHON_BIN" -c 'import secrets;print(secrets.token_hex(32))'; }
rand_pw()  { "$PYTHON_BIN" -c 'import secrets;a="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";print("".join(secrets.choice(a) for _ in range(18)))'; }

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$APP_DIR/.env.example" ]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    _key="$(rand_hex)"
    _pw="$(rand_pw)"
    # Replace placeholders in place (portable sed, value-only edit).
    sed -i "s|^INTERNAL_API_KEY=.*|INTERNAL_API_KEY=$_key|" "$ENV_FILE"
    if [ -n "${APP_PORT:-}" ]; then
      sed -i "s|^APP_PORT=.*|APP_PORT=$APP_PORT|" "$ENV_FILE"
    fi
    if [ -n "${DATABASE_URL:-}" ]; then
      sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$DATABASE_URL|" "$ENV_FILE"
    fi
    # First-boot login. Kept in .env (mode 600) so it can be re-read; the app
    # ignores it once the account already has a password. Both keys already
    # exist in .env.example (the password one is empty), so replace in place —
    # appending would leave the empty line winning.
    if grep -q '^BOOTSTRAP_ADMIN_USERNAME=' "$ENV_FILE"; then
      sed -i 's|^BOOTSTRAP_ADMIN_USERNAME=.*|BOOTSTRAP_ADMIN_USERNAME=admin|' "$ENV_FILE"
    else
      printf 'BOOTSTRAP_ADMIN_USERNAME=admin\n' >> "$ENV_FILE"
    fi
    if grep -q '^BOOTSTRAP_ADMIN_PASSWORD=' "$ENV_FILE"; then
      sed -i "s|^BOOTSTRAP_ADMIN_PASSWORD=.*|BOOTSTRAP_ADMIN_PASSWORD=$_pw|" "$ENV_FILE"
    else
      printf 'BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$_pw" >> "$ENV_FILE"
    fi
    ok ".env created from .env.example (INTERNAL_API_KEY and bootstrap password generated)"
    say "   → first-boot login will be: admin / $_pw (change it after logging in)"
  else
    warn "$ENV_FILE is missing and $APP_DIR/.env.example not found — the service will not start"
  fi
else
  ok ".env present ($ENV_FILE) — left untouched"
fi

# .env holds INTERNAL_API_KEY and the first-boot password: keep it private.
if [ -f "$ENV_FILE" ] && command -v stat >/dev/null 2>&1; then
  _mode="$(stat -c %a "$ENV_FILE" 2>/dev/null || echo 600)"
  if [ "$_mode" != "600" ] && [ "$_mode" != "400" ]; then
    chmod 600 "$ENV_FILE" 2>/dev/null && ok ".env tightened to mode 600 (was $_mode)" \
      || warn ".env is mode $_mode — run: chmod 600 $ENV_FILE"
  fi
fi

# Read just the two values this script needs (never `source` .env — an
# unquoted value would abort the install under `set -u`).
env_get() {  # env_get KEY -> value from .env, empty if absent
  [ -f "$ENV_FILE" ] || { printf ''; return; }
  grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true
}

DATABASE_URL="${DATABASE_URL:-$(env_get DATABASE_URL)}"
APP_PORT="${APP_PORT:-$(env_get APP_PORT)}"
APP_PORT="${APP_PORT:-8200}"

[ -n "$DATABASE_URL" ] || die "DATABASE_URL is not set (edit $ENV_FILE) — e.g. postgresql://fazle:SECRET@127.0.0.1:5432/fazle_core"
case "$DATABASE_URL" in
  *replace-me*|*CHANGEME*|*change-me*)
    die "DATABASE_URL still contains the example placeholder — edit $ENV_FILE, then re-run install.sh" ;;
esac
DB_HOST_DISPLAY="$(printf '%s' "$DATABASE_URL" | sed -E 's|.*@||; s|/.*||')"
if [ -z "$DB_HOST_DISPLAY" ]; then
  DB_HOST_DISPLAY="$(printf '%s' "$DATABASE_URL" | sed -nE 's|.*[?&]host=([^&]*).*|\1|p')"
  [ -n "$DB_HOST_DISPLAY" ] || DB_HOST_DISPLAY="unix socket"
fi
ok "DATABASE_URL configured (host: $DB_HOST_DISPLAY)"

mkdir -p "$APP_DIR/logs"
# systemd appends its stdout/stderr here as $RUN_USER; a sudo-ed install would
# otherwise leave the directory owned by root and the service could not write.
if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != "root" ]; then
  chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/logs"
fi

# ── 3. Preflight: port, database, extension ─────────────────────────────────
say ""
say "=== Preflight ==="

# 3a. Port. This install must not steal the port of an already-running app or a
#     WhatsApp bridge; it only needs its own free port on 127.0.0.1.
port_busy=""
if command -v ss >/dev/null 2>&1; then
  port_busy="$(ss -ltnp 2>/dev/null | awk -v p=":$APP_PORT\$" '$4 ~ p {print $0}' | head -1 || true)"
elif command -v lsof >/dev/null 2>&1; then
  port_busy="$(lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1 || true)"
fi
if [ -n "$port_busy" ]; then
  # A unit that is already ours (re-install) may be the listener — that is fine.
  if [ -f "/etc/systemd/system/$UNIT_NAME.service" ] && \
     systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
    warn "port $APP_PORT is in use by the running $UNIT_NAME unit (re-install) — it will be restarted"
  else
    warn "port $APP_PORT is already in use: $port_busy"
    die  "choose another port (edit APP_PORT in $ENV_FILE, e.g. APP_PORT=8210) and re-run"
  fi
else
  ok "port $APP_PORT is free (127.0.0.1)"
fi

# 3b. Database reachability + pg_trgm. Migration 029 runs CREATE EXTENSION
#     pg_trgm, which needs a superuser on some clusters — check before applying
#     so a shared Postgres is never left half-migrated.
DB_STATUS=0
if [ "${SKIP_DB_CHECKS:-0}" = "1" ]; then
  warn "SKIP_DB_CHECKS=1 — skipping database preflight"
elif [ -x "$VENV_DIR/bin/python" ]; then
  DATABASE_URL="$DATABASE_URL" "$VENV_DIR/bin/python" - <<'PY' || DB_STATUS=$?
import asyncio, os, sys

async def main() -> int:
    import asyncpg
    url = os.environ["DATABASE_URL"]
    try:
        conn = await asyncpg.connect(url, timeout=8)
    except Exception as exc:
        print(f"!! cannot connect to the database: {exc}", file=sys.stderr)
        print("   Is Postgres running? Is the password right? (SKIP_DB_CHECKS=1 to bypass)",
              file=sys.stderr)
        return 1
    try:
        installed = await conn.fetchval(
            "SELECT count(*) FROM pg_extension WHERE extname='pg_trgm'")
        available = await conn.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name='pg_trgm'")
        if installed:
            print("✓ pg_trgm extension already installed")
        elif available:
            print("!! pg_trgm is available but not installed — migration 029 will try to "
                  "create it; if that fails run as a superuser: CREATE EXTENSION pg_trgm;")
        else:
            print("!! pg_trgm is NOT available on this server — migration 029 will fail; "
                  "install the postgresql-contrib package first", file=sys.stderr)
            return 1
        version = await conn.fetchval("SHOW server_version")
        print(f"✓ database reachable (PostgreSQL {version})")
        applied = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        if applied:
            n = await conn.fetchval("SELECT count(*) FROM schema_migrations")
            print(f"✓ schema_migrations present ({n} migration(s) already applied)")
        else:
            print("✓ fresh database (no schema_migrations yet)")
        virgin = await conn.fetchval(
            "SELECT to_regclass('public.fpe_employees') IS NULL")
        if virgin:
            print("✓ virgin database — the installer will build the full ordered "
                  "schema (payroll → escort → db/migrations)")
        else:
            print("✓ payroll schema already present — only db/migrations will run")
        return 0
    finally:
        await conn.close()

sys.exit(asyncio.run(main()))
PY
else
  warn "no venv python available — database preflight skipped"
fi
if [ "$DB_STATUS" -ne 0 ]; then
  die "database preflight failed — fix the above (or SKIP_DB_CHECKS=1 bash install.sh)"
fi

if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
  say ""
  ok "preflight complete — nothing installed, no service touched (run 'bash install.sh' to install)"
  exit 0
fi

# ── 4. Schema ───────────────────────────────────────────────────────────────
# Three ordered sets, not one: the payroll-engine tables and the two standalone
# escort schemas are prerequisites for db/migrations (030 indexes fpe_employees,
# 046 references it, 024 references escort_order_groups). scripts/apply_schema.py
# applies them in order on a virgin database and records every file in the same
# schema_migrations table db/migrate.py uses. On a database that already has the
# payroll schema only db/migrations run — the data-bearing prerequisite files are
# never re-applied behind an operator's back.
say ""
say "=== Schema (payroll → escort → db/migrations, tracked in schema_migrations) ==="
SCHEMA_RC=0
DATABASE_URL="$DATABASE_URL" "$VENV_DIR/bin/python" "$APP_DIR/scripts/apply_schema.py" || SCHEMA_RC=$?
if [ "$SCHEMA_RC" -eq 3 ]; then
  warn "the schema is INCOMPLETE: some db/migrations files need objects that only"
  warn "exist in the original production database (see the list above)."
  warn "Recommended: restore a dump of that database instead of building from scratch,"
  warn "or create the missing objects by hand and re-run this script."
  if [ "${ALLOW_INCOMPLETE_SCHEMA:-0}" != "1" ]; then
    die "refusing to install on a half-built schema (ALLOW_INCOMPLETE_SCHEMA=1 overrides)"
  fi
  warn "ALLOW_INCOMPLETE_SCHEMA=1 — continuing anyway; expect errors on affected features"
elif [ "$SCHEMA_RC" -ne 0 ]; then
  die "schema build failed (exit $SCHEMA_RC) — nothing after this step was run"
fi
ok "schema build finished"

# ── 5. systemd unit ─────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/$UNIT_NAME.service" ]; then
  die "$APP_DIR/$UNIT_NAME.service (unit template) not found"
fi

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    "$APP_DIR/$UNIT_NAME.service" > "$RENDERED"

# Refuse to install a unit that still contains an unsubstituted placeholder in a
# directive (comment lines are allowed to talk about them).
if grep -v '^[[:space:]]*#' "$RENDERED" | grep -q '@[A-Z_]\+@'; then
  warn "unsubstituted placeholder left in the unit file:"
  grep -v '^[[:space:]]*#' "$RENDERED" | grep -o '@[A-Z_]\+@' | sort -u >&2
  exit 2
fi

sudo install -m 644 "$RENDERED" "/etc/systemd/system/$UNIT_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME" >/dev/null
ok "unit installed: /etc/systemd/system/$UNIT_NAME.service"

# ── 6. Start + verify ───────────────────────────────────────────────────────
RESTARTS_BEFORE="$(systemctl show -p NRestarts --value "$UNIT_NAME" 2>/dev/null || echo 0)"
sudo systemctl restart "$UNIT_NAME"

BASE_URL="http://127.0.0.1:${APP_PORT}"
HEALTH_URL="$BASE_URL/health"
say "→ waiting for readiness ($HEALTH_URL) …"

if "$PYTHON_BIN" "$APP_DIR/scripts/restart_gate_readiness.py" \
      --service "$UNIT_NAME" \
      --url "$HEALTH_URL" \
      --err-log "$APP_DIR/logs/fazle-core.error.log" \
      --restarts-before "${RESTARTS_BEFORE:-0}" \
      --timeout 90; then
  ok "service installed, started and verified ready"
else
  warn "service did NOT become ready — recent errors:"
  sudo journalctl -u "$UNIT_NAME" -n 40 --no-pager >&2 || true
  warn "unit file: /etc/systemd/system/$UNIT_NAME.service"
  exit 1
fi

say ""
say "=== Done ==="
say "Dashboard: $BASE_URL/"
say "Health:    $HEALTH_URL"
say "Logs:      tail -f $APP_DIR/logs/fazle-core.log"
say "Errors:    tail -f $APP_DIR/logs/fazle-core.error.log"
say "Status:    systemctl status $UNIT_NAME --no-pager"
say ""
say "First login: open the dashboard and use the username/password form."
say "  username: $(grep -E '^BOOTSTRAP_ADMIN_USERNAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || echo admin)"
say "  password: the BOOTSTRAP_ADMIN_PASSWORD in $ENV_FILE, or the one-time"
say "            password the app logged once on first start:"
say "            grep -A3 'FIRST-BOOT ADMIN LOGIN' $APP_DIR/logs/fazle-core.log"
say "  The app marks it as a default and asks you to change it after login."
say "  (Public access still needs a reverse proxy in front of $BASE_URL — see README.)"
