# Discord media downloader

Save every attachment of a Discord message (or a few messages) at once — images, videos, and
any other file — as a ZIP or straight to your phone — from inside the Discord app.

- **Right-click / long-press a message → Apps → Download attachments.** The reply (only you see it)
  is one panel: thumbnails of the files and one-tap buttons **⬇️ Download all** and
  **🗜️ Download ZIP**, plus **Add to basket** and **Revoke**. A new reply replaces your previous one.
- **Add to ZIP basket** on several messages (or **Add to basket** on a reply): the panel's
  **🧺 Basket ZIP** button gives one ZIP with a folder per message (also `/basket show`).
- **Forward messages to the bot's DM** (or upload files there): one reply with a link for all
  of them.

The server never touches the files. The bot captures Discord's signed CDN URLs, and your
browser downloads them directly from Discord and builds the ZIP locally (Discord's CDN sends
`Access-Control-Allow-Origin: *`). Links live ≤ 60 min, are revocable, and are kept in memory
only. Details: [PLAN.md](PLAN.md).

## Requirements

- A Discord application with a bot (setup below).
- Docker + Docker Compose.
- A public HTTPS hostname. The container only listens on `127.0.0.1:8080`; put your own
  reverse proxy with TLS in front (example for nginx + certbot in
  [deploy/nginx.conf.example](deploy/nginx.conf.example)).

## 1. Discord Developer Portal

1. <https://discord.com/developers/applications> → **New Application**.
2. **General Information:** set *Terms of Service URL* to `https://<host>/terms` and
   *Privacy Policy URL* to `https://<host>/privacy`.
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
# edit .env: DISCORD_TOKEN=..., PUBLIC_BASE_URL=https://dl.example.com
```

All settings and defaults are listed in [.env.example](.env.example). The app refuses to
start if a value is missing or invalid, and says which one (never printing its value).

`MEDIA_TYPES` (default `image,video`) decides which attachments go into a link: any of
`image`, `video`, `audio`, or `all` for every file (PDFs, archives, files without an
extension...). `audio` and `all` are opt-ins. With `all`, expect failures: Discord's CDN
sends no CORS headers for documents and similar files (images and videos get
`Access-Control-Allow-Origin: *`), so the browser can't fetch them and the download page
reports them as failed. Embeds (link previews, GIFs) only ever count as images or videos.

`TRUSTED_PROXIES` (default `127.0.0.1/32`) lists the peers whose `X-Forwarded-For` header
is believed, for per-client rate limiting. With nginx on the host the container sees the
Docker bridge gateway as the peer. `docker-compose.yml` uses a fixed `zipbot` network with
gateway `172.30.88.1` and sets `TRUSTED_PROXIES=127.0.0.1,172.30.88.1` for you. Other Docker
setups must set it to their own bridge gateway (`docker network inspect <network>`);
otherwise all clients share one rate-limit bucket.

## 3. Run

```sh
docker compose up -d --build
docker compose ps          # STATUS shows (healthy) after ~20 s
docker compose logs -f     # "synced 3 global commands", "gateway ready"
```

Then point your reverse proxy at `http://127.0.0.1:8080` and check
`https://<host>/healthz` → `{"status": "ok"}`. Slash commands and context menus appear in
Discord within a minute; restart the Discord app if they don't.

Update: `git pull && docker compose up -d --build`. Links and baskets are lost on restart
(they are short-lived by design). When a command changed (e.g. "Download media" became
"Download attachments" in v0.1), the bot re-syncs on start; restart the Discord app if the
old name still shows.

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
