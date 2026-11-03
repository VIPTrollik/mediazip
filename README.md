# MediaZip

Save every image and video of a Discord message (or a few messages) at once — as a ZIP or
straight to your phone — from inside the Discord app.

- **Right-click / long-press a message → Apps → Download media.** The reply (only you see it)
  is one panel: thumbnails of the files and one-tap buttons **⬇️ Download all** and
  **🗜️ Download ZIP**, plus **Add to ZIP** and **Revoke**. A new reply replaces your previous one.
- **Add to ZIP** on several messages (or on a reply): your ZIP lives in one message in the
  bot's DM (posted silently, then kept up to date), with **🗜️ Download your ZIP** for one ZIP
  with a folder per message. Each add answers with a short note and an **Open your ZIP** link
  to it (also `/zip show`); **Clear your ZIP** deletes it, and it turns into "Your ZIP
  expired" when it does. If the bot can't DM you, your ZIP shows in the reply panel instead
  (`BASKET_DM=false` does that for everyone). Internally this is still the "basket".
- **Forward messages to the bot's DM** (or upload photos and videos there): one reply with a link for all
  of them.

The server never touches the files. The bot captures Discord's signed CDN URLs, and your
browser downloads them directly from Discord and builds the ZIP locally (Discord's CDN sends
`Access-Control-Allow-Origin: *`). Links live ≤ 60 min, are revocable, and are kept in memory
only. Details: [PLAN.md](PLAN.md).

## Requirements

- A Discord application with a bot (setup below).
- Docker + Docker Compose.
- A public HTTPS hostname (production: `mediazip.download`). The container is published on
  `127.0.0.1:8080` only (`HOST_PORT` changes the port); put your own reverse proxy with TLS
  in front (example for nginx + certbot in [deploy/nginx.conf.example](deploy/nginx.conf.example)).

## 1. Discord Developer Portal

1. <https://discord.com/developers/applications> → **New Application**.
2. **General Information:** name `MediaZip`; set *Terms of Service URL* to
   `https://mediazip.download/terms` and *Privacy Policy URL* to
   `https://mediazip.download/privacy` (`https://<host>/…` for another host).
3. **Bot:** **Reset Token** and copy it (this is `DISCORD_TOKEN`). *Public Bot* on,
   *Requires OAuth2 Code Grant* off. **All three Privileged Gateway Intents off** — the app
   doesn't need them.
4. **Installation:**
   - Installation Contexts: ✅ User Install, ✅ Guild Install.
   - Install Link: *Discord Provided Link*.
   - User Install scopes: `applications.commands`.
   - Guild Install scopes: `applications.commands`, `bot`; permissions: none.
5. Leave *Interactions Endpoint URL* empty (the bot uses the gateway).

Install it with the link shown on the Installation page: *Add to My Apps* (works in every
server and DM, even where the bot isn't a member) and/or *Add to Server*.

## 2. Configure

```sh
cp .env.example .env
# edit .env: DISCORD_TOKEN=..., PUBLIC_BASE_URL=https://mediazip.download
# (local dev: PUBLIC_BASE_URL=http://localhost:8080)
```

All settings and defaults are listed in [.env.example](.env.example). The app refuses to
start if a value is missing or invalid, and says which one (never printing its value).

`MEDIA_TYPES` (default `image,video`) decides which attachments go into a link: any of
`image`, `video`, `audio`, or `all` for every file (PDFs, archives, files without an
extension...). `audio` and `all` are opt-ins. With `all`, expect failures: Discord's CDN
sends no CORS headers for documents and similar files (images and videos get
`Access-Control-Allow-Origin: *`), so the browser can't fetch them and the download page
reports them as failed. Embeds (link previews, GIFs) only ever count as images or videos.

`GA_MEASUREMENT_ID` (default unset: no analytics) turns on Google Analytics 4 for the
landing, privacy and terms pages only, in Consent Mode with all storage denied, so it runs
without cookies (check your own legal requirements, e.g. whether you need a notice or
banner where you operate). The server then adds a `ga-id` meta tag and
`/static/analytics.js` to those three pages, widens their CSP for Google's hosts, and shows
the "Website analytics" section of the privacy page (without the setting, that page says no
analytics are used). The download page `/d`, whose URL fragment holds the link token, the API
and the install page never load it.

Expect partial numbers: with consent denied, GA4 gets cookieless pings only, so reports are
modelled or incomplete (no returning users, little or no attribution), and Realtime may show
little or nothing. That is the price of not setting cookies, not a fault in the setup.

`TRUSTED_PROXIES` (default `127.0.0.1/32`) lists the peers whose `X-Forwarded-For` header
is believed, for per-client rate limiting. With nginx on the host the container sees the
Docker bridge gateway as the peer. `docker-compose.yml` uses a fixed `zipbot` network,
by default subnet `172.30.88.0/24` with gateway `172.30.88.1`, and sets
`TRUSTED_PROXIES=127.0.0.1,<gateway>` for you. Other Docker setups must set it to their own
bridge gateway (`docker network inspect <network>`); otherwise all clients share one
rate-limit bucket.

Compose-only settings, read from the same `.env` (or the shell) when `docker compose` runs:

- `HOST_PORT` (default `8080`): the host port, always bound to `127.0.0.1`. Use e.g. `8090`
  if 8080 is taken, and point nginx there.
- `ZIPBOT_SUBNET` / `ZIPBOT_GATEWAY` (defaults `172.30.88.0/24` / `172.30.88.1`): the
  network's fixed subnet, for hosts where it overlaps another Docker network ("Pool overlaps
  with other one on this address space"; `docker network inspect $(docker network ls -q)
  --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'` lists the taken ones).
  Change both together; `TRUSTED_PROXIES` follows the gateway unless you set it yourself.
  An existing network keeps its old subnet, so run `docker compose down` before changing
  them, then `docker compose up -d`.

## 3. Run

```sh
docker compose up -d --build
docker compose ps          # STATUS shows (healthy) after ~20 s
docker compose logs -f     # "synced 3 global commands", "gateway ready"
```

Then point your reverse proxy at `http://127.0.0.1:${HOST_PORT}` (8080 by default) and check
`https://<host>/healthz` → `{"status": "ok"}`. Slash commands and context menus appear in
Discord within a minute; restart the Discord app if they don't.

Update: `git pull && docker compose up -d --build`. Links and ZIPs (baskets) are lost on restart
(they are short-lived by design). When a command changed (e.g. "Download attachments" is
"Download media" again since media became the scope), the bot re-syncs on start; restart the
Discord app if the old name still shows.

### Deploy on mediazip.download

The image builds natively on amd64 and arm64 Linux hosts with Docker
(the base image and every locked wheel have arm64 builds).

1. DNS: `A` (and `AAAA`, if the server has IPv6) records for `mediazip.download` → the server.
2. `git clone <repo> ~/mediazip && cd ~/mediazip`, then `cp .env.example .env` and set:

   ```sh
   DISCORD_TOKEN=...
   PUBLIC_BASE_URL=https://mediazip.download
   GA_MEASUREMENT_ID=G-Z8VRMRQ3X4
   HOST_PORT=8090
   # only if 172.30.88.0/24 is already used by another network (`docker compose down` first
   # if the stack already ran):
   # ZIPBOT_SUBNET=172.30.89.0/24
   # ZIPBOT_GATEWAY=172.30.89.1
   ```

3. `sudo docker compose up -d --build`, then `sudo docker compose ps` (healthy) and
   `curl -s http://127.0.0.1:8090/healthz`.
4. nginx, HTTP first: write the plain `listen 80` block from
   [deploy/nginx.conf.example](deploy/nginx.conf.example) (`server_name mediazip.download`,
   `proxy_pass http://127.0.0.1:8090`) to `/etc/nginx/sites-enabled/mediazip.download` (or wherever the
   server's sites are included), and create `/etc/nginx/zipbot_proxy.conf` as shown at its end.
5. `sudo nginx -t && sudo systemctl reload nginx`, and check
   `curl -s http://mediazip.download/healthz`.
6. `sudo certbot --nginx -d mediazip.download`: it adds `listen 443 ssl`, the certificate and
   the HTTP → HTTPS redirect to that block (add `http2 on;` afterwards if you want it), and
   the HSTS header from the example then takes effect.
7. Verify: `https://mediazip.download/healthz` → `{"status": "ok"}`, `http://` redirects to
   `https://`, and in the Developer Portal set the Terms URL `https://mediazip.download/terms`
   and the Privacy URL `https://mediazip.download/privacy`.
8. Analytics check: open `https://mediazip.download/` in a fresh browser profile. DevTools →
   Application → Cookies shows no cookies for the site, and the Console shows no CSP
   violations; `/d` loads nothing from Google (Network tab). In GA, Realtime or DebugView may
   show only a few or no hits, as described above.
9. Stop any other copy of the bot (e.g. a local one) once this one is up: both would receive
   every command and DM, and links made by one don't exist in the other's memory.

### If the token leaks

Developer Portal → Bot → **Reset Token**, put the new one in `.env`,
`docker compose up -d`. The old token stops working immediately.

## Development

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Windows; .venv/bin/python elsewhere
.venv/Scripts/python -m playwright install chromium firefox webkit
powershell -File scripts/check.ps1                     # or: make check
```

`check` runs ruff, pyright, the test suite (unit, handler, web, and Playwright tests on three
browser engines) and the coverage gate.

Handy for manual testing:

- `PUBLIC_BASE_URL=http://localhost:8080` in `.env`, and `adb reverse tcp:8080 tcp:8080`
  so an Android phone can open the links.
- `python scripts/dev_link.py <channel_id> <message_id> ...` serves the page with a link to
  real messages (fetched with the bot token), without starting the bot.

Layout: `src/zipbot/` (`media.py` extraction, `naming.py` file names, `store.py` links and
baskets, `handlers.py`/`views.py`/`cogs/` the Discord side, `web/` the page and API),
`tests/` (fixtures are scrubbed real payloads; `tests/contract/` holds the versioned API
schema), `tests/spike/` the Phase 0 experiments.

## License

MIT for this project; `client-zip` (vendored, MIT) — see
`src/zipbot/web/static/VENDOR.md`.
