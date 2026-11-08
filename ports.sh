
VPN_PORT_CANDIDATES=(443 8443 2053 2083 2087 2096 9443 10443)
OTHER_VPN_SERVICES=(x-ui 3x-ui s-ui marzban marzban-node sing-box v2ray hysteria-server hysteria tuic amnezia-awg wg-quick@wg0 outline-ss-server)

port_owner() {
  ss -ltnpH "( sport = :$1 )" 2>/dev/null | head -1 | sed -n 's/.*users:(("\([^"]*\)".*/\1/p'
}
port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }

free_vpn_ports() {
  local p out=()
  for p in "${VPN_PORT_CANDIDATES[@]}"; do
    local o; o=$(port_owner "$p")
    { [ -z "$o" ] || [ "$o" = "xray" ]; } && out+=("$p")
  done
  echo "${out[*]}"
}

report_vpn_ports() {
  echo ">>> $(m "Проверка портов для VPN" "Checking ports for VPN")"
  local p o
  for p in "${VPN_PORT_CANDIDATES[@]}"; do
    o=$(port_owner "$p")
    if [ -z "$o" ]; then printf "    %-6s %s\n" "$p" "$(m "свободен" "free")"
    elif [ "$o" = "xray" ]; then printf "    %-6s %s\n" "$p" "$(m "занят Xray этой панели" "used by this panel's Xray")"
    else printf "    %-6s %s\n" "$p" "$(m "ЗАНЯТ" "BUSY"): $o"; fi
  done
  local free; free=$(free_vpn_ports)
  if [ -n "$free" ]; then
    echo "    $(m "Для VPN на этом сервере используйте:" "Use for VPN on this server:") $free"
  else
    echo "!!! $(m "Все популярные порты заняты — для VPN выберите любой свободный порт 1024–65535." "All common ports are busy — pick any free port 1024–65535 for VPN.")"
  fi
}

check_other_vpn() {
  local svc found=()
  for svc in "${OTHER_VPN_SERVICES[@]}"; do
    systemctl is-active --quiet "$svc" 2>/dev/null && found+=("$svc")
  done
  if pgrep -f "xray-linux" >/dev/null 2>&1 && [[ ! " ${found[*]} " == *" x-ui "* ]]; then found+=("xray-linux (x-ui?)"); fi
  [ ${#found[@]} -eq 0 ] && return 0
  echo
  echo "!!! $(m "На сервере работает другая VPN-панель:" "Another VPN panel is running on this server:") ${found[*]}"
  echo "    $(m "Она может занимать порты (обычно 443). Если её пользователи вам больше не нужны — остановите её." "It may hold ports (usually 443). If you no longer need its users — stop it.")"
  if [ -t 0 ]; then
    for svc in "${found[@]}"; do
      [[ "$svc" == xray-linux* ]] && continue
      local a; read -rp "    $(m "Остановить и отключить" "Stop and disable") $svc? [y/N]: " a
      case "$a" in y|Y|yes|д|Д|да) systemctl disable --now "$svc" >/dev/null 2>&1 && echo "    ✔ $svc $(m "остановлен" "stopped")" ;; esac
    done
  else
    echo "    $(m "Остановить, например:" "Stop it, e.g.:") systemctl disable --now ${found[0]}"
  fi
}

check_existing_vpn_ports() {
  local state=$1 p o bad=0
  [ -f "$state" ] || return 0
  for p in $(grep -o '"port": *[0-9]*' "$state" | grep -o '[0-9]*$' | sort -u); do
    o=$(port_owner "$p")
    if [ -n "$o" ] && [ "$o" != "xray" ]; then
      echo "!!! $(m "Порт VPN $p занят программой «$o» — Xray не сможет его открыть." "VPN port $p is used by “$o” — Xray cannot open it.")"
      bad=1
    fi
  done
  [ "$bad" = 1 ] && echo "    $(m "Остановите эту программу или поменяйте порт VPN в админке (вкладка VPN)." "Stop that program or change the VPN port in the admin panel (VPN tab).")"
  return 0
}
