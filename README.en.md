<p align="center">
  <img src="docs/logo.svg" height="80" alt="HideVP">
</p>

<p align="center"><b>VPN service management panel on Xray</b></p>

<p align="center">
  <a href="README.md">Русский</a> · <b>English</b>
</p>

<p align="center">
  <a href="https://github.com/pronitar/hidevp-panel/releases/latest"><img src="https://img.shields.io/github/v/release/pronitar/hidevp-panel?label=version" alt="Version"></a>
  <img src="https://img.shields.io/badge/Xray-VLESS%20%2B%20Reality-6d28d9" alt="Xray VLESS + Reality">
  <img src="https://img.shields.io/badge/Ubuntu%20%7C%20Debian-supported-2563eb" alt="Ubuntu | Debian">
</p>

**HideVP Panel** is a personal panel for running your own VPN service — from a single server to a network of nodes in different countries.

- 🖥 **One control center.** The master panel goes on the main server, work panels on the others. All servers, connections and users are managed from a single admin panel.
- ⚙️ **Convenient interface.** A clear admin panel in Russian and English, lots of settings and features, one-click updates and rollbacks.
- 🤖 **Telegram bot for users.** Built in and configured right in the panel: sign-up, payment, renewal, promo codes, notices.
- 💳 **Subscription payments.** Telegram Stars, cryptocurrency (CryptoBot), bank card and bank transfer.

Protocols: VLESS and Trojan with Reality disguise, Shadowsocks-2022 and Hysteria2. Users connect with **Happ** or **INCY** using a subscription link or their personal page.

```
            ┌──────────────────── master ────────────────────┐
 Admin ───► │ Admin panel · Telegram bot · personal pages /u/ │ ◄─── Users
            │ subscriptions /sub/… · SQLite · sync           │      (Happ / INCY)
            └───────┬──────────────────────────┬─────────────┘
           HTTPS + API key              HTTPS + API key
                    ▼                          ▼
          ┌──── work ────┐             ┌──── work ────┐
          │ agent + Xray │             │ agent + Xray │ ◄── VPN traffic
          └──────────────┘             └──────────────┘
```

**Protocols** (all work in Happ and INCY):

| Protocol | Disguise | Port | When to choose |
|---|---|---|---|
| VLESS + Reality (TCP + Vision, XHTTP, gRPC) | as a real website, no own domain or certificate | TCP | the main option |
| Trojan + Reality (TCP, XHTTP, gRPC) | same as VLESS | TCP | a fallback with the same disguise |
| Shadowsocks-2022 | none, traffic looks like random data | TCP and UDP | simple and fast, for countries without strict blocking |
| Hysteria2 | as an HTTP/3 website; optional Salamander obfuscation | UDP | high speed on poor and mobile networks |

Hysteria2 needs Xray 26.3.27 or newer on the node — the installer sets up the latest version.

## Features

- **Servers.** Connect a node with a `NODE-…` key; country flag, panel versions, CPU/memory/disk load; a "Check" button (Xray, ports, firewall, camouflage site, internet access, clock).
- **VPN connections.** VLESS, Trojan, Shadowsocks-2022 and Hysteria2, several connections per server. A free port is filled in automatically; the panel checks that the port is free and reachable from outside. Keys and certificates are created automatically. Adding and removing users is applied on the fly without restarting Xray, so other users stay connected.
- **Auto server selection.** One checkbox on the VPN page: Happ and INCY show "⚡ Auto" at the top of the list, which connects to the fastest available server and switches automatically when a server stops responding.
- **SOCKS5 for the administrator.** The "SOCKS5" button on a server card issues a login/password link: `socks5://`, an app import link and a Telegram proxy link. It is never given to users. Requires work panel 1.2.3+.
- **Users.** Fixed-term or unlimited subscription, traffic and device limits, torrent blocking, traffic reset, link reissue (users can also do it themselves in the bot), Telegram messages, payment history. The subscription link works in Happ and INCY.
- **Plans & payments.** Terms from 1 to 24 months. Prices in Telegram Stars and in a currency (₽, $, €, ₸, ₴, Br). Methods: Telegram Stars, card (Telegram payment provider), CryptoBot, bank transfer confirmed by the administrator with unique payment codes.
- **Promo codes.** 1–100 % discount, plans, validity period, usage limit, single-use or multi-use, new users only.
- **Notices.** Maintenance and news with start and end dates and an audience (everyone, active or expired subscriptions). Shown on the personal page, in the bot and in the apps; sent out in Telegram.
- **Telegram bot.** Configurable buttons, texts and their order, "Clean chat" mode, administrator notifications. Works from Russia via a foreign server or a proxy.
- **Statistics.** Revenue, payments and users for a period, filters, charts, CSV export.
- **Backups.** Automatic (daily, weekly or monthly), full or selective, restore by merging or replacing.
- **Updates.** Versions of all panels on one page, one-click update, rollback to an older version. Packages are signed with Ed25519.
- **Website.** Phone number login (the code arrives in the bot), personal page with a QR code, renewal and "Add to Happ / INCY" buttons.
- Admin panel, bot and website in Russian and English. Four admin panel themes.

## Requirements

- A VPS with **Ubuntu 22.04/24.04** or **Debian 11/12** and root SSH access:
  - one server for the master panel (it can also be a VPN node);
  - node servers in the countries you need.
- Nodes need access to github.com: Xray-core is installed from there.
- A Telegram bot (created with @BotFather).
- Recommended: a domain with an A record pointing to the master server's IP. Without a domain the admin panel works over `http://IP:port`, and the password is sent unencrypted.

## Installation

### 1. Start the menu

One command on each server:

```bash
bash <(curl -fsSL https://github.com/pronitar/hidevp-panel/releases/latest/download/install.sh)
```

Manually: download `hidevp-panel-<version>.zip` from [Releases](https://github.com/pronitar/hidevp-panel/releases), upload it to `/root` and run:

```bash
cd /root && unzip -o hidevp-panel-*.zip && bash hidevp-panel/hidevp.sh
```

On first start the menu asks for a language: 1 — English, 2 — Русский.

### 2. Master panel

Choose **"Install the master panel"** in the menu (or "Install both on this server" if the server will also be a VPN node). The menu asks for a domain:

- **you have a domain** — enter it (the A record must already point to the server). If nginx is running on the server, a separate site and a certbot certificate are added; other sites are not touched. Without nginx, Caddy is installed;
- **no domain** — press Enter; the panel runs on `http://IP:random_port`. A domain can be added later: `hidevp` → "Domain & certificate".

At the end the admin panel address, login and password are shown — save them.

### 3. Work panels (nodes)

On each VPN server choose **"Install the work panel"**. Xray-core and the agent are installed and BBR is enabled. At the end you get a table of free VPN ports and a **`NODE-…` key** to connect the node to the master.

If the master is installed with a domain and the same server will run a VPN, port 443 is taken by the website — use another port for the VPN (for example, 8443).

### 4. First setup

1. **Servers → Connect server** — paste the `NODE-…` key of each node.
2. **VPN → Create** — choose a server, a protocol and a transport; the port is filled in automatically.
3. **Telegram bot** — the token from @BotFather, link administrators ("Link my Telegram" button). If the master is in Russia: "Telegram connection" → "Via a connected server" and pick a foreign node.
4. **Plans & payments** — terms, prices and payment methods.
5. **Settings** — service name, public address, support contact, default limits.
6. **"Reload panel"** at the top, then **"Check"** on each server — every item should be green.
7. **Backups** — turn on automatic backups.

### 5. Ports

| Server | Port | Purpose |
|---|---|---|
| master | 80 and 443 | with a domain: website, admin panel, subscriptions |
| master | from installation | without a domain: website, admin panel, subscriptions |
| node | from installation | work panel API (key access only) |
| node | your VPN ports (443, 8443…) | VPN traffic: TCP; UDP for Hysteria2, TCP and UDP for Shadowsocks |

If your hosting provider has an external firewall (security groups), open these ports there. The installers and the panel open ports in `ufw` themselves.

## The `hidevp` menu

The `hidevp` command opens the menu from anywhere. The items depend on what is installed on the server.

| Item | What it does |
|---|---|
| Admin login details | Admin panel address and login |
| Change admin credentials/settings | Login, password, secret path, port, name |
| Domain & certificate | Add or change a domain, reissue the certificate, go back to IP |
| Show / Reissue node key | The `NODE-…` key for the master panel |
| Xray: restart / reinstall | Restart, update Xray-core, fix log permissions |
| Restart services / Logs | Restart the panels and view logs |
| Update the panel | Version check and update (see below) |
| Roll back to an older version | Install an older version |
| Reinstall / Remove the panel | Keeping data or from scratch; the database is copied to `/root` before removal |
| Language / Язык | English or Russian |

Commands without the menu:

```
hidevp-panel info                     # admin panel address and login
hidevp-panel reset-admin              # new login and password
hidevp-panel set-admin --login L --password P
hidevp-node key | rotate              # show or reissue the node key
journalctl -u hidevp-panel -f         # master panel log
journalctl -u hidevp-node -f          # work panel log
journalctl -u xray -f                 # Xray log
```

## Updates and rollback

The master panel version is shown at the top right of the admin panel. When a new version is released, a green ↑ badge appears next to it and administrators get a Telegram message. The master and work panels are versioned independently.

New versions are published only in [GitHub releases](https://github.com/pronitar/hidevp-panel/releases); panels check for them every 6 hours. The server needs access to github.com — if it has none, upload the release archive to `/root` and choose `hidevp` → "Update the panel".

**Admin panel → "Updates":**
- **Master panel** — the "Update master panel to …" button. The panel restarts in about a minute; a work panel on the same server is updated together with it.
- **Servers** — the work panel version on each server and an "Update to …" button. VPN clients stay connected.
- **Installation progress** is shown step by step with a log in a separate window; the rest of the admin panel is locked while it runs. If a master panel update fails, the previous version is restored automatically.
- **Rollback** — "Roll back…" for the master panel and "Other version…" for a server. Rollback is at your own risk: an older version may not support data from a newer one. The database is backed up automatically before a master panel rollback.

**Console:** `hidevp` → "Update the panel". The menu shows the versions on the server and finds an available one: an archive in `/root`, otherwise the latest from GitHub. If it is newer, the menu offers to update. If it is the same, it says "You have the latest version". If it is older, it warns about the risks and asks you to type the word `rollback`.

`hidevp` → "Roll back to an older version" lists copies of previous versions on the server (the installer saves them before each update in `/opt/hidevp-panel/rollback/`, last 3) and GitHub releases.

Only packages with the author's signature and a matching checksum are installed.

## Backups

The **"Backups"** section of the admin panel.

- **Automatic:** off, daily, weekly or monthly; the number of copies to keep is configurable.
- **Manual:** "Full backup" or "Selective" with checkboxes: users, servers, VPN connections, traffic statistics, payments, promo codes, notices, settings. A backup can be downloaded or saved on the server.
- **Restore:**
  - "Update data" — merge with current data: matching records are updated, new ones are added;
  - "Replace" — current data is replaced with data from the backup. For a selective backup only the data it contains is replaced.
- The administrator login, address, port and secret path of the admin panel do not change on restore. The current database is backed up automatically before a restore and can be returned with the "Roll back" button.
- Backups are stored in `/opt/hidevp-panel/master/data/backups/`. A full backup contains the node connection keys — keep it in a safe place.

## Security

- The admin panel runs on a random path and port. The password is stored as a scrypt hash. After 5 failed attempts, login from that IP is blocked for 10 minutes.
- Session cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS. Origin check for state-changing requests, CSP, `X-Frame-Options` and HSTS headers.
- Website phone login: a 6-digit code valid for 5 minutes, 5 attempts, request limits per number, IP and server.
- Price, currency and promo code are checked before payment; a repeated payment is not counted twice.
- Connection to nodes is HTTPS with a pinned certificate and a 256-bit key.
- The database, backups, node keys and Xray logs are accessible to root only.

## Project structure

```
hidevp-panel/
├── hidevp.sh              install and management menu
├── install-master.sh      master panel installer
├── install-node.sh        work panel installer
├── ports.sh               port checks
├── get.sh                 one-command installer
├── verify_release.py      release signature check
├── VERSION                master panel version
├── tools/release.py       release build and signing
├── .github/workflows/     release publishing on GitHub
├── docs/logo.svg          logo
├── master/
│   ├── app/main.py        web server: admin API, website, subscriptions
│   ├── app/bot.py         Telegram bot
│   ├── app/botui.py       bot buttons and texts
│   ├── app/payments.py    payment methods
│   ├── app/promo.py       promo codes
│   ├── app/notices.py     notices
│   ├── app/stats.py       statistics and CSV export
│   ├── app/backup.py      backups
│   ├── app/update.py      updates and rollback
│   ├── app/sync.py        node sync, limits
│   ├── app/nodes.py       work panel client
│   ├── app/tg.py          Telegram connection (direct, via a node, via a proxy)
│   ├── app/core.py        users, Reality keys, VLESS links
│   ├── app/db.py          SQLite database
│   ├── app/i18n.py        message translation
│   ├── app/qr.py          QR code
│   ├── app/cli.py         the hidevp-panel command
│   └── static/            admin panel, personal page, login page, flags
└── node/
    ├── agent.py           node API: Xray config, statistics, online users
    └── nodectl.py         the hidevp-node command
```

Dependencies: Python 3.10+, Starlette, Uvicorn, cryptography; Xray-core (XTLS) on nodes. Country flags — [flag-icons](https://github.com/lipis/flag-icons) (MIT, license in `master/static/flags/LICENSE.txt`).

## Troubleshooting

Start with **Servers → Check**: it shows the cause of most VPN problems.

| Problem | What to do |
|---|---|
| 502 in the browser | `journalctl -u hidevp-panel -n 30 --no-pager` |
| Server is "Offline" | On the node: `systemctl status hidevp-node`; check that the API port is open at the hosting provider. Cards open instantly; each server's data loads separately |
| "Port is used by another program" | Usually an old x-ui: `x-ui stop && systemctl disable x-ui`, then "Reload panel". Or choose another port |
| Xray does not start | The error is shown on the server card; `hidevp` → Xray → fix permissions or reinstall |
| After a while the connection to all servers is lost | If "Check" is green and another network (Wi-Fi ↔ mobile) works, it's blocking on the provider side: try XHTTP, Hysteria2, another port, camouflage site or IP. If everyone drops at once, run `journalctl -u xray -u hidevp-node --since "1 hour ago"`, `df -h`, `free -m` on the node. Turn on "Auto server selection" so the app moves to a working server by itself |
| The app connects but there is no internet | "Check" on the server; refresh the subscription in the app, try XHTTP, another protocol (e.g. Hysteria2), another port or camouflage site |
| The bot is silent, `timed out` when saving the token | Telegram is unreachable from the master: Telegram bot → "Via a connected server" or a proxy |
| CryptoBot: error 403 or "invalid token" | Bot route "Via a connected server"; a @CryptoBot token — without the "Testnet" checkbox, a @CryptoTestnetBot token — with it |
| An invoice is not created | Press pay in the bot yourself: the administrator sees the reason in the message |
| The website login code does not arrive | The user must press /start in the bot and not block it; the phone number must be linked |
| 🌐 instead of a country flag | Servers → Refresh, or set the country code manually (Edit) |
| Forgot the admin password | `hidevp-panel reset-admin` on the master |
| "Reload panel" | Re-sends settings to all servers and restarts Xray and the bot. Helps when something gets stuck |

## Author

**pronitar** — [github.com/pronitar](https://github.com/pronitar)

Questions and suggestions — via [Issues](https://github.com/pronitar/hidevp-panel/issues).

### Support the project

If the panel is useful to you, you can support its development:

- **Boosty:** https://boosty.to/pronitar/donate
- **TON:** `UQCEpLG3CGuwFGlfwy2UkTEkYbr5sy4TUIlBi33HA0MgoFZH`
- **ETH:** `0x116414f271E19A2ccEdf8F9069206b97603e048F`
- **BTC:** `bc1q8cqyc7mp7yw98kxyr2qt4m3zxghsdz5jp0nny2`

The "❤ Donate" button in the admin panel opens the same details.
