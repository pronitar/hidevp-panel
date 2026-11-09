#!/usr/bin/env bash
set -euo pipefail
HV_LANG="${HV_LANG:-$(cat /opt/hidevp-panel/lang 2>/dev/null || echo ru)}"
m() { [ "$HV_LANG" = en ] && printf '%s' "$2" || printf '%s' "$1"; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/hidevp-panel/master
NGINX_SITE=hidevp-panel
DOMAIN="${DOMAIN:-}"

[ "$(id -u)" -eq 0 ] || { echo "$(m "Запустите от root: sudo bash $0" "Run as root: sudo bash $0")"; exit 1; }
command -v apt-get >/dev/null || { echo "$(m "Нужна Ubuntu 22.04+/Debian 11+" "Ubuntu 22.04+/Debian 11+ required")"; exit 1; }

say()  { echo -e "\n>>> $*"; }
die()  { echo -e "\n!!! $*" >&2; exit 1; }
port_busy() { ss -ltnH "( sport = :$1 )" | grep -q .; }
free_port() { local p; while :; do p=$(shuf -i 20000-60000 -n 1); port_busy "$p" || { echo "$p"; return; }; done; }

say "$(m "Установка пакетов" "Installing packages")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "!!! $(m "apt-get update завершился с ошибкой (сломанный сторонний репозиторий?) — продолжаю" "apt-get update failed (broken third-party repo?) — continuing")"
apt-get install -y -qq python3 python3-venv python3-pip curl openssl iproute2 dnsutils >/dev/null

. "$SRC/ports.sh"
NODE_STATE=/opt/hidevp-panel/node/data/state.json
vpn_uses_port() { [ -f "$NODE_STATE" ] && grep -q "\"port\": *$1[,}]" "$NODE_STATE"; }
port_busy_or_vpn() { port_busy "$1" || vpn_uses_port "$1"; }

PUBLIC_IP=$(curl -4 -s --max-time 8 https://api.ipify.org || true)
[ -n "$PUBLIC_IP" ] || PUBLIC_IP=$(hostname -I | awk '{print $1}')

say "$(m "Копирование файлов в" "Copying files to") $DEST"
mkdir -p "$DEST"
RELKEY=""; [ -f "$DEST/app/release_key.pub" ] && RELKEY=$(cat "$DEST/app/release_key.pub")
rm -rf "$DEST/app" "$DEST/static"
cp -r "$SRC/master/app" "$SRC/master/static" "$SRC/master/requirements.txt" "$DEST/"
[ -n "$RELKEY" ] && [ ! -f "$DEST/app/release_key.pub" ] && echo "$RELKEY" > "$DEST/app/release_key.pub"
[ -d "$DEST/venv" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

cat > /usr/local/bin/hidevp-panel <<EOF
#!/bin/bash
cd $DEST && PANEL_HOME=$DEST exec $DEST/venv/bin/python -m app.cli "\$@"
EOF
chmod +x /usr/local/bin/hidevp-panel

setting() {
  "$DEST/venv/bin/python" -c "import sqlite3,sys
try:
    r=sqlite3.connect('$DEST/data/panel.db').execute('SELECT value FROM settings WHERE key=?',(sys.argv[1],)).fetchone()
    print(r[0] if r else '')
except Exception: print('')" "$1"
}
ADMINS=0
if [ -f "$DEST/data/panel.db" ]; then
  ADMINS=$("$DEST/venv/bin/python" -c "import sqlite3;print(sqlite3.connect('$DEST/data/panel.db').execute('SELECT COUNT(*) FROM admins').fetchone()[0])" 2>/dev/null || echo 0)
fi
FIRST=1; [ "$ADMINS" != "0" ] && FIRST=0

if [ -z "$DOMAIN" ] && [ -t 0 ]; then
  echo
  if [ "$FIRST" = 1 ]; then
    echo "$(m "Укажите домен для панели (например, hidevp.ru)." "Enter a domain for the panel (e.g. example.com).")"
    echo "$(m "A-запись домена должна указывать на этот сервер:" "The domain's A record must point to this server:") $PUBLIC_IP"
    read -rp "$(m "Домен (Enter — без домена, доступ по http://IP:порт): " "Domain (Enter — no domain, access via http://IP:port): ")" DOMAIN
  else
    echo "$(m "Текущий адрес:" "Current URL:") $(setting public_url)"
    read -rp "$(m "Новый домен (Enter — оставить как есть): " "New domain (Enter — keep as is): ")" DOMAIN
  fi
fi
DOMAIN=$(echo "${DOMAIN}" | tr 'A-Z' 'a-z' | sed -e 's#^https\?://##' -e 's#/.*##' -e 's/[[:space:]]//g')
[ "$DOMAIN" = "none" ] && DOMAIN=""

if [ -n "$DOMAIN" ]; then
  echo "$DOMAIN" | grep -qE '^([a-z0-9-]+\.)+[a-z]{2,}$' || die "$(m "Некорректный домен:" "Invalid domain:") $DOMAIN"
  RESOLVED=$(dig +short A "$DOMAIN" | tail -1)
  if [ "$RESOLVED" != "$PUBLIC_IP" ]; then
    die "$(m "Домен $DOMAIN указывает на '${RESOLVED:-ничего}', а IP этого сервера — $PUBLIC_IP.
    Создайте A-запись $DOMAIN → $PUBLIC_IP, подождите 5–15 минут и запустите установку снова." "Domain $DOMAIN points to '${RESOLVED:-nothing}', but this server's IP is $PUBLIC_IP.
    Create an A record $DOMAIN → $PUBLIC_IP, wait 5–15 minutes and run the installer again.")"
  fi
  WWW=""
  [ "$(dig +short A "www.$DOMAIN" | tail -1)" = "$PUBLIC_IP" ] && WWW="www.$DOMAIN"
fi

cat > /etc/systemd/system/hidevp-panel.service <<EOF
[Unit]
Description=HideVP Panel (master)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DEST
Environment=PANEL_HOME=$DEST
ExecStart=$DEST/venv/bin/python -m app.run
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable hidevp-panel >/dev/null 2>&1

open_port() { if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$1"/tcp >/dev/null; fi; }

setup_nginx() {
  local internal=$1 names="$DOMAIN${WWW:+ $WWW}" re conflicts
  re=$(echo "$DOMAIN" | sed 's/\./\\./g')
  conflicts=$(grep -RPl "^\s*server_name\s[^;]*(?<![\w.-])(www\.)?$re(?![\w.-])" /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null \
              | grep -v "/$NGINX_SITE" || true)
  if [ -n "$conflicts" ]; then
    echo "!!! $(m "Домен $DOMAIN уже используется в других сайтах nginx:" "Domain $DOMAIN is already used by other nginx sites:")"
    for f in $conflicts; do echo "      $(readlink -f "$f")"; grep -nP "server_name\s[^;]*$re" "$f" | sed "s/^/         $(m строка line) /"; done
    die "$(m "Удалите (или закомментируйте) блоки server { ... } с server_name $DOMAIN в этих файлах,
    проверьте 'nginx -t && systemctl reload nginx' и запустите установку снова." "Remove (or comment out) the server { ... } blocks with server_name $DOMAIN in these files,
    check 'nginx -t && systemctl reload nginx' and run the installer again.")"
  fi
  say "$(m "Настройка nginx для" "Configuring nginx for") $names"
  command -v certbot >/dev/null || apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  dpkg -s python3-certbot-nginx >/dev/null 2>&1 || apt-get install -y -qq python3-certbot-nginx >/dev/null
  local avail=/etc/nginx/sites-available enabled=/etc/nginx/sites-enabled
  [ -d "$avail" ] || { avail=/etc/nginx/conf.d; enabled=""; }
  cat > "$avail/$NGINX_SITE.conf" <<EOF
# HideVP Panel — создано install-master.sh
server {
    listen 80;
    listen [::]:80;
    server_name $names;
    client_max_body_size 100m;   # загрузка резервной копии

    location / {
        proxy_pass http://127.0.0.1:$internal;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
  [ -n "$enabled" ] && ln -sf "$avail/$NGINX_SITE.conf" "$enabled/$NGINX_SITE.conf"
  local test; test=$(nginx -t 2>&1) || { echo "$test"; die "$(m "nginx -t завершился с ошибкой" "nginx -t failed")"; }
  if echo "$test" | grep -qi "conflicting server name"; then
    echo "$test" | grep -i "conflicting"
    die "$(m "Домен $DOMAIN объявлен в nginx несколько раз. Удалите старые блоки server с этим server_name и запустите установку снова." "Domain $DOMAIN is declared in nginx several times. Remove the old server blocks with this server_name and run again.")"
  fi
  systemctl reload nginx
  say "$(m "Выпуск сертификата Let's Encrypt" "Issuing a Let's Encrypt certificate")"
  local dargs=(-d "$DOMAIN"); [ -n "$WWW" ] && dargs+=(-d "$WWW")
  certbot --nginx "${dargs[@]}" --non-interactive --agree-tos --register-unsafely-without-email \
          --redirect --keep-until-expiring \
    || die "$(m "certbot не смог выпустить сертификат. Проверьте, что порт 80 открыт и $DOMAIN указывает на $PUBLIC_IP." "certbot could not issue a certificate. Check that port 80 is open and $DOMAIN points to $PUBLIC_IP.")"
  systemctl reload nginx
  open_port 80; open_port 443
}

setup_caddy() {
  local internal=$1 pub=$2 site
  say "$(m "Установка Caddy (HTTPS для $DOMAIN)" "Installing Caddy (HTTPS for $DOMAIN)")"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
  site="$DOMAIN"; [ -n "$WWW" ] && site="$DOMAIN, $WWW"
  [ "$pub" != 443 ] && site="$DOMAIN:$pub"
  cat > /etc/caddy/hidevp-panel.caddy <<EOF
$site {
    reverse_proxy 127.0.0.1:$internal
}
EOF
  if [ ! -f /etc/caddy/Caddyfile ] || grep -q "/usr/share/caddy" /etc/caddy/Caddyfile; then
    echo "import /etc/caddy/hidevp-panel.caddy" > /etc/caddy/Caddyfile
  elif ! grep -q "hidevp-panel.caddy" /etc/caddy/Caddyfile; then
    echo "import /etc/caddy/hidevp-panel.caddy" >> /etc/caddy/Caddyfile
  fi
  systemctl enable caddy >/dev/null 2>&1
  systemctl restart caddy
  open_port 80; open_port "$pub"
}

INIT_ARGS=()
if [ -n "$DOMAIN" ]; then
  INTERNAL=$(setting web_port); [ -n "$INTERNAL" ] || INTERNAL=$(free_port)
  OWN443=$(port_owner 443)
  if systemctl is-active --quiet nginx; then
    if { [ -n "$OWN443" ] && [ "$OWN443" != "nginx" ]; } || { [ -z "$OWN443" ] && vpn_uses_port 443; }; then
      die "$(m "Порт 443 на этом сервере занят (${OWN443:-VPN этой панели}), а сайту панели через nginx нужен 443.
    Перенесите VPN этого сервера на другой порт (админка → VPN, например 2053) или остановите программу, затем повторите." "Port 443 on this server is taken (${OWN443:-the VPN of this panel}), but the panel site via nginx needs 443.
    Move this server's VPN to another port (admin → VPN, e.g. 2053) or stop that program, then run again.")"
    fi
    setup_nginx "$INTERNAL"
    PUBLIC_URL="https://$DOMAIN"
  elif ! port_busy 80 && ! port_busy_or_vpn 443; then
    setup_caddy "$INTERNAL" 443
    PUBLIC_URL="https://$DOMAIN"
  elif ! port_busy 80; then
    PUB=$(free_port)
    echo "!!! $(m "Порт 443 занят (${OWN443:-VPN этой панели}). Сайт будет на" "Port 443 is busy (${OWN443:-the VPN of this panel}). The site will be at") https://$DOMAIN:$PUB"
    setup_caddy "$INTERNAL" "$PUB"
    PUBLIC_URL="https://$DOMAIN:$PUB"
  else
    die "$(m "Порты 80 и 443 заняты не nginx'ом — не могу получить сертификат. Освободите порт 80 или установите без домена." "Ports 80 and 443 are used by something other than nginx — cannot get a certificate. Free port 80 or install without a domain.")"
  fi
  INIT_ARGS=(--port "$INTERNAL" --listen 127.0.0.1 --public-url "$PUBLIC_URL")
  [[ "$PUBLIC_URL" == *":"[0-9]* ]] || SITE_ON_443=1
elif [ "$FIRST" = 1 ]; then
  PUB=$(free_port)
  open_port "$PUB"
  INIT_ARGS=(--port "$PUB" --listen 0.0.0.0 --public-url "http://$PUBLIC_IP:$PUB")
fi

echo
if [ ${#INIT_ARGS[@]} -gt 0 ]; then
  hidevp-panel init "${INIT_ARGS[@]}" --lang "$HV_LANG"
fi
chmod 700 "$DEST/data" 2>/dev/null || true
chmod 600 "$DEST"/data/panel.db* 2>/dev/null || true
[ -d "$DEST/data/backups" ] && chmod -R go-rwx "$DEST/data/backups"
systemctl restart hidevp-panel
sleep 2
systemctl is-active --quiet hidevp-panel || { journalctl -u hidevp-panel -n 30 --no-pager; die "$(m "Панель не запустилась (лог выше)." "The panel failed to start (log above).")"; }

mkdir -p /opt/hidevp-panel; echo "$HV_LANG" > /opt/hidevp-panel/lang
STORE=/opt/hidevp-panel/src
if [ "$(readlink -f "$SRC")" != "$STORE" ]; then
  OLDV=$(cat "$STORE/VERSION" 2>/dev/null || true); NEWV=$(cat "$SRC/VERSION" 2>/dev/null || true)
  if [ -n "$OLDV" ] && [ "$OLDV" != "$NEWV" ] && [ -f "$STORE/install-master.sh" ]; then
    mkdir -p /opt/hidevp-panel/rollback && chmod 700 /opt/hidevp-panel/rollback
    tar -czf "/opt/hidevp-panel/rollback/hidevp-panel-$OLDV.tar.gz" -C /opt/hidevp-panel --exclude=__pycache__ \
      --transform 's,^src,hidevp-panel,' src 2>/dev/null || true
    ls -1t /opt/hidevp-panel/rollback/hidevp-panel-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
  fi
  rm -rf "$STORE"; mkdir -p "$STORE"
  cp -r "$SRC/install-master.sh" "$SRC/install-node.sh" "$SRC/hidevp.sh" "$SRC/ports.sh" "$SRC/master" "$SRC/node" "$STORE/"
  cp "$SRC/get.sh" "$SRC/verify_release.py" "$STORE/" 2>/dev/null || true
  cp "$SRC/VERSION" "$SRC/README.md" "$STORE/" 2>/dev/null || true
  find "$STORE" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
fi
chmod +x "$STORE/hidevp.sh"
ln -sf "$STORE/hidevp.sh" /usr/local/bin/hidevp

echo
if [ "$FIRST" = 1 ]; then
  echo "  $(m "Master-панель установлена. Откройте ссылку выше и войдите." "Master panel installed. Open the link above and sign in.")"
else
  echo "  $(m "Master-панель обновлена. Данные сохранены." "Master panel updated. Data preserved.")"
  hidevp-panel info
fi
echo "  $(m "Сайт для пользователей (вход по номеру):" "User website (phone login):") $(setting public_url)/"
if [ "${SITE_ON_443:-}" = 1 ] || port_owner 443 | grep -qE "nginx|caddy"; then
  echo
  echo "  $(m "Порт 443 на этом сервере занят сайтом панели." "Port 443 on this server is used by the panel website.")"
  echo "  $(m "Если поставите сюда work-панель, VPN создавайте на портах:" "If you install a work panel here, create VPNs on ports:") $(free_vpn_ports)"
fi
echo
echo -e "  $(m "Меню управления:" "Management menu:") \033[1mhidevp\033[0m"
