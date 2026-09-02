#!/usr/bin/env bash
#
# ============================================================================
#  VpnShop + Amnezia Web Panel — one-shot VPS setup for Ubuntu 20.04/22.04/24.04
# ============================================================================
#  Run as root on a FRESH Ubuntu VPS.
#
#  Two public subdomains, SAME VPS:
#    <panel-domain>  -> Amnezia Web Panel  (nginx -> 127.0.0.1:5000)
#    <shop-domain>   -> VpnShop web shop + Platega webhook (nginx -> 127.0.0.1:8080)
#
#  What it does:
#    1. Update system, install: docker, docker-compose plugin, nginx,
#       certbot, git, python3-venv
#    2. Install Amnezia Web Panel (systemd service on 127.0.0.1:5000)
#    3. Build & start the VpnShop web shop + Telegram bot (Docker, SQLite volume)
#    4. Configure nginx + Let's Encrypt HTTPS for BOTH subdomains
#
#  PREREQUISITES before running:
#    - DNS A-records for both subdomains already point to this VPS IP.
#    - The shop repo is already cloned on the server at /opt/vpn-shop.
#
#  USAGE:
#    sudo bash install.sh <shop-domain> <panel-domain>
#    e.g. sudo bash install.sh my.3set.online panel.3set.online
# ============================================================================
set -euo pipefail

SHOP_DOMAIN="${1:-}"
PANEL_DOMAIN="${2:-}"
if [ -z "$SHOP_DOMAIN" ] || [ -z "$PANEL_DOMAIN" ]; then
  echo "Usage: sudo bash $0 <shop-domain> <panel-domain>"
  echo "  e.g. sudo bash install.sh my.3set.online panel.3set.online"
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

echo "==> [3/6] Registering systemd service for the panel"
PANELE_FILE="$SHOP_DIR/deploy/systemd/amnezia-panel.service"
if [ -f "$PANELE_FILE" ]; then
  cp "$PANELE_FILE" /etc/systemd/system/amnezia-panel.service
  # point WorkingDirectory/EnvironmentFile/ExecStart at the actual panel dir
  sed -i "s|WorkingDirectory=/opt/Amnezia-Web-Panel|WorkingDirectory=$PANEL_DIR|" /etc/systemd/system/amnezia-panel.service
  sed -i "s|EnvironmentFile=/opt/Amnezia-Web-Panel/.env|EnvironmentFile=$PANEL_DIR/.env|" /etc/systemd/system/amnezia-panel.service
  sed -i "s|ExecStart=/opt/Amnezia-Web-Panel/venv/bin/python app.py|ExecStart=$PANEL_DIR/venv/bin/python app.py|" /etc/systemd/system/amnezia-panel.service
  systemctl daemon-reload
  systemctl enable --now amnezia-panel
  echo "    Panel running on 127.0.0.1:$PANEL_PORT (admin/admin)"
else
  echo "    !! deploy/systemd/amnezia-panel.service not found; panel NOT started."
fi

echo "==> [4/6] Preparing VpnShop (Docker)"
if [ ! -d "$SHOP_DIR" ]; then
  echo "    !! $SHOP_DIR not found. Clone the repo first:"
  echo "        cd /opt && git clone https://github.com/xxldrago/vpn-shop.git"
  exit 1
fi
cd "$SHOP_DIR"
ENV_FILE="$SHOP_DIR/deploy/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp deploy/.env.example "$ENV_FILE"
  echo "    Created $ENV_FILE — YOU MUST EDIT IT (panel token, platega, secret key, shop url, telegram token)."
fi

echo "==> [5/6] Building & starting shop + bot"
docker compose -f deploy/docker-compose.yml up -d --build

echo "==> [6/6] nginx + Let's Encrypt for both subdomains"
rm -f /etc/nginx/sites-enabled/default

# Shop site
sed "s/__DOMAIN__/$SHOP_DOMAIN/g" "$SHOP_DIR/deploy/nginx/vpnshop.conf" \
  > /etc/nginx/sites-available/vpnshop
ln -sf /etc/nginx/sites-available/vpnshop /etc/nginx/sites-enabled/vpnshop

# Panel site
sed "s/__DOMAIN__/$PANEL_DOMAIN/g" "$SHOP_DIR/deploy/nginx/panel.conf" \
  > /etc/nginx/sites-available/panel
ln -sf /etc/nginx/sites-available/panel /etc/nginx/sites-enabled/panel

nginx -t
systemctl reload nginx

# Issue & install HTTPS for both domains (single certbot call with -d flags)
certbot --nginx -d "$SHOP_DOMAIN" -d "$PANEL_DOMAIN" \
  --non-interactive --agree-tos -m "admin@$SHOP_DOMAIN" --redirect

echo
echo "========================  DONE  ========================"
echo "  Amnezia Panel : https://$PANEL_DOMAIN  (admin/admin — CHANGE IT)"
echo "  VpnShop       : https://$SHOP_DOMAIN"
echo "  Platega webhook: https://$SHOP_DOMAIN/webhook/callback"
echo
echo "  NEXT STEPS:"
echo "    1. Edit $ENV_FILE and set: PANEL_TOKEN, PLATEGA_MERCHANT_ID,"
echo "       PLATEGA_SECRET, SHOP_PUBLIC_URL=$SHOP_DOMAIN,"
echo "       SHOP_SECRET_KEY, TELEGRAM_BOT_TOKEN;"
echo "       then:  cd $SHOP_DIR && docker compose -f deploy/docker-compose.yml up -d"
echo "    2. In the panel ($PANEL_DOMAIN): create an API token"
echo "       (Settings -> API Tokens) and add your VPN server(s) (Servers)."
echo "    3. In the shop admin ($SHOP_DOMAIN/admin, admin/admin — CHANGE IT):"
echo "       set panel url=http://127.0.0.1:$PANEL_PORT, panel token,"
echo "       shop public url=https://$SHOP_DOMAIN, Platega keys, bot token."
echo "========================================================"
