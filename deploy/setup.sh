#!/usr/bin/env bash
# deploy/setup.sh — One-command server setup for lead-automation
#
# Tested on: Ubuntu 22.04 LTS, Debian 12
# Run as root:  sudo bash deploy/setup.sh
#
# After setup:
#   1.  cp /path/to/credentials.json  /opt/lead-automation/credentials/credentials.json
#   2.  nano /opt/lead-automation/.env          # fill in your values
#   3.  systemctl start lead-automation
#   4.  journalctl -u lead-automation -f        # tail logs

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/lead-automation}"
APP_USER="${APP_USER:-automation}"
PYTHON="${PYTHON:-python3}"

echo "========================================"
echo " Lead Automation — Server Setup"
echo "========================================"
echo " APP_DIR  = $APP_DIR"
echo " APP_USER = $APP_USER"
echo ""

# ── System dependencies ───────────────────────────────────────────────
echo "==> Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    ca-certificates wget curl gnupg2 \
    logrotate

# ── Application user ─────────────────────────────────────────────────
echo "==> Creating user '$APP_USER'…"
id "$APP_USER" &>/dev/null || useradd -m -s /bin/bash "$APP_USER"

# ── Application directory ────────────────────────────────────────────
echo "==> Deploying application to $APP_DIR…"
mkdir -p "$APP_DIR"

# Copy everything except venv, .env, credentials, generated artefacts
rsync -a --exclude='.git' --exclude='venv/' --exclude='.env' \
    --exclude='credentials/' --exclude='logs/' --exclude='screenshots/' \
    --exclude='__pycache__/' --exclude='*.pyc' \
    . "$APP_DIR/"

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ── Python virtual environment ────────────────────────────────────────
echo "==> Creating Python venv and installing dependencies…"
sudo -u "$APP_USER" bash -c "
    set -euo pipefail
    cd '$APP_DIR'
    $PYTHON -m venv venv
    source venv/bin/activate
    pip install --upgrade pip --quiet
    pip install -r requirements.txt --quiet
    playwright install chromium --with-deps
    mkdir -p logs screenshots credentials
    chmod +x run.sh
"

# ── Systemd service ───────────────────────────────────────────────────
echo "==> Installing systemd service…"
UNIT_SRC="$APP_DIR/deploy/lead-automation.service"
UNIT_DST="/etc/systemd/system/lead-automation.service"

cp "$UNIT_SRC" "$UNIT_DST"
# Patch paths + user in case APP_DIR / APP_USER were overridden
sed -i "s|/opt/lead-automation|$APP_DIR|g" "$UNIT_DST"
sed -i "s|User=automation|User=$APP_USER|g"   "$UNIT_DST"
sed -i "s|Group=automation|Group=$APP_USER|g" "$UNIT_DST"

systemctl daemon-reload
systemctl enable lead-automation.service

# ── Log rotation ──────────────────────────────────────────────────────
echo "==> Installing logrotate config…"
LOGROTATE_DST="/etc/logrotate.d/lead-automation"
cp "$APP_DIR/deploy/logrotate.conf" "$LOGROTATE_DST"
sed -i "s|/opt/lead-automation|$APP_DIR|g" "$LOGROTATE_DST"

echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Copy credentials:"
echo "     cp /path/to/credentials.json $APP_DIR/credentials/credentials.json"
echo "     chown $APP_USER:$APP_USER $APP_DIR/credentials/credentials.json"
echo ""
echo "  2. Configure environment:"
echo "     cp $APP_DIR/.env.example $APP_DIR/.env"
echo "     nano $APP_DIR/.env"
echo ""
echo "  3. Start the service:"
echo "     systemctl start lead-automation"
echo ""
echo "  4. Tail logs:"
echo "     journalctl -u lead-automation -f"
echo ""
