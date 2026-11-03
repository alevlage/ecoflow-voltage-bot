#!/usr/bin/env bash
# One-time server setup for Ubuntu. Run on the server from the unpacked
# project folder:  bash deploy/setup.sh
set -euo pipefail

APP_DIR=/opt/voltage-bot

sudo timedatectl set-timezone Europe/Kyiv  # chart times and 🕒 stamps use local time
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv

sudo mkdir -p "$APP_DIR"
sudo cp -r ./*.py requirements.txt "$APP_DIR"/
sudo chown -R "$USER":"$USER" "$APP_DIR"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

sed "s/__USER__/$USER/" deploy/voltage-bot.service | sudo tee /etc/systemd/system/voltage-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable voltage-bot
echo "Installed. Put .env in $APP_DIR, then: sudo systemctl start voltage-bot"
