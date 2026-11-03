# v0.1 plan — UX, landing page, download UI, any file type

Base: `v0.0.0` (b539d9e). The remote has only `feat/initial`, so step 0.1 creates `main` from it.
The rules from PLAN.md still hold: every feature is a branch merged into `main`, G-auto
(`scripts/check.ps1`) is green before each merge, and the CDN, token and CSP invariants stay
as they are.

## Status / decisions (2026-11-02)
- Phase 0 done. Reports: scratchpad `research/` (r1 Discord UX, r2 browser downloads, r3 design, v0 security).
- G-UX: **Design A** (single live ephemeral CV2 panel, previous panel deleted via stored token, ≤10 thumbs).
  Activities evaluated and rejected: no save/download action, `openExternalLink` adds a confirm modal, heavy setup.
- `feat/any-files` merged into `main` (9878e04); G-auto green.
- Folded into branches: DM burst cap + store item budget + button cooldowns + revoke wording (security
  v0 #1/#3/#4) → `feat/discord-ux`; Share batching (≤10 files, ≤50 MiB, allowlisted types), directory /
  save-file pickers on Chromium desktop, one-tap gate for risky types on auto-start (security v0 #2) →
  `feat/download-ui`; IPv4-mapped client IP + RecursionError 400 (v0 #6/#7) → `feat/landing` (owns app.py).

- Branding: renamed to **MediaZip** (2026-11-03; naming report `research/r5-naming.md`, round 2). Basket →
  "Add to ZIP", `/basket` → `/zip`; internal names and `zb:basket…` custom_ids stay. Colours stay (user's call).
  Bot renamed in the Developer Portal; domain **mediazip.download** bought (deploy: `PUBLIC_BASE_URL=https://mediazip.download`).
- Analytics: Google Analytics `G-Z8VRMRQ3X4`, **cookieless** (Consent Mode v2, all storage denied, no banner),
  only on `/`, `/privacy`, `/terms` — never on `/d` (token in the fragment). Measurement id from env; CSP opened
  for Google's hosts on those routes only; privacy page discloses it. Goes in with the rename branch.
- Deploy (after basket-dm + rename): `~/mediazip` git clone, compose + `.env`, host port
  `127.0.0.1:8090`, an nginx site for mediazip.download,
  certbot `--nginx`. Stop the local bot once the server copy is up.
- Live-verified: a user-installed app can DM its installer with no shared server; non-installers get 403/50278.
  Discord's CDN sends no CORS for documents → app scoped to images and videos.

## Phase overview

| Phase | What | Who | Branch | Runs in parallel? |
|---|---|---|---|---|
| 0 | Research + audit (read-only) | 3 research subagents + 1 review subagent | — | yes, all 4 |
| G-UX | Pick the Discord reply design | **user** | — | — |
| 1 | Foundations: any file type (#4), design tokens + component CSS | 2 implementer subagents (worktrees) | `feat/any-files`, `feat/theme` | yes |
| 2 | Features: Discord UX (#1), landing (#2), download UI (#3) | 3 implementer subagents (worktrees) | `feat/discord-ux`, `feat/landing`, `feat/download-ui` | yes |
| 3 | Independent review per branch, then fixes | 3 reviewer subagents, then the original implementers | same | yes |
| 4 | Merge, cross-browser + phone E2E, tag `v0.1.0` | me (sequential) | `main` | no |

I (the orchestrating session) brief each subagent, check its result against the gate, and do the
merges. Subagents never push, never touch `.env` or `tests/fixtures/raw/`, and never message
the DM channel.

The upgrades are regrouped by dependency:
- **#4 goes first.** It changes the resolve contract, which #3 renders.
- **The design "library" is split out of #3 into `feat/theme`.** Both the landing page (#2) and the download UI (#3) build on it.

---

## Phase 0 — research and audit (no code changes)

Four subagents run at once. Each returns a short report to `docs/research/<name>.md` in the
scratchpad, and I copy the decisions into this file.

**R1 — Discord UX researcher** (general-purpose, web access). Each question needs an answer
with a docs link:
1. Which responses can a context-menu command give: message, deferred, modal, launch activity? Is there any silent acknowledgement? (Assumption: no. A context-menu command has no "deferred update", so it must answer with a message or a modal.)
2. What can a modal hold today: Label, text input, selects, file upload, text display, checkbox/radio? Can a modal open a URL or carry a link button? (Assumption: no, so the reply message stays necessary to start a download. The only way around it is an Activity, which is out of scope.)
3. Ephemeral lifetime: can we delete or edit an earlier ephemeral reply through its interaction token (15-minute window)? This is how "keep only the last basket message" would work.
4. Can a **user-installed** app DM the user who installed it with no shared server? This decides whether "send the basket to my DMs" is possible, with one bot message edited in place.
5. Components V2: can a container, a media gallery or a thumbnail row sit next to link buttons in an ephemeral reply? What are the limits (40 components; no `content` field)? Do discord.py 2.7 `LayoutView` and Android render it?

**R2 — browser download researcher** (general-purpose, web access):
1. Desktop "Download all" without one save dialog per file. Compare:
   - `showDirectoryPicker()`: pick a folder once, then write every file into it (Chromium);
   - `showSaveFilePicker()`: stream the ZIP to disk without holding it in memory;
   - Chrome's "multiple downloads" permission when "Ask where to save" is on;
   - Firefox and Safari fallbacks.
2. The Android Chrome Web Share allowlist of file types. Which types does `canShare` reject (e.g. `.zip`, `.apk`, `.exe`, `.txt`)? This decides how "Download all" handles file types that can't be shared.

**R3 — design auditor** (general-purpose):
- Screenshots of `/`, `/d` (in its ready, busy, done, error and expired states), `/privacy` and `/terms` at 360 px and 1280 px, light and dark. It runs `scripts/dev_link.py` plus Playwright against the local container.
- It returns:
  - an audit of inconsistencies (spacing, type scale, button hierarchy, emoji used as icons, the grid's metadata, focus states, contrast);
  - a token and component proposal: colors, spacing, radii, buttons (primary, secondary, ghost), card, file tile, progress, alert, and an inline SVG icon set;
  - one mock HTML page.
- Constraints: the current minimal look stays; there are no external fonts or CDNs; the page works under the current CSP (`self` only).
- Recommendation going in: our own tokens plus about 150 lines of component CSS. No CSS framework, because vendoring one adds weight and CSP work for little gain.

**V0 — security reviewer** (general-purpose, fresh context, given the code but not my conclusions). This is the v0.0.0 review that is still outstanding. It reviews the whole repo, not a diff, because `origin/HEAD` is `feat/initial` itself.

**Gate G-UX (user decision).** I present R1's findings as 2–3 reply designs with mock-ups (see the next section), and you pick one. Everything else in Phase 0 needs no input from you.

---

## Phase 1 — foundations (2 worktree subagents in parallel)

### `feat/any-files` (#4)
The plumbing already exists: `MEDIA_TYPES=all` maps to `kinds=None`. What's left:
- Make `all` the default in `config.py` and `.env.example`.
- **Contract:** bump the schema to `v2` and add `kind: "file"` for non-media files, so the page can tell files apart from unknown-type media. `thumb` stays null for files.
- `naming.py`: keep the original extension. Add tests for dotfiles, files with no extension, and double extensions (`.tar.gz`).
- Update the wording that says "images and videos":
  - `AddResult.EMPTY`;
  - context-menu names: rename "Download media" to "Download attachments". Discord caches command names, so the `command_signature` sync handles this;
  - the README, and the copy on the landing, privacy and terms pages.
- Embeds stay media-only: a link preview is not an attachment.
- Size caps are unchanged. `MAX_FILES`/`MAX_TOTAL` already bound each link.
- **Gate:** G-auto, plus a contract test with a `.pdf`, a `.txt`, a `.zip` and a no-extension file.

### `feat/theme` (the design library; it uses R3's proposal)
- `style.css` is rebuilt as tokens (light, dark, and `prefers-contrast`) plus components: `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-ghost`, `.card`, `.tile`, `.alert`, `.progress`, `.icon`.
- `icons.svg` is a sprite of about 12 icons: download, zip, share, check, alert, file, image, video, audio, archive, document and code.
- The emoji currently used as icons are replaced.
- `/privacy` and `/terms` move onto the theme, which proves the components work.
- **Gate:** G-auto, plus a Playwright screenshot diff that I review by eye, plus contrast at AA in both themes.

I merge `feat/any-files`, then `feat/theme`, into `main`. Phase 2 branches from that.

---

## Phase 2 — features (3 worktree subagents in parallel)

Each implementer owns specific files. A change outside them goes back to me, which avoids
merge conflicts.

| Branch | Owns |
|---|---|
| `feat/discord-ux` | `views.py`, `handlers.py`, `cogs/*`, `bot.py`, `tests/test_bot.py`, `tests/test_dm.py` |
| `feat/landing` | `index.html`, `config.py` (one field), `web/app.py` (the `/` route only), `tests/test_web.py` (landing tests) |
| `feat/download-ui` | `d.html`, `d.js`, `tests/e2e/*` (`style.css` only through new component classes added at the end of the file) |

### `feat/discord-ux` (#1)
This follows the design picked at G-UX. The likely shape, depending on R1's answers:
- **Reply:** one compact message.
  - Line 1: the file count, total size and expiry.
  - Optionally, a Components V2 thumbnail strip, if Android renders it well.
  - Buttons:
    - **Download all** (primary; was "Save to phone"; `#token.files`);
    - **Download ZIP**;
    - an overflow for "Add to basket" and "Revoke";
    - "Open page" is dropped. "Download all" lands on the page anyway.
- **Basket ephemerals:**
  - A button-originated add edits the existing message in place.
  - A context-menu add deletes the previous basket reply through its stored interaction token, if that token is under 15 minutes old. The result is one live basket message.
  - If R1.4 confirms it works, a **"Send to my DMs"** option keeps a single basket message in your DM that the bot edits in place.
- **Modals:** used only where they cut steps. One candidate is "Choose files…": a select of up to 25 attachments for picking a subset before the link is made. A modal is never on the main path, because it can't open the page.
- **Gate:** G-auto, plus a smoke test in the test channel on the phone, run by me via adb.

### `feat/landing` (#2)
- One screen:
  - the name and a one-line pitch;
  - **Add to Discord**: the install link built from `application_id`, which the bot already knows at runtime, with user install as the default;
  - three steps with icons;
  - one screenshot-style illustration in inline SVG, with no real user data;
  - a privacy line;
  - the footer.
- "Download link" is read as the *install* link. If you meant a link to the downloader page itself, that's a one-line change.
- **Gate:** G-auto, plus screenshots at 360 px and 1280 px in light and dark, plus Lighthouse a11y ≥ 95 run locally.

### `feat/download-ui` (#3)
- **Grid:**
  - cleaner tiles: a fixed aspect ratio, name ellipsis with the full name in `title`, size as a quiet second line;
  - a video badge;
  - a check or error overlay per tile;
  - a skeleton state while loading.
- **Non-media files (#4):** an icon tile chosen by extension group (document, archive, audio, code, other) with the extension label.
- **Wording:** "Save to phone" becomes **Download all**; "Download ZIP" stays.
- **Desktop "Download all" (from R2):**
  - Chromium uses `showDirectoryPicker()`: one folder prompt, then every file is written into it.
  - Other browsers fall back to sequential downloads, with a note that a ZIP avoids repeated prompts.
  - `showSaveFilePicker()` streams the ZIP where available, which lowers memory use for large sets.
- **Mobile "Download all":** stays as it is. Types the share sheet rejects (R2.2) go into a ZIP of their own, with a status line explaining why.
- **Gate:** G-auto, plus the Playwright e2e suite on 3 engines with new cases (mixed file types, directory-picker mocked, share-unsupported fallback), plus screenshots.

---

## Phase 3 — review (3 reviewer subagents in parallel, fresh context)

- Each reviewer gets a branch's diff and the security invariants from PLAN.md, and no summary from the implementer, so its read isn't anchored.
- Reviewers report findings ranked by severity, and I verify each one before forwarding it.
- The fixes go back to the original implementer via SendMessage, so it keeps its context.
- `/security-review` runs once more on the combined diff `main..v0.0.0` after Phase 4's merge.

## Phase 4 — integration (me, sequential)

1. Merge `feat/discord-ux`, `feat/download-ui` and `feat/landing` into `main`, in that order, and re-run G-auto after each.
2. Run the cross-browser e2e suite and the fresh-clone Docker build.
3. **Phone E2E via adb** (the test channel only; nothing is ever sent to the DM). Checks:
   - the new reply;
   - Download all, Download ZIP, and the mixed-file-type set;
   - basket dedupe of ephemerals;
   - the landing install link.

   **Manual step (you):** force-stop Discord if it caches the old commands, which happened last time.
4. Bump the version to `0.1.0`, update the status section in PLAN.md, and tag. I push only once you say so.

## Open assumptions (each one is settled by R1/R2 or a one-line answer)
- "Download link" on the landing page means the Discord install link.
- Renaming the commands to "Download attachments" / "Add to basket" is acceptable.
- The default changes to any file type. `MEDIA_TYPES=image,video` stays as an opt-out.
