#!/bin/bash
# Fazle Core — Installation & Start Script
# Run once: bash install.sh

set -e
cd /home/azim/fazle-core

echo "=== Fazle Core Install ==="

# Create venv if not exists
if [ ! -d venv ]; then
  python3 -m venv venv
  echo "✓ venv created"
fi

# Install deps
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "✓ packages installed"

# Create logs dir
mkdir -p logs

# Install systemd service
sudo cp fazle-core.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fazle-core
sudo systemctl restart fazle-core
echo "✓ service installed and started"

sleep 2
sudo systemctl status fazle-core --no-pager
curl -s http://localhost:8200/health | python3 -m json.tool || echo "Health check pending..."

echo ""
echo "=== Done ==="
echo "Dashboard: http://localhost:8200/"
echo "Health:    http://localhost:8200/health"
echo "Logs:      tail -f /home/azim/fazle-core/logs/fazle-core.log"
