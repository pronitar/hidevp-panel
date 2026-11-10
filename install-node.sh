#!/usr/bin/env bash
set -euo pipefail
HV_LANG="${HV_LANG:-$(cat /opt/hidevp-panel/lang 2>/dev/null || echo ru)}"
m() { [ "$HV_LANG" = en ] && printf '%s' "$2" || printf '%s' "$1"; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${HV_STAGED:-}" ] && [ "$(id -u)" -eq 0 ]; then
  HV_STAGED=$(mktemp -d /var/tmp/hidevp-inst.XXXXXX)
  cp -r "$SRC/." "$HV_STAGED/"
  export HV_STAGED HV_ORIG_SRC="$SRC"
  exec bash "$HV_STAGED/$(basename "${BASH_SOURCE[0]}")" "$@"
fi
[ -n "${HV_STAGED:-}" ] && trap 'rm -rf "$HV_STAGED"' EXIT
DEST=/opt/hidevp-panel/node

[ "$(id -u)" -eq 0 ] || { echo "$(m "Запустите от root: sudo bash $0" "Run as root: sudo bash $0")"; exit 1; }
command -v apt-get >/dev/null || { echo "$(m "Нужна Ubuntu 22.04+/Debian 11+" "Ubuntu 22.04+/Debian 11+ required")"; exit 1; }

echo ">>> $(m "Установка пакетов" "Installing packages")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "!!! $(m "apt-get update завершился с ошибкой (сломанный сторонний репозиторий?) — продолжаю" "apt-get update failed (broken third-party repo?) — continuing")"
apt-get install -y -qq python3 python3-venv python3-pip curl openssl iproute2 unzip >/dev/null

. "$SRC/ports.sh"
check_other_vpn

if [ -x /usr/local/bin/xray ] && [ "${XRAY_REINSTALL:-0}" != 1 ]; then
  echo ">>> $(m "Xray-core уже установлен:" "Xray-core is already installed:") $(/usr/local/bin/xray version 2>/dev/null | head -1 | awk '{print $2}')"
else
  echo ">>> $(m "Установка Xray-core (официальный скрипт XTLS)" "Installing Xray-core (official XTLS script)")"
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
fi
mkdir -p /etc/systemd/system/xray.service.d
printf '[Service]\nLogsDirectory=xray\nLogsDirectoryMode=0750\nUMask=0027\n' > /etc/systemd/system/xray.service.d/hidevp-logs.conf
systemctl daemon-reload
XUSER=$(systemctl show -p User --value xray 2>/dev/null); [ -n "$XUSER" ] || XUSER=nobody
mkdir -p /var/log/xray
touch /var/log/xray/access.log /var/log/xray/error.log
chown -R "$XUSER": /var/log/xray 2>/dev/null || chown -R nobody /var/log/xray
chmod 750 /var/log/xray; chmod 640 /var/log/xray/*.log

echo ">>> $(m "Включение BBR" "Enabling BBR")"
grep -q "tcp_congestion_control=bbr" /etc/sysctl.conf || {
  echo "net.core.default_qdisc=fq" >> /etc/sysctl.conf
  echo "net.ipv4.tcp_congestion_control=bbr" >> /etc/sysctl.conf
}
sysctl -p >/dev/null 2>&1 || true

PUBLIC_IP=$(curl -4 -s --max-time 8 https://api.ipify.org || true)
[ -n "$PUBLIC_IP" ] || PUBLIC_IP=$(hostname -I | awk '{print $1}')

mkdir -p "$DEST/data"
cp "$SRC/node/agent.py" "$SRC/node/nodectl.py" "$SRC/node/requirements.txt" "$DEST/"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"

if [ -f "$DEST/data/node.json" ]; then
  PORT=$("$DEST/venv/bin/python" -c "import json;print(json.load(open('$DEST/data/node.json'))['port'])")
  FIRST=0
else
  while :; do
    PORT=$(shuf -i 20000-60000 -n 1)
    ss -ltnH "( sport = :$PORT )" | grep -q . || break
  done
  FIRST=1
fi

if [ ! -f "$DEST/data/cert.pem" ]; then
  echo ">>> $(m "Генерация TLS-сертификата для API" "Generating TLS certificate for the API")"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -keyout "$DEST/data/key.pem" -out "$DEST/data/cert.pem" -subj "/CN=hidevp-node" \
    -addext "subjectAltName=IP:$PUBLIC_IP" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign" 2>/dev/null
  chmod 600 "$DEST/data/key.pem"
fi

cd "$DEST" && NODE_HOME=$DEST "$DEST/venv/bin/python" nodectl.py init --host "$PUBLIC_IP" --port "$PORT" > /dev/null

if [ "$FIRST" = 1 ]; then
  cat > /usr/local/etc/xray/config.json <<'EOF'
{"log":{"loglevel":"warning"},"api":{"tag":"api","services":["StatsService"]},"stats":{},
 "inbounds":[{"tag":"api","listen":"127.0.0.1","port":10085,"protocol":"dokodemo-door","settings":{"address":"127.0.0.1"}}],
 "outbounds":[{"protocol":"freedom","tag":"direct"}],
 "routing":{"rules":[{"type":"field","inboundTag":["api"],"outboundTag":"api"}]}}
EOF
fi
systemctl enable xray >/dev/null 2>&1
systemctl daemon-reload
if [ "$FIRST" = 1 ] || ! systemctl is-active --quiet xray; then systemctl restart xray; fi

cat > /etc/systemd/system/hidevp-node.service <<EOF
[Unit]
Description=HideVP Panel (work node agent)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DEST
Environment=NODE_HOME=$DEST
ExecStart=$DEST/venv/bin/python -m uvicorn agent:app --host 0.0.0.0 --port $PORT --ssl-keyfile $DEST/data/key.pem --ssl-certfile $DEST/data/cert.pem --log-level warning
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/hidevp-node <<EOF
#!/bin/bash
cd $DEST && NODE_HOME=$DEST exec $DEST/venv/bin/python nodectl.py "\${@:-key}"
EOF
chmod +x /usr/local/bin/hidevp-node

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null
  echo "!!! $(m "ufw включён: не забудьте открыть порты VPN, которые создадите в панели (например: ufw allow 443/tcp)" "ufw is active: remember to open the VPN ports you create in the panel (e.g. ufw allow 443/tcp)")"
fi

systemctl daemon-reload
systemctl enable hidevp-node >/dev/null 2>&1
chmod 700 "$DEST/data"; chmod 600 "$DEST/data/node.json" "$DEST/data/key.pem" 2>/dev/null || true
systemctl restart hidevp-node
sleep 2

mkdir -p /opt/hidevp-panel; echo "$HV_LANG" > /opt/hidevp-panel/lang
STORE=/opt/hidevp-panel/src
if [ "$(readlink -f "${HV_ORIG_SRC:-$SRC}")" != "$STORE" ]; then
  OLDV=$(cat "$STORE/VERSION" 2>/dev/null || true); NEWV=$(cat "$SRC/VERSION" 2>/dev/null || true)
  if [ -n "$OLDV" ] && [ "$OLDV" != "$NEWV" ] && [ -f "$STORE/install-master.sh" ]; then
    mkdir -p /opt/hidevp-panel/rollback && chmod 700 /opt/hidevp-panel/rollback
    tar -czf "/opt/hidevp-panel/rollback/hidevp-panel-$OLDV.tar.gz" -C /opt/hidevp-panel --exclude=__pycache__ \
      --transform 's,^src,hidevp-panel,' src 2>/dev/null || true
    ls -1t /opt/hidevp-panel/rollback/hidevp-panel-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
  fi
  exec 9>/run/hidevp-store.lock; flock -w 600 9 || true
  NEWSTORE=$(mktemp -d /opt/hidevp-panel/.src-new.XXXXXX)
  cp -r "$SRC/install-master.sh" "$SRC/install-node.sh" "$SRC/hidevp.sh" "$SRC/ports.sh" "$SRC/master" "$SRC/node" "$NEWSTORE/"
  cp "$SRC/get.sh" "$SRC/verify_release.py" "$NEWSTORE/" 2>/dev/null || true
  cp "$SRC/VERSION" "$SRC/README.md" "$NEWSTORE/" 2>/dev/null || true
  find "$NEWSTORE" -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  chmod 755 "$NEWSTORE"
  rm -rf "$STORE.old"; [ -e "$STORE" ] && mv "$STORE" "$STORE.old"
  mv "$NEWSTORE" "$STORE"; rm -rf "$STORE.old" /opt/hidevp-panel/.src-new.*
  flock -u 9 || true
fi
chmod +x "$STORE/hidevp.sh"
ln -sf "$STORE/hidevp.sh" /usr/local/bin/hidevp

echo
report_vpn_ports
check_existing_vpn_ports "$DEST/data/state.json"

echo
echo "================================================================"
echo "  $(m "Work-панель установлена. IP: $PUBLIC_IP, порт API: $PORT" "Work panel installed. IP: $PUBLIC_IP, API port: $PORT")"
echo "  $(m "Ключ для подключения в master-панели (Серверы → Подключить):" "Key to connect in the master panel (Servers → Connect):")"
echo
hidevp-node key
echo
echo "================================================================"
echo -e "  $(m "Меню управления:" "Management menu:")      \033[1mhidevp\033[0m"
echo "  $(m "Показать ключ снова:" "Show the key again:")  hidevp-node key"
echo "  $(m "Перевыпустить ключ:" "Reissue the key:")   hidevp-node rotate"
echo "  $(m "Логи:" "Logs:")                 journalctl -u hidevp-node -f ; journalctl -u xray -f"
