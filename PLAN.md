# Discord Media Downloader — Implementation Plan

Status: **v0.0.0 released (G7 signed off by the user, 2026-11-02).** The `/security-review` skill runs after the first push. Last updated 2026-11-02.

## 1. Problem & scope

**Problem:** Discord has no "download all". Getting several images/videos from a message (or a few messages) onto a device means saving them one by one — painful on desktop, worse on mobile.

**Goal:** From inside the Discord app (desktop + mobile), one action on a message gives the user a link; one tap on that link downloads everything (ZIP) or saves it to the phone's gallery.

| In scope | Out of scope (for now) |
|---|---|
| One message, or a handful (≤ 10) via a basket | Whole-channel / bulk archiving (dedicated tools exist) |
| Public app, guild + user install | Server-side ZIP / file proxy (not needed, see §2) |
| Images + videos (all attachment types behind a flag) | Discord login on the download page |
| Desktop + mobile (in-app or device browser is fine) | Persistent storage, multi-instance scaling |

**Core principle: the server never touches file bytes.** The bot captures signed CDN URLs; the user's browser downloads directly from Discord's CDN and builds the ZIP locally.

## 2. Verified facts (tested 2026-11-02)

Tested against a real signed attachment URL:

| Check | Result | Consequence |
|---|---|---|
| `GET cdn.discordapp.com/...` with foreign `Origin` | 200, `Access-Control-Allow-Origin: *` | Browser can `fetch()` attachments directly |
| `GET media.discordapp.net/...` | 200, `Access-Control-Allow-Origin: *` | Same for media proxy (thumbnails, embeds) |
| `Range` request | 206 + CORS header | Partial/resumable fetch possible |
| `Content-Length` | Present | Sizes known up-front (also in attachment object) |
| Unsigned URL / tampered `hm` | 404 | URLs are bearer secrets; must be captured fresh |
| `ex - is` | 24 h | Link TTL (60 min) is well within CDN validity |

From the Discord docs:

- **Message context menu** commands deliver the full target message (incl. `attachments`) in `resolved.messages`; content of the target message is **exempt from the Message Content intent**.
- **Forwarded messages** carry `message_snapshots[0]` with `attachments`/`embeds` (no author). **DM content with the bot is exempt** from the Message Content intent.
- Commands support `integration_types` (GUILD_INSTALL, USER_INSTALL) and `contexts` (GUILD, BOT_DM, PRIVATE_CHANNEL). User-installed apps in servers without the bot are capped at 5 followups per interaction (we need 1).
- Interaction must be answered within 3 s; token valid 15 min. Link-button URLs max 512 chars (so URL lists can't be encoded into the link — a small server-side record is required).
- ⇒ **No privileged intents required.**

## 3. UX

### A. Context menu — "Download attachments" (v0.1: one live panel, "Design A" at G-UX)
Right-click / long-press message → Apps → **Download attachments**.
Ephemeral reply: **one Components V2 panel** (`views.py`, a `LayoutView` with one Container):
```
📦 12 files · 48.3 MB · link expires in 59 min   (+ "only the first N of M" note)
[thumb][thumb][thumb]…                            MediaGallery, ≤ 10 images/videos (+N more)
-# 📎 report.pdf · notes.txt · +3 more            other files; names escaped, one line
[⬇️ Download all ↗] [🗜️ Download ZIP ↗]           #token.files / #token.zip
──────────────────────────────────────
🧺 Basket: 7 files from 3 messages                (only with a non-empty basket)
[➕ Add to basket] [🧺 Basket ZIP ↗] [🧹 Clear basket] [🗑️ Revoke]
```
- **Direct actions (decided at G0):** the action rides in the fragment (`/d#<token>.files`, `/d#<token>.zip`), and the page starts it on load. "Open page" was dropped in v0.1: Download all lands on the page anyway.
- **One live panel per user:** a new panel deletes the user's previous one through the interaction token that last touched it (if < 14 min old; tokens live 15). Buttons edit the panel they sit on and refresh its token. Only the live panel takes a click's token, and replies are ordered by a ticket taken before their send. In memory only: after a restart old panels stay until dismissed.
- **Basket ZIP** is a pre-minted basket link; each user has one live basket link. A new panel re-mints it (only token hashes are stored), and an in-place edit keeps it while the basket is unchanged.
- **Revoke** says what it does: the link can't be opened again; pages already open keep working until Discord's file links expire.
- Notices (no attachments, errors, cooldowns) are plain ephemeral text and leave the live panel alone. No attachments → "This message has no attachments to download."

### A2. Context menu — "Add to ZIP basket"
The basket's own panel replaces the previous one: "✓ Added N files to your basket." (or duplicate/full), "🧺 Basket: N files from M messages · size · expiry", thumbnails, `[Download all ↗]` `[Basket ZIP ↗]` `[Clear basket]`.

### B. DM the bot
- **Forward** a message to the bot's DM, or **upload** files directly to it.
- Messages arriving within `DM_DEBOUNCE_SEC` (default 8 s) form one burst. The bot sends **one** reply (the same panel, as an ordinary message that is never deleted) with a link for exactly that burst (one folder per message); **Add to basket** on it combines bursts (changed at G6).
- Bounded: a burst holds at most `MAX_FILES_PER_LINK` items (sent at once when full) and lives at most 3 × debounce (≤ 30 s); a user starts at most 5 bursts a minute (one notice beyond that).
- Non-media message → short help text, at most once per 5 minutes per user.

### /basket
`/basket show | link | clear` — available in all contexts. `show` and `link` both answer with the basket panel.

### Download page (`https://<host>/d#<token>`)
- Thumbnail grid (images via media proxy resize params; videos show a placeholder + duration if known).
- **Save to phone** — the **primary action on mobile** (decided at G0): downloads each file individually, ~300 ms apart. Files land in Downloads and Android indexes them into the gallery.
- **Download ZIP** — the primary action on desktop; fetches each file directly from CDN, streams into `client-zip`, saves as `discord-media-YYYYMMDD-HHMM.zip`. Per-file progress.
- **Share** — a secondary action, shown when `navigator.canShare({files})` is true. Phase 0 found that on Pixel it only offers cloud upload ("Upload to Photos"), not a local save.
- Per-file download links.
- Expiry countdown; clear error state for "link expired or invalid" and "Discord file no longer available".
- Fallback if ZIP blob download is blocked (in-app browser): offer "Share ZIP" via Web Share, plus "Open in browser" hint.

## 4. Architecture

```
 Discord client ──(interaction / DM)──▶ Gateway ──▶ ┌──────────── zipbot container ────────────┐
                                                    │ discord.py bot ──▶ in-memory TTL store   │
                                                    │                     ▲                    │
 User's browser ──HTTPS──▶ nginx+certbot ──────────▶│ aiohttp web: page, POST /api/resolve     │
       │                                            └──────────────────────────────────────────┘
       └────────── fetch() files directly (CORS *) ──▶ cdn.discordapp.com / media.discordapp.net
```

- **One Python 3.12 process**: discord.py bot (gateway) + aiohttp.web server in the same event loop, sharing an in-memory store. Links/baskets are lost on restart (acceptable: short-lived by design).
- No Interactions Endpoint URL (gateway only).
- Frontend: static HTML + vanilla JS + vendored `client-zip` (no build step, no third-party script hosts).

### Data model (in memory)

```python
MediaItem:   url, proxy_url, filename, size, content_type, width, height,
             duration_secs, source_index, cdn_expires_at   # parsed from `ex`
LinkRecord:  key=sha256(token), owner_user_id, items[MediaItem],
             created_at, expires_at = min(now+LINK_TTL, min(item.cdn_expires_at) - 5 min),
             zip_name
Basket:      user_id → entries[(message_id, added_at, items)], sliding TTL,
             caps: BASKET_MAX_MESSAGES, MAX_FILES_PER_LINK
DmDebounce:  user_id → (timer, reply_message_id)
```
A background sweeper evicts expired records every minute.

### Media extraction (`media.py`)
Sources, in order: `message.attachments` → `message_snapshots[*].attachments` → image/video `embeds` (using `proxy_url`, e.g. Tenor GIFs) → stickers (optional, later).
Filter by `MEDIA_TYPES` (`content_type` prefix, fallback to extension). Dedupe by attachment id / URL. **Host allowlist**: only `cdn.discordapp.com`, `media.discordapp.net` and `images-ext-1/2.discordapp.net` (external embed proxy, see Phase 0 findings) URLs are ever put into a record.

### Filenames in ZIP
- Sanitize: strip path separators, control chars, Windows-reserved names, trailing dots/spaces; cap length.
- Dedupe: `name (1).jpg`.
- Basket with > 1 message: folder per message `01/`, `02/` …

## 5. Web API

| Route | Description |
|---|---|
| `GET /` | Landing: what it is, install links, privacy note |
| `GET /d` | Static download page (identical for every token) |
| `POST /api/resolve` `{"token": "..."}` | `200 {expires_at, items:[{url, thumb, name, kind, mime, size, width, height, duration}]}` (**v1 frozen at G2; v2 in v0.1 adds `kind: "file"`**: `tests/contract/resolve.schema.json`) or a generic `404` (no distinction between invalid, expired and revoked). File names and folders are computed **server-side** (`naming.py`). The ZIP name is set by the page from the viewer's local time |
| `GET /static/*` | JS/CSS, vendored `client-zip` |
| `GET /privacy`, `GET /terms` | Required for a public app / verification |
| `GET /healthz` | Liveness for Docker healthcheck |

Token lives in the **URL fragment** (`#token`) — never sent in requests by the browser, so it never appears in nginx access logs or `Referer` headers. The page reads it and POSTs it to `/api/resolve`.

## 6. Security

Bearer links are accepted; the following keeps them safe:

- **Token**: `secrets.token_urlsafe(32)` (256-bit). Stored only as `sha256(token)`. Delivered only in **ephemeral** messages or the user's own DM.
- **Short-lived & revocable**: TTL 60 min (clamped to CDN expiry), `[Revoke link]` button, cap `MAX_ACTIVE_LINKS_PER_USER`.
- **No token in logs/referrers**: fragment + POST; `Referrer-Policy: no-referrer`.
- **No sensitive logging**: never log tokens, CDN URLs, filenames, or message content — only counts/sizes/durations.
- **Headers** on all responses: strict CSP (`default-src 'self'; img-src 'self' blob: cdn.discordapp.com media.discordapp.net images-ext-1.discordapp.net images-ext-2.discordapp.net; media-src` same; `connect-src` same hosts; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`), `X-Content-Type-Options: nosniff`, `X-Robots-Tag: noindex`, `Cache-Control: no-store` on `/api/*`. HSTS set in nginx.
- **XSS**: all user-derived strings (filenames) rendered via `textContent`, never `innerHTML`.
- **No third-party scripts**: `client-zip` vendored at a pinned version, checksum recorded in `static/VENDOR.md`.
- **Host allowlist** (§4) — the browser is never pointed at arbitrary external hosts (prevents IP leaks to third parties via embeds).
- **Rate limiting**: `/api/resolve` per IP in-app (`RESOLVE_RATE_PER_MIN`) + nginx `limit_req`; bot per-user cooldown. Client IP taken from `X-Forwarded-For` only when peer is in `TRUSTED_PROXIES`.
- **Container hardening**: non-root user, `read_only: true`, `tmpfs: /tmp`, `cap_drop: [ALL]`, `no-new-privileges`, port bound to `127.0.0.1` only, memory limit.
- **Secrets**: only via `.env` (git- and docker-ignored); never baked into the image. Token reset procedure in README.
- **Dependencies**: pinned versions; `pip-audit` in CI later.
- **Privacy policy** states: no files stored, only temporary in-memory links for ≤ 60 min.

## 7. Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `DISCORD_TOKEN` | — (required) | Bot token |
| `PUBLIC_BASE_URL` | — (required) | e.g. `https://dl.example.com`, must be https |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | Inside container |
| `LINK_TTL_MIN` | `60` | |
| `BASKET_TTL_MIN` | `60` | Sliding |
| `BASKET_MAX_MESSAGES` | `10` | |
| `MAX_FILES_PER_LINK` | `100` | |
| `MEDIA_TYPES` | `image,video` | `audio`, `all` opt-in; the CDN sends no CORS for documents |
| `DM_DEBOUNCE_SEC` | `8` | |
| `MAX_ACTIVE_LINKS_PER_USER` | `20` | |
| `RESOLVE_RATE_PER_MIN` | `30` | per IP |
| `TRUSTED_PROXIES` | `127.0.0.1/32` | nginx peers; compose sets `127.0.0.1,172.30.88.1` (its `zipbot` network gateway); other Docker setups must set their bridge gateway or all clients share one rate-limit bucket |
| `LOG_LEVEL` | `INFO` | |

Loaded with `pydantic-settings`; app fails fast on missing/invalid values.

## 8. Repository layout

```
discord_attachments_zip_bot/
├─ PLAN.md  README.md  LICENSE
├─ Dockerfile               # python:3.12-slim, non-root, pip install, CMD python -m zipbot
├─ docker-compose.yml
├─ .env.example  .dockerignore  .gitignore
├─ pyproject.toml           # deps: discord.py>=2.5, pydantic-settings; dev: pytest, pytest-asyncio, pytest-cov, ruff, pyright, pip-audit, playwright
├─ Makefile / scripts/check.ps1   # G-auto: lint, format, types, tests, coverage
├─ deploy/nginx.conf.example
├─ src/zipbot/
│  ├─ __main__.py           # config → logging → start bot + web in one loop, graceful SIGTERM
│  ├─ config.py
│  ├─ bot.py                # intents (guilds, dm_messages), setup_hook, command sync
│  ├─ media.py              # Message/snapshot/embeds → [MediaItem]
│  ├─ naming.py             # sanitize + dedupe filenames, folder layout
│  ├─ store.py              # links, baskets, debounce, sweeper
│  ├─ views.py              # reply embeds/buttons (link, add-to-basket, revoke, clear)
│  ├─ cogs/context_menu.py  # "Download media", "Add to ZIP basket"
│  ├─ cogs/dm.py            # forwards/uploads in DM + debounce
│  ├─ cogs/basket.py        # /basket
│  └─ web/
│     ├─ app.py             # routes, security headers middleware, rate limiter
│     └─ static/            # index.html, d.html, d.js, style.css, client-zip.js, VENDOR.md
└─ tests/
   ├─ fixtures/*.json       # raw message payloads: attachments, forward, embed gif, none
   ├─ test_media.py  test_naming.py  test_store.py  test_web.py
   └─ spike/                # Phase 0 standalone test page
```

## 9. Docker & hosting

`docker-compose.yml` (single service):
- `build: .`, `env_file: .env`, `restart: unless-stopped`
- `ports: ["127.0.0.1:8080:8080"]`
- `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `mem_limit: 256m`
- `healthcheck`: `python -c "urllib.request.urlopen('http://127.0.0.1:8080/healthz')"`

nginx (yours, with certbot) — `deploy/nginx.conf.example` will contain: `proxy_pass http://127.0.0.1:8080`, `X-Forwarded-For`/`X-Forwarded-Proto`, `limit_req` zone for `/api/`, HSTS, `client_max_body_size 1k`.

## 10. Discord Developer Portal setup

1. **Applications → New Application**: name, icon, description. **General Information**: set Terms of Service URL (`/terms`) and Privacy Policy URL (`/privacy`). Note the **Application ID**.
2. **Bot**: Reset Token → `.env` `DISCORD_TOKEN`. Public Bot **ON**. Requires OAuth2 Code Grant **OFF**. Privileged Gateway Intents: **all OFF** (Presence, Server Members, Message Content).
3. **Installation**:
   - Installation Contexts: ✅ **Guild Install**, ✅ **User Install**
   - Install Link: **Discord Provided Link**
   - Default Install Settings → User Install: scopes `applications.commands`
   - Default Install Settings → Guild Install: scopes `applications.commands`, `bot`; permissions **none** (integer `0`) — interaction replies need no channel permissions; the `bot` scope exists only so server members can DM the bot.
4. **Interactions Endpoint URL**: leave empty (gateway).
5. **Dev testing**: use a private test server. Commands are synced **globally only**: in Phase 0 they appeared instantly, and also syncing to a guild produced duplicates.
6. **Later (≥ 75–100 servers)**: apply for App Verification (no intent approval needed).

## 11. Testing

**Unit (pytest)**: media extraction against fixture payloads; filename sanitize/dedupe; `ex` parsing & expiry clamp; store TTL/eviction/caps; web routes (aiohttp test client): resolve OK / 404 / rate limit / security headers present.

**Fixtures from real payloads**: during Phase 0 the dev bot dumps raw gateway/interaction payloads (attachment message, forward snapshot, GIF embed, DM upload, no-media) to `tests/fixtures/`; personal data (ids, names, URLs) scrubbed before commit.

**Contract**: `/api/resolve` response defined as a JSON Schema in `tests/contract/resolve.schema.json`; both the Python response builder and the Playwright mock are validated against it.

**Bot handlers**: cogs stay thin (parse → call `media`/`store` → render `views`); tested with a mocked `discord.Interaction`.

**Frontend (Playwright: Chromium, Firefox, WebKit)**: local mock server serves fixture files with `Access-Control-Allow-Origin: *` and stubs `/api/resolve`. Asserts: ZIP downloaded, entries/names/dedupe/folders correct; expired-token and CDN-404 error states; XSS — filename `<img src=x onerror=…>` rendered as text; Share button shown only when `navigator.canShare({files})`; CSP violations = test failure.

**Android automation**: Playwright `_android` over adb drives Chrome on the device for the spike and release smoke (experimental API). Discord in-app browser remains manual.

**Phase 0 spike** (`tests/spike/`): a standalone page with a fresh attachment URL pasted in; tests ZIP-in-browser + Web Share.
- Desktop: Chrome, Edge, Firefox — ZIP saves to Downloads.
- Android via adb:
  - `adb reverse tcp:8080 tcp:8080` then `adb shell am start -a android.intent.action.VIEW -d http://localhost:8080/spike.html` (localhost is a secure context, so Web Share works).
  - Verify ZIP: `adb shell ls -l /sdcard/Download`; verify share → Gallery/Photos.
  - Debug via `chrome://inspect` (remote devtools) and `adb logcat`.
  - Then via a real link posted in Discord on the phone → Discord's in-app browser (Custom Tab) with the deployed domain.
- iOS: no test device available → **known risk**; Safari supports Web Share with files, behaviour in Discord's in-app browser unverified.

**E2E (manual, Discord desktop + Android)**: guild install and user install (in a server without the bot); A, A2, B (forward + direct upload), basket caps, revoke, expiry.

## 12. Phases & gates

**G-auto** (every commit, every phase): `ruff check` + `ruff format --check`, `pyright`, `pytest`, coverage ≥ 90 % on `media.py`/`naming.py`/`store.py`. Run locally via `make check` (or `scripts/check.ps1`); CI later.

| # | Phase | Gate (must pass before next phase) |
|---|---|---|
| 0 | Spike: browser ZIP + Web Share on desktop & Android (Chrome + Discord in-app); DM to user-installed bot without shared server; capture real payload fixtures | **G0**: results recorded in this file, fallback decision made — **user sign-off** |
| 1 | Skeleton: config, bot + web in one loop, Docker/compose, `/healthz`, nginx example | **G1**: G-auto; `docker compose build`; container healthy; `/healthz` 200 through nginx; no secrets in image layers (`docker history`) |
| 2 | `media.py`, `naming.py`, `store.py`, resolve contract schema + tests | **G2**: G-auto incl. coverage; fixture tests on real payloads green; **contract frozen** |
| 3 | Web: `/d` page, `/api/resolve`, security headers, rate limit, `client-zip` ZIP, Web Share, error states | **G3**: G-auto; Playwright suite green on 3 engines; header test green; independent review — no High findings open |
| 4 | Trigger A (context menu → link, revoke) | **G4**: G-auto; handler tests; manual E2E guild + user install |
| 5 | Basket (A2 + `/basket` + buttons) | **G5**: G-auto; handler tests; manual E2E multi-message ZIP with folders |
| 6 | Trigger B (DM forward/upload + debounce into basket) | **G6**: G-auto; debounce tests; manual E2E 3 quick forwards → one reply; forward from a server the bot is **not** in → media extracted; independent review — no High findings open |
| 7 | Hardening & docs: cooldowns, caps, graceful shutdown, README, privacy/terms pages | **G7 (release)**: `/security-review` clean; `pip-audit` clean; fresh-clone-to-running < 10 min via README; full E2E desktop + Android — **user sign-off** |

### G4–G7 results (2026-11-02, Pixel 9 Pro XL + Discord Android, driven over adb)

| Check | Result |
|---|---|
| G3 open item: one tap in Discord starts the download (no tap on the page) | ✅ **Download ZIP** and **Save to phone** both auto-start in Discord's in-app browser (Custom Tab). "Save to phone" saved 5 files with no multiple-downloads prompt |
| G4 guild install: Download media → reply → ZIP / Save to phone / Revoke | ✅ "5 files · 51 KB". The ZIP is valid. Revoke edits the reply to "Link revoked" and removes the buttons |
| G4 user install, server **without** the bot | ✅ Download media works. A JPG + SVG + PDF message gives 2 files; the PDF is filtered by `MEDIA_TYPES` |
| G5 basket: "Add to basket" button + "Add to ZIP basket" + Get link | ✅ The ZIP contains `01/` (5 attachments) and `02/Rotating_earth_(large).gif`, the embed fetched via `images-ext-1` through the CSP |
| G6 DM: a burst of forwards → one reply | ✅ Verified with a 30 s debounce override, because adb UI driving is slower than 8 s; then re-checked live with the 8 s default |
| G6 forward from a server the bot is **not** in | ✅ Media extracted (2 files) |
| G6 forward from a private DM (user's DM with a friend; nothing was sent there) | ✅ 8 images extracted |
| G6 independent review | ✅ No High findings. Fixed: library loggers clamped to INFO (discord.py logs HTTP bodies, i.e. link tokens, at DEBUG); views detached (bounded memory); DM race on cancellation. **DM semantics changed:** each burst gets its own link (one folder per message) instead of accumulating in the basket. "Add to basket" on the reply combines bursts. This removed the stale-basket, "basket full" and CDN-expiry failure modes |
| G7 `pip-audit` | ✅ Clean, after bumping setuptools 84.0.0 → 84.1.0 (GHSA-xmj3-9gf7-h52w). It now runs as part of G-auto |
| G7 security review | ✅ A whole-repo review stands in for the `/security-review` skill, which needs a git remote and commits: no High/Medium. Lows fixed: hash-pinned lock (`--require-hashes`, build backend pinned, `--no-build-isolation`); `.gitignore` gaps; privacy wording on revoke vs. already-issued CDN URLs; compose network with a fixed subnet and `TRUSTED_PROXIES` defaulting to its gateway (verified live: per-client limiting works and a single client gets 429 at request 31); fixture discriminator/`joined_at` scrubbed; `server_tokens off` at http level |
| G7 fresh clone → running | ✅ **41 s** from copy to `healthy`, following the README (excluding the Developer Portal steps) |
| Phase 7 extras | `/privacy` and `/terms` pages. Per-user cooldown (10 commands per 60 s) with an ephemeral "slow down" reply. Generic error reply that logs only the exception type. Startup skips the command sync when nothing changed (Discord stalled a restart for 40 s during testing) |

Known observations:
- Discord's mobile client caches the command list per user install. After the commands change, restart the Discord app to see them.
- On a phone, a forwarded message's Forward sheet lists real contacts. The test helper only ever ticked the bot and checked the screen before sending.

### Phases 3–6 status (2026-11-02)

**G3:**
- G-auto ✅ (186 tests).
- Playwright ✅ on Chromium, Firefox and WebKit (15 page tests each). They cover:
  - ZIP, auto-start from `#token.zip`, and "save each file" (`#token.files`);
  - a missing CDN file, invalid/expired tokens, and the countdown;
  - XSS rendered as text, foreign URLs dropped, Share, mobile vs desktop button order;
  - no third-party requests, the token absent from request URLs and Referer, an 8 MB file, and a device clock 2 h off;
  - CSP violations fail the test.
- Header tests ✅.
- Independent review ✅ **no High findings**. What was fixed:
  - Medium: the rate limiter could be bypassed via IPv6. It now keys on /64 and is a size-capped LRU, so per-request work stays constant.
  - Low: 500 responses now keep the security headers.
  - Low: the page uses the server clock (from the `Date` header) for expiry.
  - Low: Share handles the case where every file fails, and a partial result can be retried.
  
  Deferred to Phase 7: `/privacy` and `/terms`, plus documenting `TRUSTED_PROXIES`.
  
  Known limit, accepted: the ZIP and Share paths hold the files in memory, which is fine for the "one message / a few messages" scope.
- ⏳ **Open: on-device check of auto-start without a tap** (Android Chrome and Discord's Custom Tab). It was blocked because the phone was locked and then disconnected. Fallback if it's blocked: the page already shows a "tap the button above" hint.

**Phases 4–6 (code + handler tests ✅):**
- `handlers.py` holds the pure command logic; the cogs stay thin.
- Context menus "Download media" and "Add to ZIP basket", and `/basket show|link|clear`. Contexts are guild + bot DM + group DM; installs are guild + user.
- The reply has 3 link buttons (Save to phone / Download ZIP / Open page) plus persistent `DynamicItem` buttons (Add to basket, Revoke, Get link, Clear) that survive restarts.
- DM cog: a burst within `DM_DEBOUNCE_SEC` → **one** reply. The previous link reply is deleted, text-only DMs get the help text, and bot and guild messages are ignored.
- ⏳ G4–G6 manual E2E and the G6 independent review are still to do.

### G2 result (2026-11-02)

- G-auto ✅: 80 tests; coverage: media 100 %, naming 100 %, store 99 %.
- Fixture tests on the 4 real payloads ✅.
- Resolve contract frozen.
- Thumbnails verified live: `media.discordapp.net` accepts `format=webp&width=&height=` on signed URLs (CORS kept), and `format=webp` on a video proxy URL returns its first frame. `images-ext-*` accepts `width/height`.
- Store behaviour:
  - At the per-user link cap, the **oldest link is evicted** (no error).
  - Total links are capped at 50 000 (memory bound).
  - Revoke uses a separate public `link_id` in the button `custom_id`, never the token.
- DM debounce lives in the DM cog (Phase 6), not the store.

### G1 result (2026-11-02)

- G-auto ✅: ruff, pyright (`pyright[nodejs]`, so there's no dependency on system Node), pytest (18 tests).
- `docker compose build` ✅; container **healthy**; `/healthz` 200 through nginx 1.29 (the example's `location`/proxy blocks, plain HTTP) with all security headers.
- No secrets in the image: no `.env` file anywhere, and only public values in `Config.Env`.
- Hardening verified: uid 10001, `CapEff=0`, `NoNewPrivs=1`, read-only root with writable `/tmp`, port bound to `127.0.0.1` only, 256 MB limit.
- SIGTERM → clean exit 0 in 1 s.
- Runtime deps are locked in `requirements.lock`, generated inside the pinned `python:3.12.16-slim` digest.

### Phase 0 status (2026-11-02)

Spike artifacts in `tests/spike/`: `spike.html`/`spike.js` (vendored `client-zip` 2.5.1, pure JS, so no `wasm-unsafe-eval` is needed), `serve.py` (page server using the §6 CSP, plus a mock CDN on :8081), `run_desktop.py`, `run_android.py`, `capture_payloads.py`.

| Check | Result |
|---|---|
| Desktop ZIP (`run_desktop.py`). Engines: Playwright Chromium, Firefox, WebKit; installed Chrome and Edge on Windows | ✅ All 5 pass. Inputs: 4 mock files (43 MB) plus 2 real signed Discord files (cdn + media host). The ZIP is valid (CRC ok), names are deduped (`photo (1).png`), and there were **0 CSP violations** under the strict §6 policy |
| `canShare({files})` on desktop | Chrome/Edge on Windows: true (images and zip). Playwright builds: n/a |
| Android Chrome 156 (Pixel 9 Pro XL, Android 17): ZIP | ✅ A real tap shows "File downloaded (42.16 MB)". The file lands in `/sdcard/Download`, with a valid ZIP and correct names. The first run failed because of the harness, not the page: Playwright's CDP attach redirects downloads to a PC-side folder. Fixed by resetting `Browser.setDownloadBehavior` to `default` |
| Android: Web Share with a real Discord image | ⚠️ The share sheet opens and user activation survives the fetch. But **Pixel has no local "save to gallery" target**: Photos only offers "Upload to Photos" (cloud). Not a dependable "save to phone" path |
| Android: "Download each file" (one blob download per file, 300 ms apart) | ✅ 3 files saved and **indexed by MediaStore** (`relative_path=Download/`), so they appear in the gallery. There was no "multiple downloads" permission prompt |
| End-to-end on Android: context menu → ephemeral reply → link button → Discord in-app browser (Chrome Custom Tab, page on `localhost` via `adb reverse`) | ✅ Discord accepts a `http://localhost` link button. It shows a **"Leaving Discord" interstitial with the full URL (fragment included)** and a "Trust domain" checkbox. In the Custom Tab: ZIP ✅ saved, "Download each file" ✅ saved and indexed, Share ✅ ("Sharing 2 images") |
| Link-button URL limit | Confirmed: only **2 of 5** attachment URLs fit in 512 chars, so the server-side record (§4) is required |
| Forward to the bot DM (guild install, 1 shared server) | ✅ MESSAGE_CREATE arrives **without the Message Content intent**. `message_snapshots[0].message.attachments` holds all 5 files. Snapshots get **their own attachment copies** (new ids, fresh signed URLs in the DM channel) |
| DM to a user-installed bot **without** a shared server | ⏳ Not tested: only the guild install was done, so the account shares a server with the bot. Needs a second account, or a user install before the guild install |
| Direct file upload in the bot DM | ⏳ Not tested. Driving the picker over adb would show the user's personal photos. Low risk: same attachment structure as a guild message, and the DM exemption applies |
| Real payload fixtures | ✅ Scrubbed with `scrub_fixtures.py` into `tests/fixtures/`: `interaction_context_menu_attachments.json` (3 jpg, gif, mp4 with `duration_secs`), `interaction_context_menu_no_media.json`, `dm_forward.json`, `message_image_embed.json` |

Findings that change the plan (applied in Phase 2 / 4):

1. **Host allowlist** must also include `images-ext-1.discordapp.net` and `images-ext-2.discordapp.net`, the external-embed media proxy. Tested: `Access-Control-Allow-Origin: *`; a tampered URL gives 401. CSP `img-src`/`connect-src` gets the same hosts.
2. **Image embeds** (`type: "image"`, e.g. a pasted GIF link) carry the media in `thumbnail.proxy_url`, not `image`.
3. The bot receives MESSAGE_CREATE for **its own** ephemeral replies, with no `guild_id`. The DM handler must ignore messages authored by the bot.
4. Syncing commands globally **and** to `DEV_GUILD_ID` shows duplicate commands. In dev, sync to the guild only.

**G0 sign-off (user, 2026-11-02):**
- "Save to phone" is the primary mobile action.
- Reply buttons start the chosen action directly.
- The no-shared-server DM and direct DM upload are accepted as working; iOS is ignored for now.
- Still open, and added to the G6 E2E: **forwarding from a server the bot is not in** to the bot DM.

Portal fix applied via API (`PATCH /applications/@me`): the guild install scopes were missing `bot` and are now `applications.commands bot`, permissions 0.

Plan deviation: Python Playwright has no `_android`. `run_android.py` attaches to the phone's Chrome over CDP instead (`adb forward tcp:9222 localabstract:chrome_devtools_remote`).

## 13. Execution & orchestration

The codebase is small (~1–2k LOC), so orchestration is kept light; subagents are used only where they add independence or real parallelism.

| Stage | Executor | Notes |
|---|---|---|
| Phase 0 | Main agent + user | User operates devices; G0 is a user decision |
| Phases 1–2 | Main agent, sequential | Establishes shared interfaces (`MediaItem`, store API, resolve contract) — must be frozen before any parallel work |
| Phases 3–6 | Main agent, sequential (**decided 2026-11-02**) | Parallel `web`/`bot` subagents in worktrees were considered and declined; the frozen contract still keeps the two halves cleanly separated |
| After G3 and G6 | Fresh reviewer subagent | Receives only the diff + §6 Security (not the implementer's reasoning) for an unanchored read; findings triaged and fixed by main agent |
| G7 | `/security-review` skill + manual E2E | |

Rules: no multi-agent Workflow runs without explicit user opt-in; one subagent per file area (no two agents editing the same files); every subagent result is verified by running the gates, not taken at face value; commits only on a feature branch, never to `main` without the user asking.

## 14. Deferred / possible later

- Server-side streaming ZIP fallback (only if Phase 0 shows in-app browsers can't save blobs and Web Share isn't enough).
- Persistent store (Redis) for restarts / multiple instances.
- `/zip link:`, reaction trigger, channel archive (require Message Content intent).
- Sticker extraction, localisation.
