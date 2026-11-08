#!/usr/bin/env bash
set -uo pipefail

BASE=${HIDEVP_BASE:-/opt/hidevp-panel}
MASTER=$BASE/master
NODE=$BASE/node
SRC_STORE=$BASE/src
LANG_FILE=$BASE/lang
SELF_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

WORK=$(mktemp -d /tmp/hidevp.XXXXXX); trap 'rm -rf "$WORK"' EXIT

G='\033[0;32m'; Y='\033[0;33m'; R='\033[0;31m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'

[ "$(id -u)" -eq 0 ] || { echo "Run as root / Запустите от root: sudo bash $0"; exit 1; }

HV_LANG=$(cat "$LANG_FILE" 2>/dev/null || true)
choose_lang() {
  echo
  echo -e "${B}Language / Язык${N}"
  echo "1) English"
  echo "2) Русский"
  local a; read -rp "> " a
  case "$a" in 1|en|EN|e) HV_LANG=en ;; *) HV_LANG=ru ;; esac
  mkdir -p "$BASE"; echo "$HV_LANG" > "$LANG_FILE"
}
[ "$HV_LANG" = en ] || [ "$HV_LANG" = ru ] || choose_lang
export HV_LANG
m() { [ "$HV_LANG" = en ] && printf '%s' "$2" || printf '%s' "$1"; }

line()    { echo -e "${C}──────────────────────────────────────────${N}"; }
ok()      { echo -e "${G}✔ $*${N}"; }
warn()    { echo -e "${Y}! $*${N}"; }
err()     { echo -e "${R}✘ $*${N}"; }
pause()   { echo; read -rp "$(m "Enter — назад в меню… " "Enter — back to menu… ")" _; }
ask()     { local a; read -rp "$1 " a; echo "$a"; }
confirm() { local a; read -rp "$1 [y/N]: " a; case "$a" in y|Y|yes|д|Д|да|Да) return 0 ;; *) return 1 ;; esac; }
active()  { systemctl is-active --quiet "$1" 2>/dev/null; }
st()      { active "$1" && echo -e "${G}$(m работает running)${N}" || echo -e "${R}$(m остановлен stopped)${N}"; }
CHOICE()  { ask "$(m "Выбор:" "Choice:")"; }

has_master() { [ -f "$MASTER/data/panel.db" ] && [ -f /etc/systemd/system/hidevp-panel.service ]; }
has_node()   { [ -f "$NODE/data/node.json" ] && [ -f /etc/systemd/system/hidevp-node.service ]; }

ver_of() { [ -f "$1/VERSION" ] && tr -d ' \n' < "$1/VERSION" || echo "?"; }
installed_ver() { ver_of "$SRC_STORE"; }
mget() { hidevp-panel get 2>/dev/null | sed -n "s/^$1=//p"; }
public_ip() { curl -4 -s --max-time 6 https://api.ipify.org || hostname -I | awk '{print $1}'; }

ZIP_DIR=""
find_source() {
  local cands=() c best="" bestv=""
  if [ "$SELF_DIR" != "$SRC_STORE" ] && [ -f "$SELF_DIR/install-master.sh" ]; then echo "$SELF_DIR"; return; fi
  local inst=0; [ -f "$SRC_STORE/VERSION" ] && inst=$(stat -c %Z "$SRC_STORE/VERSION" 2>/dev/null || echo 0)
  local zip; zip=$(ls -t /root/hidevp-panel*.zip /root/latest.zip 2>/dev/null | head -1)
  if [ -n "$zip" ]; then
    if [ -z "$ZIP_DIR" ]; then
      command -v unzip >/dev/null || apt-get install -y -qq unzip >/dev/null
      ZIP_DIR=$(mktemp -d -p "$WORK")
      unzip -q -o "$zip" -d "$ZIP_DIR" 2>/dev/null
    fi
    c=$(find "$ZIP_DIR" -maxdepth 3 -name install-master.sh 2>/dev/null | head -1)
    if [ -n "$c" ]; then
      [ "$(stat -c %Z "$zip")" -gt "$inst" ] && { echo "$(dirname "$c")"; return; }
      cands+=("$(dirname "$c")")
    fi
  fi
  for c in /root/hidevp-panel /root/hidevp-panel/hidevp-panel; do
    if [ -f "$c/install-master.sh" ]; then
      [ -f "$c/VERSION" ] && [ "$(stat -c %Z "$c/VERSION")" -gt "$inst" ] && { echo "$c"; return; }
      cands+=("$c")
    fi
  done
  [ -f "$SRC_STORE/install-master.sh" ] && { echo "$SRC_STORE"; return; }
  [ -f "$SELF_DIR/install-master.sh" ] && cands+=("$SELF_DIR")
  for c in "${cands[@]}"; do
    local v; v=$(ver_of "$c"); [ "$v" = "?" ] && v=0
    if [ -z "$best" ] || { [ "$v" != "$bestv" ] && [ "$(printf '%s\n%s\n' "$bestv" "$v" | sort -V | tail -1)" = "$v" ]; }; then
      best=$c; bestv=$v
    fi
  done
  echo "$best"
}

backup_db() {
  [ -f "$MASTER/data/panel.db" ] || return 0
  local f="/root/hidevp-backup-$(date +%F-%H%M).db"
  if command -v sqlite3 >/dev/null; then sqlite3 "$MASTER/data/panel.db" ".backup '$f'"; else cp "$MASTER/data/panel.db" "$f"; fi
  ok "$(m "Резервная копия базы:" "Database backup:") $f"
}

restart_master() {
  systemctl restart hidevp-panel; sleep 2
  active hidevp-panel && ok "$(m "Панель перезапущена" "Panel restarted")" \
    || err "$(m "Панель не запустилась:" "Panel failed to start:") journalctl -u hidevp-panel -n 30"
}

run_installer() {
  local kind=$1; shift
  local src; src=$(find_source)
  [ -n "$src" ] || { err "$(m "Не найдены файлы панели. Загрузите hidevp-panel.zip в /root." "Panel files not found. Upload hidevp-panel.zip to /root.")"; return 1; }
  echo -e "$(m "Файлы:" "Files:") ${C}$src${N} ($(m версия version) $(ver_of "$src"))"
  ( cd "$src" && env HV_LANG="$HV_LANG" "$@" bash "install-$kind.sh" )
}

install_master() {
  line; echo -e "${B}$(m "Установка master-панели" "Installing the master panel")${N}"; line
  echo "$(m "Домен нужен для HTTPS и входа пользователей по номеру." "A domain enables HTTPS and phone login for users.")"
  echo "$(m "A-запись домена должна указывать на:" "The domain's A record must point to:") $(public_ip)"
  local d; d=$(ask "$(m "Домен (Enter — без домена, http://IP:порт):" "Domain (Enter — no domain, http://IP:port):")")
  run_installer master DOMAIN="${d:-none}"
}

install_node() {
  line; echo -e "${B}$(m "Установка work-панели (узел)" "Installing the work panel (node)")${N}"; line
  run_installer node
}

node_ver()   { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$NODE/agent.py" 2>/dev/null; }
master_ver() { [ -f "$MASTER/app/main.py" ] && installed_ver || echo "?"; }
agent_of()   { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1/node/agent.py" 2>/dev/null; }
vcmp() {
  [ "$1" = "$2" ] && { echo same; return; }
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ] && echo newer || echo older
}
ROLLBACK_DIR=$BASE/rollback

UPDATE_URLS_DEFAULT="https://github.com/pronitar/hidevp-panel/releases/latest/download/latest.json https://hidevp.ru/updates/latest.json"
update_urls() { local u; u=$(has_master && mget update_url); echo "${u:-$UPDATE_URLS_DEFAULT}"; }
pyexe() { [ -x "$MASTER/venv/bin/python" ] && echo "$MASTER/venv/bin/python" || echo python3; }

fetch_release() {
  local want=$1; shift
  local d url f sha v got=""; d=$(mktemp -d -p "$WORK")
  for url in "$@"; do
    curl -fsSL --max-time 20 "$url" -o "$d/m.json" 2>/dev/null || continue
    read -r f sha v < <(python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print(m['file'],m['sha256'],m['version'])" "$d/m.json" 2>/dev/null)
    [ -n "$f" ] && { [ -z "$want" ] || [ "$v" = "$want" ]; } || continue
    curl -fsSL --max-time 300 "${url%/*}/$(basename "$f")" -o "$d/p.zip" 2>/dev/null || continue
    echo "$sha  $d/p.zip" | sha256sum -c --quiet - >/dev/null 2>&1 && { got=1; break; }
  done
  [ -n "$got" ] || return 1
  local key="$SRC_STORE/master/app/release_key.pub"; [ -f "$key" ] || key="$MASTER/app/release_key.pub"
  if [ -f "$key" ] && [ -f "$SRC_STORE/verify_release.py" ]; then
    local rc=0; "$(pyexe)" "$SRC_STORE/verify_release.py" "$d/m.json" "$key" "$d/p.zip" >/dev/null 2>&1 || rc=$?
    [ "$rc" = 1 ] && return 2
  fi
  command -v unzip >/dev/null || apt-get install -y -qq unzip >/dev/null
  unzip -q -o "$d/p.zip" -d "$d/x" && [ -f "$d/x/hidevp-panel/install-master.sh" ] || return 1
  echo "$d/x/hidevp-panel"
}
fetch_latest() { fetch_release "" $(update_urls); }

show_installed() {
  echo "$(m "Установлено на этом сервере:" "Installed on this server:")"
  if has_master; then echo -e "  ${G}✔${N} $(m "Master-панель" "Master panel")  ${B}$(master_ver)${N}   $(st hidevp-panel)"
  else echo -e "  ${Y}—${N} $(m "Master-панель не установлена" "Master panel is not installed")"; fi
  if has_node; then echo -e "  ${G}✔${N} $(m "Work-панель" "Work panel")    ${B}$(node_ver)${N}   $(st hidevp-node)   Xray: $(st xray)"
  else echo -e "  ${Y}—${N} $(m "Work-панель не установлена" "Work panel is not installed")"; fi
}

rollback_warning() {
  echo
  warn "$(m "ВНИМАНИЕ: откат на более старую версию — только на свой страх и риск." "WARNING: rolling back to an older version is entirely at your own risk.")"
  echo "$(m "Старая версия может не поддерживать данные и настройки, созданные новой: часть функций" "An older version may not support data and settings created by a newer one: some features")"
  echo "$(m "может работать неправильно или не работать. Перед откатом будет создана копия базы." "may work incorrectly or not at all. A database backup is made before the rollback.")"
  local a; a=$(ask "$(m "Чтобы продолжить, введите «откат»:" "To continue, type “rollback”:")")
  case "$a" in откат|Откат|ОТКАТ|rollback|Rollback|ROLLBACK) return 0 ;; esac
  echo "$(m "Отменено." "Cancelled.")"; return 1
}

apply_src() {
  local src=$1 todo=()
  has_node && todo+=(node); has_master && todo+=(master)
  local res_node="" res_master=""
  [ -n "${HV_ROLLBACK:-}" ] && backup_db
  if [[ " ${todo[*]} " == *" node "* ]]; then
    line; echo -e "${B}$(m "Установка work-панели" "Installing the work panel") $(agent_of "$src")${N}"
    if ( cd "$src" && HV_LANG="$HV_LANG" bash install-node.sh </dev/null ) && active hidevp-node; then res_node=ok; else res_node=fail; fi
  fi
  if [[ " ${todo[*]} " == *" master "* ]]; then
    line; echo -e "${B}$(m "Установка master-панели" "Installing the master panel") $(ver_of "$src")${N}"
    if ( cd "$src" && HV_LANG="$HV_LANG" DOMAIN="" bash install-master.sh </dev/null ) && active hidevp-panel; then res_master=ok; else res_master=fail; fi
  fi
  line; echo -e "${B}$(m "Итог" "Summary")${N}"
  [ -n "$res_master" ] && { [ "$res_master" = ok ] && ok "$(m "Master-панель:" "Master panel:") $(master_ver)" \
                                                 || err "$(m "Master-панель: ошибка (journalctl -u hidevp-panel -n 30)" "Master panel: failed (journalctl -u hidevp-panel -n 30)")"; }
  [ -n "$res_node" ] && { [ "$res_node" = ok ] && ok "$(m "Work-панель:" "Work panel:") $(node_ver), Xray: $(st xray)" \
                                             || err "$(m "Work-панель: ошибка (journalctl -u hidevp-node -n 30)" "Work panel: failed (journalctl -u hidevp-node -n 30)")"; }
  has_master && echo "$(m "Work-панели на других серверах: админка → Обновления (или hidevp → Обновить панель на том сервере)." "Work panels on other servers: admin panel → Updates (or hidevp → Update on that server).")"
}

offer_src() {
  local src=$1 newv newa curv cura kind
  newv=$(ver_of "$src"); newa=$(agent_of "$src"); curv=$(installed_ver); cura=$(node_ver)
  echo
  echo -e "$(m "Доступная версия:" "Available version:") ${C}$newv${N} ($(m "work-панель" "work panel") ${newa:-?})   $(m "файлы:" "files:") $src"
  has_master && echo "  $(m "Master-панель" "Master panel"): $curv → $newv  [$(vcmp "$newv" "$curv" | sed "s/newer/$(m обновление update)/;s/same/$(m "без изменений" "no change")/;s/older/$(m откат rollback)/")]"
  has_node && [ -n "$newa" ] && echo "  $(m "Work-панель" "Work panel"): $cura → $newa  [$(vcmp "$newa" "${cura:-0}" | sed "s/newer/$(m обновление update)/;s/same/$(m "без изменений" "no change")/;s/older/$(m откат rollback)/")]"
  kind=$(vcmp "$newv" "$curv")
  { has_node && [ -n "$newa" ] && [ "$kind" = same ] && [ "$(vcmp "$newa" "${cura:-0}")" = older ]; } && kind=older
  case "$kind" in
    newer) confirm "$(m "Обновить?" "Update?")" ;;
    same)  echo "$(m "У вас последняя версия." "You have the latest version.")"
           confirm "$(m "Переустановить текущую версию?" "Reinstall the current version?")" ;;
    older) rollback_warning && HV_ROLLBACK=1 ;;
  esac
}

update_all() {
  line; echo -e "${B}$(m "Обновление" "Update")${N}"; line
  show_installed
  { has_master || has_node; } || { warn "$(m "Ничего не установлено — выберите установку." "Nothing is installed — choose an install option.")"; return; }
  local src="${1:-}"; [ -n "$src" ] || src=$(find_source)
  if [ -z "${1:-}" ] && { [ -z "$src" ] || [ "$src" = "$SRC_STORE" ]; }; then
    echo; echo "$(m "Проверяю новую версию на сервере обновлений…" "Checking the update server for a new version…")"
    local dl rc=0; dl=$(fetch_latest) || rc=$?
    if [ "$rc" = 2 ]; then err "$(m "Подпись скачанного обновления не прошла проверку — пропускаю" "The downloaded update failed signature verification — skipping")"
    elif [ -n "$dl" ]; then src=$dl
    else warn "$(m "Сервер обновлений недоступен — сравниваю с установленными файлами." "Update server unreachable — comparing with installed files.")"; fi
  else
    echo; echo "$(m "Найден загруженный архив/папка:" "Found an uploaded archive/folder:") $src"
  fi
  [ -n "$src" ] || { err "$(m "Файлы не найдены. Загрузите hidevp-panel.zip в /root." "Files not found. Upload hidevp-panel.zip to /root.")"; return; }
  local HV_ROLLBACK=""
  if offer_src "$src"; then apply_src "$src"; return; fi
  echo
  confirm "$(m "Показать прошлые версии для отката?" "Show older versions to roll back to?")" && menu_rollback
}

menu_rollback() {
  line; echo -e "${B}$(m "Откат на другую версию" "Roll back to another version")${N}"; line
  show_installed
  local curv; curv=$(installed_ver)
  local items=() labels=() f v u
  for f in $(ls -1t "$ROLLBACK_DIR"/hidevp-panel-*.tar.gz 2>/dev/null); do
    v=$(basename "$f" .tar.gz); v=${v#hidevp-panel-}
    [ "$v" = "$curv" ] && continue
    items+=("local $f"); labels+=("$v  — $(m "копия на этом сервере" "copy on this server") ($(date -r "$f" +%F))")
  done
  echo "$(m "Получаю список версий…" "Fetching the version list…")"
  if [ -f "$SRC_STORE/verify_release.py" ]; then
    while read -r v u; do
      [ -n "$v" ] && [ "$v" != "$curv" ] || continue
      printf '%s\n' "${labels[@]}" | grep -q "^$v " && continue
      items+=("online $v $u"); labels+=("$v  — $(m "релиз" "release")$([ "$(vcmp "$v" "$curv")" = newer ] && m " (новее установленной)" " (newer than installed)")")
    done < <("$(pyexe)" "$SRC_STORE/verify_release.py" --list $(update_urls) 2>/dev/null | head -15)
  fi
  [ ${#items[@]} -gt 0 ] || { warn "$(m "Других версий не найдено." "No other versions found.")"; return; }
  local i; for i in "${!labels[@]}"; do echo "$((i+1))) ${labels[$i]}"; done
  echo "0) $(m "Назад" "Back")"
  local c; c=$(CHOICE)
  [[ "$c" =~ ^[0-9]+$ ]] && [ "$c" -ge 1 ] && [ "$c" -le ${#items[@]} ] || return
  local it=${items[$((c-1))]} src=""
  set -- $it
  if [ "$1" = local ]; then
    local d; d=$(mktemp -d -p "$WORK")
    tar -xzf "$2" -C "$d" 2>/dev/null && [ -f "$d/hidevp-panel/install-master.sh" ] && src="$d/hidevp-panel"
  else
    local rc=0; src=$(fetch_release "$2" "$3") || rc=$?
    [ "$rc" = 2 ] && { err "$(m "Подпись пакета не прошла проверку — отменено" "Package signature verification failed — cancelled")"; return; }
  fi
  [ -n "$src" ] || { err "$(m "Не удалось получить файлы версии" "Could not get the version files")"; return; }
  local HV_ROLLBACK=""
  offer_src "$src" && apply_src "$src"
}

publish_release() {
  line; echo -e "${B}$(m "Публикация обновления" "Publish an update")${N}"; line
  echo "$(m "Архив подписывается вашим ключом (/root/.hidevp-release, создаётся при первом запуске)" "The archive is signed with your key (/root/.hidevp-release, created on first run)")"
  echo "$(m "и выкладывается по адресу" "and published at") $(mget public_url)/updates/latest.json"
  echo "$(m "Все панели, проверяющие этот адрес, увидят обновление в течение 6 часов (или сразу по кнопке «Проверить»)." "All panels checking this URL see the update within 6 hours (or immediately via “Check”).")"
  echo
  local zip notes; zip=$(ask "$(m "Путь к архиву [/root/hidevp-panel.zip]:" "Archive path [/root/hidevp-panel.zip]:")"); zip=${zip:-/root/hidevp-panel.zip}
  [ -f "$zip" ] || { err "$(m "Файл не найден:" "File not found:") $zip"; return; }
  notes=$(ask "$(m "Что нового (одной строкой, можно пусто):" "What's new (one line, may be empty):")")
  hidevp-panel release "$zip" --notes "$notes" || { err "$(m "Не удалось опубликовать" "Publishing failed")"; return; }
  local pkg; pkg=$(ls -t "$MASTER"/data/releases/hidevp-panel-*.zip 2>/dev/null | head -1)
  [ -n "$pkg" ] || return
  echo
  confirm "$(m "Установить этот (подписанный) релиз на этот сервер сейчас?" "Install this (signed) release on this server now?")" || return
  local d; d=$(mktemp -d -p "$WORK"); unzip -q -o "$pkg" -d "$d"
  update_all "$d/hidevp-panel"
  rm -rf "$d"
}

xray_ver() { /usr/local/bin/xray version 2>/dev/null | head -1 | awk '{print $2}'; }

xray_fix_perms() {
  mkdir -p /etc/systemd/system/xray.service.d
  printf '[Service]\nLogsDirectory=xray\nLogsDirectoryMode=0755\n' > /etc/systemd/system/xray.service.d/hidevp-logs.conf
  systemctl daemon-reload
  local xu; xu=$(systemctl show -p User --value xray 2>/dev/null); [ -n "$xu" ] || xu=nobody
  mkdir -p /var/log/xray; touch /var/log/xray/access.log /var/log/xray/error.log
  chown -R "$xu": /var/log/xray 2>/dev/null || chown -R nobody /var/log/xray
}

xray_restart() {
  xray_fix_perms
  systemctl restart xray; sleep 2
  if active xray; then ok "$(m "Xray перезапущен" "Xray restarted") ($(xray_ver))"
  else err "$(m "Xray не запустился. Последние строки лога:" "Xray failed to start. Last log lines:")"; journalctl -u xray -n 12 --no-pager -o cat; fi
}

xray_reinstall() {
  confirm "$(m "Переустановить Xray-core (последняя версия)? VPN на этом сервере прервётся примерно на минуту." "Reinstall Xray-core (latest version)? VPN on this server stops for about a minute.")" || return
  local cfg=/usr/local/etc/xray/config.json bak=""
  [ -f "$cfg" ] && { bak=$(mktemp); cp "$cfg" "$bak"; }
  echo "$(m "Загрузка и установка Xray (github.com)…" "Downloading and installing Xray (github.com)…")"
  if ! bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install --force >/tmp/hidevp-xray-install.log 2>&1; then
    err "$(m "Установка не удалась (нет доступа к GitHub?). Лог: /tmp/hidevp-xray-install.log" "Install failed (no access to GitHub?). Log: /tmp/hidevp-xray-install.log")"
    tail -5 /tmp/hidevp-xray-install.log; [ -n "$bak" ] && rm -f "$bak"; return
  fi
  [ -n "$bak" ] && { cp "$bak" "$cfg"; rm -f "$bak"; }
  rm -f "$NODE/data/config.sha256"
  systemctl enable xray >/dev/null 2>&1
  xray_restart
  systemctl restart hidevp-node 2>/dev/null
  ok "$(m "Готово. Нажмите в админке «Перезагрузить панель», чтобы сразу отправить настройки на узел." "Done. Press “Reload panel” in the admin panel to push the settings to the node right away.")"
}

menu_xray() {
  while true; do
    clear; line; echo -e "${B}Xray${N}"; line
    echo -e "$(m "Состояние:" "State:") $(st xray)   $(m "Версия:" "Version:") ${C}$(xray_ver || echo "—")${N}"
    if [ -f /usr/local/etc/xray/config.json ]; then
      local n; n=$(grep -c '"tag": "in-' /usr/local/etc/xray/config.json 2>/dev/null || echo 0)
      echo "$(m "VPN-подключений в конфигурации:" "VPN connections in the config:") $n"
    fi
    line
    echo "1) $(m "Перезапустить Xray" "Restart Xray")"
    echo "2) $(m "Переустановить / обновить Xray-core" "Reinstall / update Xray-core")"
    echo "3) $(m "Лог Xray (последние 40 строк)" "Xray log (last 40 lines)")"
    echo "4) $(m "Проверить конфигурацию" "Test the configuration")"
    echo "0) $(m "Назад" "Back")"
    case $(CHOICE) in
      1) xray_restart; pause ;;
      2) xray_reinstall; pause ;;
      3) journalctl -u xray -n 40 --no-pager -o cat; pause ;;
      4) /usr/local/bin/xray run -test -c /usr/local/etc/xray/config.json 2>&1 | tail -5; pause ;;
      *) return ;;
    esac
  done
}

web_mode() {
  if [ -e /etc/nginx/sites-available/hidevp-panel.conf ] || [ -e /etc/nginx/conf.d/hidevp-panel.conf ]; then echo nginx
  elif [ -f /etc/caddy/hidevp-panel.caddy ]; then echo caddy
  else echo none; fi
}
cur_domain() { mget public_url | sed -n 's#^https://\([^:/]*\).*#\1#p'; }
cert_expiry() {
  local d=$1 hostport f
  f=/etc/letsencrypt/live/$d/fullchain.pem
  if [ -f "$f" ]; then openssl x509 -enddate -noout -in "$f" 2>/dev/null | cut -d= -f2; return; fi
  hostport=$(mget public_url | sed -n 's#^https://\([^/]*\).*#\1#p'); [[ "$hostport" == *:* ]] || hostport="$hostport:443"
  echo | timeout 8 openssl s_client -connect "$hostport" -servername "$d" 2>/dev/null | openssl x509 -enddate -noout 2>/dev/null | cut -d= -f2
}

reissue_cert() {
  local d; d=$(cur_domain)
  [ -n "$d" ] || { warn "$(m "Панель работает без домена — сертификата нет." "The panel runs without a domain — there is no certificate.")"; return; }
  case "$(web_mode)" in
    nginx)
      local args=(-d "$d"); grep -q "server_name.*www\.$d" /etc/nginx/sites-available/hidevp-panel.conf 2>/dev/null && args+=(-d "www.$d")
      echo "$(m "Перевыпуск сертификата через certbot…" "Reissuing the certificate with certbot…")"
      if certbot --nginx "${args[@]}" --non-interactive --agree-tos --register-unsafely-without-email --force-renewal --redirect; then
        systemctl reload nginx; ok "$(m "Сертификат перевыпущен. Действует до:" "Certificate reissued. Valid until:") $(cert_expiry "$d")"
      else err "$(m "certbot не смог выпустить сертификат. Проверьте, что порт 80 открыт и домен указывает на сервер." "certbot failed. Check that port 80 is open and the domain points to this server.")"; fi
      ;;
    caddy)
      echo "$(m "Перевыпуск сертификата через Caddy…" "Reissuing the certificate with Caddy…")"
      systemctl stop caddy
      find /var/lib/caddy /root/.local/share/caddy -type d -path "*/certificates/*/$d" -prune -exec rm -rf {} + 2>/dev/null
      systemctl start caddy; sleep 8
      ok "$(m "Готово. Действует до:" "Done. Valid until:") $(cert_expiry "$d")"
      ;;
    *) warn "$(m "Не найден nginx или Caddy для панели." "No nginx or Caddy site found for the panel.")" ;;
  esac
}

drop_domain() {
  confirm "$(m "Отключить домен и перейти на http://IP:порт? Ссылки пользователей изменятся." "Disable the domain and switch to http://IP:port? User links will change.")" || return
  for f in /etc/nginx/sites-enabled/hidevp-panel.conf /etc/nginx/sites-available/hidevp-panel.conf /etc/nginx/conf.d/hidevp-panel.conf; do rm -f "$f"; done
  active nginx && nginx -t >/dev/null 2>&1 && systemctl reload nginx
  if [ -f /etc/caddy/hidevp-panel.caddy ]; then rm -f /etc/caddy/hidevp-panel.caddy; sed -i '/hidevp-panel.caddy/d' /etc/caddy/Caddyfile; active caddy && systemctl reload caddy; fi
  local p ip; ip=$(public_ip)
  while :; do p=$(shuf -i 20000-60000 -n 1); ss -ltnH "( sport = :$p )" | grep -q . || break; done
  hidevp-panel init --port "$p" --listen 0.0.0.0 --public-url "http://$ip:$p" >/dev/null
  command -v ufw >/dev/null && ufw status | grep -q "Status: active" && ufw allow "$p"/tcp >/dev/null
  restart_master
  echo -e "$(m "Новый адрес админки:" "New admin URL:") ${C}$(mget url)${N}"
}

menu_domain() {
  while true; do
    clear; line; echo -e "${B}$(m "Домен и сертификат" "Domain & certificate")${N}"; line
    local d mode; d=$(cur_domain); mode=$(web_mode)
    if [ -n "$d" ]; then
      echo -e "$(m "Домен:" "Domain:") ${C}$d${N}   ($mode)"
      echo -e "$(m "Сертификат действует до:" "Certificate valid until:") ${C}$(cert_expiry "$d" || true)${N}"
      [ "$mode" = caddy ] && echo "$(m "Caddy продлевает сертификат автоматически." "Caddy renews the certificate automatically.")"
      [ "$mode" = nginx ] && echo "$(m "certbot продлевает сертификат автоматически (systemd-таймер)." "certbot renews the certificate automatically (systemd timer).")"
    else
      echo -e "$(m "Домен не подключён. Адрес:" "No domain. URL:") ${C}$(mget public_url)${N}"
    fi
    line
    echo "1) $(m "Сменить домен / подключить домен" "Change domain / add a domain")"
    echo "2) $(m "Перевыпустить сертификат" "Reissue the certificate")"
    echo "3) $(m "Проверить автопродление (certbot --dry-run)" "Test auto-renewal (certbot --dry-run)")"
    echo "4) $(m "Отключить домен (перейти на http://IP:порт)" "Disable the domain (switch to http://IP:port)")"
    echo "0) $(m "Назад" "Back")"
    case $(CHOICE) in
      1) echo "$(m "A-запись должна указывать на:" "The A record must point to:") $(public_ip)"
         local nd; nd=$(ask "$(m "Новый домен:" "New domain:")")
         [ -n "$nd" ] && run_installer master DOMAIN="$nd"; pause ;;
      2) reissue_cert; pause ;;
      3) if [ "$mode" = nginx ]; then certbot renew --dry-run --cert-name "$d" 2>&1 | tail -5
         else warn "$(m "Доступно только для режима nginx/certbot." "Only available in nginx/certbot mode.")"; fi; pause ;;
      4) if [ -n "$d" ]; then drop_domain; else warn "$(m "Домен и так не подключён." "No domain is connected.")"; fi; pause ;;
      *) return ;;
    esac
  done
}

remove_master() {
  has_master || { warn "$(m "Master-панель не установлена" "Master panel is not installed")"; return; }
  backup_db
  systemctl disable --now hidevp-panel >/dev/null 2>&1
  rm -f /etc/systemd/system/hidevp-panel.service
  local ngx=0
  for f in /etc/nginx/sites-enabled/hidevp-panel.conf /etc/nginx/sites-available/hidevp-panel.conf /etc/nginx/conf.d/hidevp-panel.conf; do
    [ -e "$f" ] && rm -f "$f" && ngx=1
  done
  [ "$ngx" = 1 ] && active nginx && nginx -t >/dev/null 2>&1 && systemctl reload nginx
  if [ -f /etc/caddy/hidevp-panel.caddy ]; then
    rm -f /etc/caddy/hidevp-panel.caddy; sed -i '/hidevp-panel.caddy/d' /etc/caddy/Caddyfile 2>/dev/null
    active caddy && systemctl reload caddy 2>/dev/null
  fi
  rm -rf "$MASTER"; rm -f /usr/local/bin/hidevp-panel
  systemctl daemon-reload
  ok "$(m "Master-панель удалена (сертификаты Let's Encrypt оставлены)" "Master panel removed (Let's Encrypt certificates kept)")"
}

remove_node() {
  has_node || { warn "$(m "Work-панель не установлена" "Work panel is not installed")"; return; }
  systemctl disable --now hidevp-node >/dev/null 2>&1
  rm -f /etc/systemd/system/hidevp-node.service
  if confirm "$(m "Удалить и Xray-core?" "Remove Xray-core too?")"; then
    systemctl disable --now xray >/dev/null 2>&1
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove --purge >/dev/null 2>&1 \
      || rm -f /usr/local/bin/xray /etc/systemd/system/xray.service
    rm -rf /etc/systemd/system/xray.service.d /usr/local/etc/xray /var/log/xray
    ok "$(m "Xray удалён" "Xray removed")"
  else
    echo '{"inbounds":[],"outbounds":[{"protocol":"freedom"}]}' > /usr/local/etc/xray/config.json 2>/dev/null
    systemctl restart xray 2>/dev/null
  fi
  rm -rf "$NODE"; rm -f /usr/local/bin/hidevp-node
  systemctl daemon-reload
  ok "$(m "Work-панель удалена" "Work panel removed")"
}

cleanup_if_empty() {
  if ! has_master && ! has_node; then
    rm -rf "$SRC_STORE" "$LANG_FILE"; rm -f /usr/local/bin/hidevp
    rmdir "$BASE" 2>/dev/null
    ok "$(m "HideVP полностью удалён. Меню снова: cd /root/hidevp-panel && bash hidevp.sh" "HideVP fully removed. Menu again: cd /root/hidevp-panel && bash hidevp.sh")"
    exit 0
  fi
}

menu_remove() {
  line; echo -e "${R}${B}$(m "Удаление" "Removal")${N}"; line
  has_master && echo "1) $(m "Удалить master-панель (копия базы сохранится в /root)" "Remove the master panel (DB backup is saved to /root)")"
  has_node   && echo "2) $(m "Удалить work-панель (узел)" "Remove the work panel (node)")"
  has_master && has_node && echo "3) $(m "Удалить всё" "Remove everything")"
  echo "0) $(m "Назад" "Back")"
  case $(CHOICE) in
    1) confirm "$(m "Точно удалить master-панель? Пользователи перестанут получать подписку" "Really remove the master panel? Users will stop getting subscriptions")" && remove_master ;;
    2) confirm "$(m "Точно удалить work-панель? VPN на этом сервере перестанет работать" "Really remove the work panel? VPN on this server will stop")" && remove_node ;;
    3) confirm "$(m "Удалить ВСЁ?" "Remove EVERYTHING?")" && { remove_master; remove_node; } ;;
    *) return ;;
  esac
  cleanup_if_empty
}

menu_reinstall() {
  line; echo -e "${B}$(m "Переустановка" "Reinstall")${N}"; line
  echo "1) $(m "С сохранением данных (чинит файлы и окружение Python)" "Keep data (repairs files and the Python environment)")"
  echo "2) $(m "Чистая переустановка master — ВСЕ данные удалятся (копия — в /root)" "Clean master reinstall — ALL data is deleted (backup in /root)")"
  echo "3) $(m "Чистая переустановка узла — новый ключ подключения" "Clean node reinstall — new connection key")"
  echo "0) $(m "Назад" "Back")"
  local src
  case $(CHOICE) in
    1) src=$(find_source); [ -n "$src" ] || { err "$(m "Файлы не найдены" "Files not found")"; return; }
       if has_master; then rm -rf "$MASTER/venv" "$MASTER/app" "$MASTER/static"; ( cd "$src" && HV_LANG="$HV_LANG" DOMAIN="" bash install-master.sh </dev/null ); fi
       if has_node; then rm -rf "$NODE/venv"; ( cd "$src" && HV_LANG="$HV_LANG" bash install-node.sh ); fi ;;
    2) has_master || { warn "$(m "Master-панель не установлена" "Master panel is not installed")"; return; }
       confirm "$(m "Все пользователи, серверы и VPN будут удалены. Продолжить?" "All users, servers and VPNs will be deleted. Continue?")" || return
       local dom; dom=$(cur_domain); src=$(find_source)
       remove_master
       ( cd "$src" && HV_LANG="$HV_LANG" DOMAIN="${dom:-none}" bash install-master.sh </dev/null ) ;;
    3) has_node || { warn "$(m "Work-панель не установлена" "Work panel is not installed")"; return; }
       confirm "$(m "У узла будет новый ключ — его нужно заново добавить в админке. Продолжить?" "The node gets a new key — add it again in the admin panel. Continue?")" || return
       systemctl disable --now hidevp-node >/dev/null 2>&1; rm -rf "$NODE"
       ( cd "$(find_source)" && HV_LANG="$HV_LANG" bash install-node.sh ) ;;
  esac
}

menu_admin() {
  while true; do
    clear; line; echo -e "${B}$(m "Админка: данные и доступ" "Admin panel: credentials & access")${N}"; line
    echo -e "$(m "Адрес:" "URL:")    ${C}$(mget url)${N}"
    echo -e "$(m "Логин:" "Login:")    ${C}$(mget login)${N}"
    echo -e "$(m "Название:" "Name:")  $(mget title)"
    line
    echo "1) $(m "Сменить логин администратора" "Change admin login")"
    echo "2) $(m "Сменить пароль администратора" "Change admin password")"
    echo "3) $(m "Сгенерировать новые логин и пароль" "Generate new login and password")"
    echo "4) $(m "Сменить секретный путь админки" "Change the admin secret path")"
    echo "5) $(m "Сменить порт (только без домена)" "Change port (without a domain only)")"
    echo "6) $(m "Сменить название сервиса" "Change the service name")"
    echo "0) $(m "Назад" "Back")"
    case $(CHOICE) in
      1) local l; l=$(ask "$(m "Новый логин:" "New login:")"); [ -n "$l" ] && hidevp-panel set-admin --login "$l"; pause ;;
      2) local p1 p2
         read -rsp "$(m "Новый пароль (мин. 8 символов, Enter — сгенерировать): " "New password (min 8 chars, Enter — generate): ")" p1; echo
         if [ -z "$p1" ]; then p1=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16); echo -e "$(m "Пароль:" "Password:") ${B}$p1${N}"
         else read -rsp "$(m "Повторите пароль: " "Repeat password: ")" p2; echo; [ "$p1" = "$p2" ] || { err "$(m "Пароли не совпадают" "Passwords do not match")"; pause; continue; }; fi
         hidevp-panel set-admin --password "$p1" && ok "$(m "Все сессии сброшены — войдите заново" "All sessions reset — sign in again")"; pause ;;
      3) confirm "$(m "Сгенерировать новые данные входа?" "Generate new credentials?")" && hidevp-panel reset-admin; pause ;;
      4) local np; np=$(ask "$(m "Новый путь (Enter — случайный):" "New path (Enter — random):")")
         hidevp-panel set-path "$np" && restart_master; pause ;;
      5) if [ "$(mget listen)" = "127.0.0.1" ]; then
           warn "$(m "Панель работает через домен — порт менять не нужно." "The panel runs behind a domain — no need to change the port.")"
         else
           local np; np=$(ask "$(m "Новый порт (1024–65535):" "New port (1024–65535):")")
           if [[ "$np" =~ ^[0-9]+$ ]] && ! ss -ltnH "( sport = :$np )" | grep -q .; then
             hidevp-panel set-port "$np" && { command -v ufw >/dev/null && ufw status | grep -q "Status: active" && ufw allow "$np"/tcp >/dev/null; restart_master; }
           else err "$(m "Порт занят или указан неверно" "Port is busy or invalid")"; fi
         fi; pause ;;
      6) local t; t=$(ask "$(m "Название (видят пользователи):" "Name (visible to users):")"); [ -n "$t" ] && hidevp-panel set-title "$t" && restart_master; pause ;;
      *) return ;;
    esac
  done
}

change_lang() {
  choose_lang
  has_master && hidevp-panel set-lang "$HV_LANG" >/dev/null && restart_master
  ok "$(m "Язык меню и панели: русский" "Menu and panel language: English")"
}

header() {
  clear
  echo -e "${B}${C}  HideVP — $(m "управление VPN-сервисом" "VPN service management")${N}"
  line
  if has_master; then
    echo -e "$(m "Master-панель:" "Master panel:") $(st hidevp-panel)"
    echo -e "  ${C}$(mget url)${N}"
  fi
  has_node && echo -e "$(m "Work-панель:" "Work panel:")   $(st hidevp-node)   Xray: $(st xray)"
  if has_master || has_node; then
    echo -e "$(m "Версия:" "Version:") $(installed_ver)$(has_node && echo "   $(m "work-панель" "work panel") $(node_ver)")"
  else echo -e "$(m "Панель не установлена. Версия файлов:" "Panel is not installed. Files version:") $(ver_of "$SELF_DIR")"; fi
  line
}

menu_fresh() {
  echo "1) $(m "Установить master-панель (админка, бот, сайт)" "Install the master panel (admin, bot, website)")"
  echo "2) $(m "Установить work-панель (узел VPN)" "Install the work panel (VPN node)")"
  echo "3) $(m "Установить обе на этот сервер" "Install both on this server")"
  echo "4) $(m "Обновить панель" "Update the panel")"
  echo "5) $(m "Переустановить панель" "Reinstall the panel")"
  echo "6) $(m "Язык / Language" "Language / Язык")"
  echo "0) $(m "Выход" "Exit")"
  case $(CHOICE) in
    1) install_master; pause ;;
    2) install_node; pause ;;
    3) install_master && install_node; pause ;;
    4) update_all; pause ;;
    5) menu_reinstall; pause ;;
    6) choose_lang ;;
    0|q) exit 0 ;;
  esac
}

menu_installed() {
  local n=1; declare -A act
  if has_master; then
    echo "$n) $(m "Данные входа в админку" "Admin login details")";                 act[$n]=info; ((n++))
    echo "$n) $(m "Изменить данные админки/администратора" "Change admin credentials/settings")"; act[$n]=admin; ((n++))
    echo "$n) $(m "Домен и сертификат" "Domain & certificate")";                    act[$n]=domain; ((n++))
    if [ -f /root/.hidevp-release/ed25519.key ]; then
      echo "$n) $(m "Опубликовать обновление (для автора)" "Publish an update (author)")";   act[$n]=publish; ((n++))
    fi
  fi
  if has_node; then
    echo "$n) $(m "Показать ключ узла" "Show node key")";                           act[$n]=key; ((n++))
    echo "$n) $(m "Перевыпустить ключ узла" "Reissue node key")";                    act[$n]=rotate; ((n++))
    echo "$n) $(m "Xray: перезапуск / переустановка" "Xray: restart / reinstall")";      act[$n]=xray; ((n++))
  fi
  has_master || { echo "$n) $(m "Установить master-панель на этот сервер" "Install the master panel on this server")"; act[$n]=imaster; ((n++)); }
  has_node   || { echo "$n) $(m "Установить work-панель на этот сервер" "Install the work panel on this server")";   act[$n]=inode; ((n++)); }
  echo "$n) $(m "Перезапустить службы" "Restart services")";   act[$n]=restart; ((n++))
  echo "$n) $(m "Логи" "Logs")";                               act[$n]=logs; ((n++))
  echo "$n) $(m "Обновить панель" "Update the panel")";        act[$n]=update; ((n++))
  echo "$n) $(m "Откат на прошлую версию" "Roll back to an older version")"; act[$n]=rollback; ((n++))
  echo "$n) $(m "Переустановить панель" "Reinstall the panel")"; act[$n]=reinstall; ((n++))
  echo "$n) $(m "Удалить панель" "Remove the panel")";         act[$n]=remove; ((n++))
  echo "$n) $(m "Язык / Language" "Language / Язык")";         act[$n]=lang; ((n++))
  echo "0) $(m "Выход" "Exit")"
  local c; c=$(CHOICE)
  { [ "$c" = 0 ] || [ "$c" = q ]; } && exit 0
  case "${act[$c]:-}" in
    info)    hidevp-panel info; pause ;;
    admin)   menu_admin ;;
    domain)  menu_domain ;;
    publish) publish_release; pause ;;
    key)     echo; hidevp-node key; echo; echo "$(m "Вставьте в админке: Серверы → Подключить сервер" "Paste it in the admin panel: Servers → Connect server")"; pause ;;
    xray)    menu_xray ;;
    rotate)  confirm "$(m "Старый ключ перестанет работать. Перевыпустить?" "The old key will stop working. Reissue?")" && {
               echo; hidevp-node rotate; echo; systemctl restart hidevp-node
               warn "$(m "Обновите ключ в админке: Серверы → Изменить → Новый ключ" "Update the key in the admin panel: Servers → Edit → New key")"; }; pause ;;
    imaster) install_master; pause ;;
    inode)   install_node; pause ;;
    restart) has_master && restart_master
             has_node && { xray_fix_perms; systemctl restart hidevp-node xray; sleep 2; echo -e "$(m "Узел:" "Node:") $(st hidevp-node)  Xray: $(st xray)"; }
             pause ;;
    logs)    echo "1) master  2) $(m узел node)  3) Xray"
             case $(CHOICE) in
               1) journalctl -u hidevp-panel -n 40 --no-pager ;;
               2) journalctl -u hidevp-node -n 40 --no-pager ;;
               3) journalctl -u xray -n 40 --no-pager ;;
             esac; pause ;;
    update)  update_all; pause ;;
    rollback) menu_rollback; pause ;;
    reinstall) menu_reinstall; pause ;;
    remove)  menu_remove; pause ;;
    lang)    change_lang; pause ;;
  esac
}

case "${1:-}" in
  install-master) install_master; exit ;;
  install-node)   install_node; exit ;;
  update)         update_all; exit ;;
  rollback)       menu_rollback; exit ;;
  publish)        publish_release; exit ;;
esac

while true; do
  header
  if has_master || has_node; then menu_installed; else menu_fresh; fi
done
