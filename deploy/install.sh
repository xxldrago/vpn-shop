#!/usr/bin/env bash
#
# ============================================================================
#  VpnShop + Amnezia Web Panel — one-shot VPS setup for Ubuntu 20.04/22.04/24.04
# ============================================================================
#  Run as root on a FRESH Ubuntu VPS.
#
#  What it does:
#    1. Update system, install: docker, docker-compose plugin, nginx,
#       certbot, git, python3-venv
#    2. Install Amnezia Web Panel (systemd service on 127.0.0.1:5000)
#    3. Build & start the VpnShop web shop + Telegram bot from /opt/vpn-shop
#       (Docker, SQLite volume)
#    4. Configure nginx + Let's Encrypt HTTPS for the shop + Platega webhook
#
#  PREREQUISITES before running:
#    - Your domain A-record already points to this VPS IP.
#    - The shop repo is already on the server at /opt/vpn-shop
#      (scp/rsync it there first, or git clone it into place).
#
#  USAGE:
#    sudo bash install.sh vpn.example.com
# ============================================================================
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "Usage: sudo bash $0 <your-domain.com>"
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo)."
  exit 1
fi

SHOP_DIR="/opt/vpn-shop"
PANEL_DIR="/opt/Amnezia-Web-Panel"
PANEL_GIT="https://github.com/PRVTPRO/Amnezia-Web-Panel.git"
PANEL_PORT=5000
SHOP_PORT=8080

echo "==> [1/6] Updating system & installing dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

# Docker
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
# docker compose plugin
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y docker-compose-plugin
fi

apt-get install -y nginx certbot python3-certbot-nginx git python3-venv python3-pip

echo "==> [2/6] Installing Amnezia Web Panel"
if [ ! -d "$PANEL_DIR" ]; then
  git clone "$PANEL_GIT" "$PANEL_DIR"
else
  echo "    Panel already present, skipping clone."
fi
if [ ! -d "$PANEL_DIR/venv" ]; then
  (cd "$PANEL_DIR" && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt)
fi
# Panel env (optional SECRET_KEY)
if [ ! -f "$PANEL_DIR/.env" ]; then
  echo "SECRET_KEY=$(openssl rand -hex 32)" > "$PANEL_DIR/.env"
fi

echo "==> [3/6] Registering systemd service for the panel (from repo deploy folder)"
PANELE_FILE="$SHOP_DIR/deploy/systemd/amnezia-panel.service"
if [ -f "$PANELE_FILE" ]; then
  cp "$PANELE_FILE" /etc/systemd/system/amnezia-panel.service
  # point WorkingDirectory at the actual panel dir
  sed -i "s|WorkingDirectory=/opt/Amnezia-Web-Panel|WorkingDirectory=$PANEL_DIR|" /etc/systemd/system/amnezia-panel.service
  sed -i "s|EnvironmentFile=/opt/Amnezia-Web-Panel/.env|EnvironmentFile=$PANEL_DIR/.env|" /etc/systemd/system/amnezia-panel.service
  sed -i "s|ExecStart=/opt/Amnezia-Web-Panel/venv/bin/python app.py|ExecStart=$PANEL_DIR/venv/bin/python app.py|" /etc/systemd/system/amnezia-panel.service
  systemctl daemon-reload
  systemctl enable --now amnezia-panel
  echo "    Panel started: http://$(hostname -I | awk '{print $1}'):$PANEL_PORT  (admin/admin)"
else
  echo "    !! deploy/systemd/amnezia-panel.service not found; panel NOT started."
fi

echo "==> [4/6] Preparing VpnShop (Docker)"
if [ ! -d "$SHOP_DIR" ]; then
  echo "    !! $SHOP_DIR not found. Copy the repo here first, then re-run."
  exit 1
fi
cd "$SHOP_DIR"
ENV_FILE="$SHOP_DIR/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp deploy/.env.example "$ENV_FILE"
  echo "    Created $ENV_FILE — YOU MUST EDIT IT (panel token, platega, secret key, domain, telegram token)."
fi

echo "==> [5/6] Building & starting shop + bot"
docker compose -f deploy/docker-compose.yml up -d --build

echo "==> [6/6] nginx + Let's Encrypt"
# remove default site, add our config with the domain
rm -f /etc/nginx/sites-enabled/default
sed "s/__DOMAIN__/$DOMAIN/g" "$SHOP_DIR/deploy/nginx/vpnshop.conf" \
  > /etc/nginx/sites-available/vpnshop
ln -sf /etc/nginx/sites-available/vpnshop /etc/nginx/sites-enabled/vpnshop
nginx -t
systemctl reload nginx

# Issue & install HTTPS
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
  -m "admin@$DOMAIN" --redirect

echo
echo "========================  DONE  ========================"
echo "  Amnezia Panel : http://$(hostname -I | awk '{print $1}'):$PANEL_PORT  (admin/admin — CHANGE IT)"
echo "  VpnShop       : https://$DOMAIN"
echo "  Platega webhook: https://$DOMAIN/webhook/callback"
echo
echo "  NEXT STEPS:"
echo "    1. Edit $ENV_FILE and set: PANEL_TOKEN, PLATEGA_MERCHANT_ID,"
echo "       PLATEGA_SECRET, SHOP_PUBLIC_URL, SHOP_SECRET_KEY, TELEGRAM_BOT_TOKEN,"
echo "       then:  cd $SHOP_DIR && docker compose -f deploy/docker-compose.yml up -d"
echo "    2. In the panel: create an API token (Settings -> API Tokens) and"
echo "       add your VPN server(s) (Servers)."
echo "    3. Finish config in the shop admin UI: Admin -> Settings"
echo "       (panel url/token, shop url, Platega keys)."
echo "    4. Open the shop admin (https://$DOMAIN/admin, admin/admin — CHANGE IT)."
echo "    5. Set Telegram bot token either in the admin UI or in deploy/.env."
echo "========================================================"
