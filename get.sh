#!/bin/bash
set -e
URLS="https://github.com/pronitar/hidevp-panel/releases/latest/download/latest.json"
[ "$(id -u)" -eq 0 ] || { echo "Run as root / Запустите от root: sudo -i"; exit 1; }
command -v apt-get >/dev/null || { echo "Ubuntu 22.04+ / Debian 11+ required"; exit 1; }
need=""; for c in curl unzip python3; do command -v $c >/dev/null || need="$need $c"; done
[ -z "$need" ] || { apt-get update -qq || true; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need >/dev/null; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
got=""
for URL in $URLS; do
  echo ">>> HideVP: $URL"
  curl -fsSL --max-time 20 "$URL" -o "$tmp/latest.json" 2>/dev/null || { echo "    unavailable / недоступно"; continue; }
  read -r VER FILE SHA < <(python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print(m['version'],m['file'],m['sha256'])" "$tmp/latest.json") || continue
  echo ">>> HideVP $VER"
  curl -fSL --progress-bar --max-time 300 "${URL%/*}/$FILE" -o "$tmp/p.zip" || continue
  echo "$SHA  $tmp/p.zip" | sha256sum -c --quiet - && { got=1; break; }
  echo "!!! checksum mismatch / контрольная сумма не совпала"
done
[ -n "$got" ] || { echo "!!! Could not download HideVP / Не удалось скачать HideVP"; exit 1; }
unzip -q -o "$tmp/p.zip" -d "$tmp/x"
if [ -f "$tmp/x/hidevp-panel/master/app/release_key.pub" ]; then
  rc=0; python3 "$tmp/x/hidevp-panel/verify_release.py" "$tmp/latest.json" \
    "$tmp/x/hidevp-panel/master/app/release_key.pub" "$tmp/p.zip" >/dev/null 2>&1 || rc=$?
  [ "$rc" = 1 ] && { echo "!!! bad signature / подпись не прошла проверку"; exit 1; }
fi
rm -rf /root/hidevp-panel; mv "$tmp/x/hidevp-panel" /root/hidevp-panel
cp "$tmp/p.zip" /root/hidevp-panel.zip
cd /root/hidevp-panel
if [ -t 0 ]; then exec bash hidevp.sh; else exec bash hidevp.sh </dev/tty; fi
