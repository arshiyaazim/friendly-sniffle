#!/bin/bash
# Fazle Core — installation & start script (VPS / systemd hosts)
#
#   bash install.sh
#
# Creates the venv, installs requirements.txt, renders fazle-core.service for
# THIS checkout's path, installs and starts the unit, then waits for the
# restart gate to confirm the service is actually ready.
#
# Overridable: APP_DIR (defaults to this script's directory), RUN_USER
# (defaults to $SUDO_USER, else the current user), PYTHON_BIN, UNIT_NAME.
#
# For anything beyond a single-host install, follow MASTER_PRODUCTION_PLAN.md.

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
UNIT_NAME="${UNIT_NAME:-fazle-core}"
VENV_DIR="$APP_DIR/venv"

echo "=== Fazle Core Install ==="
echo "  app dir : $APP_DIR"
echo "  run user: $RUN_USER"

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "ERROR: $APP_DIR/requirements.txt not found — is APP_DIR correct?" >&2
  exit 2
fi

# ── Python environment ───────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  echo "✓ venv created ($VENV_DIR)"
fi

"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "✓ packages installed"

mkdir -p "$APP_DIR/logs"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "!! $APP_DIR/.env is missing."
  echo "   Copy $APP_DIR/.env.example to .env and fill in at least DATABASE_URL"
  echo "   before the service can start."
fi

# ── systemd unit ────────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/$UNIT_NAME.service" ]; then
  echo "ERROR: $APP_DIR/$UNIT_NAME.service (unit template) not found." >&2
  exit 2
fi

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@RUN_USER@|$RUN_USER|g" \
    "$APP_DIR/$UNIT_NAME.service" > "$RENDERED"

# Refuse to install a unit that still contains an unsubstited placeholder in a
# directive (comment lines are allowed to talk about them).
if grep -v '^[[:space:]]*#' "$RENDERED" | grep -q '@[A-Z_]\+@'; then
  echo "ERROR: unsubstituted placeholder left in the unit file:" >&2
  grep -v '^[[:space:]]*#' "$RENDERED" | grep -o '@[A-Z_]\+@' | sort -u >&2
  exit 2
fi

sudo install -m 644 "$RENDERED" "/etc/systemd/system/$UNIT_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME" >/dev/null
echo "✓ unit installed: /etc/systemd/system/$UNIT_NAME.service"

# ── Start + verify ──────────────────────────────────────────────────────────
RESTARTS_BEFORE="$(systemctl show -p NRestarts --value "$UNIT_NAME" 2>/dev/null || echo 0)"
sudo systemctl restart "$UNIT_NAME"

BASE_URL="http://127.0.0.1:${APP_PORT:-8200}"
HEALTH_URL="$BASE_URL/health"
echo "→ waiting for readiness ($HEALTH_URL) …"

if "$PYTHON_BIN" "$APP_DIR/scripts/restart_gate_readiness.py" \
      --service "$UNIT_NAME" \
      --url "$HEALTH_URL" \
      --err-log "$APP_DIR/logs/fazle-core.error.log" \
      --restarts-before "${RESTARTS_BEFORE:-0}" \
      --timeout 90; then
  echo "✓ service installed, started and verified ready"
else
  echo "✗ service did NOT become ready — recent errors:" >&2
  sudo journalctl -u "$UNIT_NAME" -n 40 --no-pager >&2 || true
  echo "  unit file: /etc/systemd/system/$UNIT_NAME.service" >&2
  exit 1
fi

echo ""
echo "=== Done ==="
echo "Dashboard: $BASE_URL/"
echo "Health:    $HEALTH_URL"
echo "Logs:      tail -f $APP_DIR/logs/fazle-core.log"
echo "Errors:    tail -f $APP_DIR/logs/fazle-core.error.log"
echo "Status:    systemctl status $UNIT_NAME --no-pager"
