#!/bin/bash
set -e

# ==============================================================================
# WEEX NAS100 15m ORB Bot Droplet Provisioning Script
# ==============================================================================

echo ">>> [1/5] Updating packages..."
apt-get update -y
apt-get install -y python3-pip python3-venv git curl

echo ">>> [2/5] Creating dedicated service user & directory..."
INSTALL_DIR="/opt/weex-nas100-orb"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo ">>> [3/5] Setting up virtual environment..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
if [ -f requirements.txt ]; then
    ./venv/bin/pip install -r requirements.txt
fi

echo ">>> [4/5] Installing systemd daemon service..."
cat << 'EOF' > /etc/systemd/system/weex-nas100-orb.service
[Unit]
Description=WEEX NAS100 15m ORB Micro-Risk Bot ($1 Risk)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/weex-nas100-orb
ExecStart=/opt/weex-nas100-orb/venv/bin/python3 /opt/weex-nas100-orb/bot.py
Restart=always
RestartSec=10
EnvironmentFile=-/opt/weex-nas100-orb/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo ">>> [5/5] Setup Complete!"
echo "------------------------------------------------------------------"
echo "NEXT STEPS:"
echo "1. Configure your credentials:"
echo "   cp config.example.json config.json"
echo "   nano config.json"
echo "   (or create a .env file with WEEX_API_KEY, TELEGRAM_BOT_TOKEN, etc.)"
echo ""
echo "2. Start the background service:"
echo "   systemctl enable --now weex-nas100-orb"
echo ""
echo "3. Monitor live execution & Telegram alerts:"
echo "   journalctl -u weex-nas100-orb -f"
echo "------------------------------------------------------------------"
