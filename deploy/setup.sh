#!/usr/bin/env bash
# One-time server setup for Ubuntu. Run on the server from the unpacked
# project folder:  bash deploy/setup.sh
set -euo pipefail

APP_DIR=/opt/voltage-bot

sudo timedatectl set-timezone Europe/Kyiv  # chart times and 🕒 stamps use local time
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv

sudo mkdir -p "$APP_DIR"
sudo cp -r ./*.py requirements.txt static "$APP_DIR"/
sudo chown -R "$USER":"$USER" "$APP_DIR"

# HTTPS for the live page (Telegram requires it). Caddy gets a free
# certificate by itself. The domain is the server's IP via sslip.io, e.g.
# 9-205-29-100.sslip.io, unless WEBAPP_DOMAIN is set.
IP=$(curl -s https://api.ipify.org)
DOMAIN=${WEBAPP_DOMAIN:-${IP//./-}.sslip.io}
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q caddy
sed "s/__DOMAIN__/$DOMAIN/" deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload-or-restart caddy
echo "Live page: https://$DOMAIN/  (set WEBAPP_URL to this in .env)"

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

sed "s/__USER__/$USER/" deploy/voltage-bot.service | sudo tee /etc/systemd/system/voltage-bot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable voltage-bot
echo "Installed. Put .env in $APP_DIR, then: sudo systemctl start voltage-bot"
