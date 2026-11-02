// Download page: resolve the token, then fetch every file straight from Discord's CDN
// and either save them (into a picked folder, or one by one), ZIP them locally, or hand
// them to the share sheet in batches it accepts.
// All user-derived text goes through textContent; URLs are re-checked against the allowlist.
import { downloadZip } from "/static/client-zip.js";

const ALLOWED_HOSTS = new Set([
  "cdn.discordapp.com",
  "media.discordapp.net",
  "images-ext-1.discordapp.net",
  "images-ext-2.discordapp.net",
]);
const ACTIONS = new Set(["zip", "files"]);
const PARALLEL = 3;
const SAVE_GAP_MS = 300;
const SPRITE = "/static/icons.svg";
const SVG_NS = "http://www.w3.org/2000/svg";
const COMMAND = "“Download attachments”";

// Chromium's share limits: more files, more bytes or one type off the list rejects the whole set.
const SHARE_MAX_FILES = 10;
const SHARE_MAX_BYTES = 50 * 1024 * 1024;
// Extension -> the exact MIME string Chromium's share allowlist accepts (no charset parameter).
const SHARE_TYPES = {
  avif: "image/avif", bmp: "image/bmp", gif: "image/gif", ico: "image/x-icon", jfif: "image/jpeg",
  jpeg: "image/jpeg", jpg: "image/jpeg", pjp: "image/jpeg", pjpeg: "image/jpeg", png: "image/png",
  svg: "image/svg+xml", svgz: "image/svg+xml", tif: "image/tiff", tiff: "image/tiff", webp: "image/webp",
  xbm: "image/x-xbitmap",
  m4v: "video/mp4", mp4: "video/mp4", mpeg: "video/mpeg", mpg: "video/mpeg", ogm: "video/ogg",
  ogv: "video/ogg", webm: "video/webm",
  flac: "audio/flac", m4a: "audio/x-m4a", mp3: "audio/mpeg", oga: "audio/ogg", ogg: "audio/ogg",
  opus: "audio/ogg", wav: "audio/wav", weba: "audio/webm",
  pdf: "application/pdf", txt: "text/plain", text: "text/plain", csv: "text/csv", css: "text/css",
  htm: "text/html", html: "text/html", ehtml: "text/html", shtm: "text/html", shtml: "text/html",
};

// Files that run code or render active content when opened: never saved without a tap.
const RISKY = new Set(
  ("exe msi msix appx appxbundle bat cmd com scr pif cpl msc ps1 psm1 ps1xml vb vbs vbe js jse mjs ws wsf wsh " +
    "hta html htm shtml xhtml xht mht mhtml svg svgz lnk url scf inf reg dll sys jar apk xapk sh bash command " +
    "app pkg dmg deb rpm appimage desktop iso img vhd vhdx chm application gadget docm xlsm pptm").split(" "),
);
// Names Chrome's File System Access refuses (Windows reserved, trailing dot, dangerous types).
const FSA_BLOCKED_EXT = new Set(["lnk", "url", "scf", "local", "ini", "cfg", "manifest", "dll"]);
const RESERVED = /^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$/i;

const GROUPS = {
  document: "pdf doc docx odt ods odp rtf txt md markdown csv tsv xls xlsx ppt pptx epub log tex pages key numbers",
  archive: "zip rar 7z tar gz tgz bz2 xz zst lz cab iso dmg",
  audio: "mp3 ogg oga opus wav flac m4a aac wma weba mid midi",
  code: "py js mjs cjs ts tsx jsx json html htm css scss sass less sh bash zsh ps1 bat cmd c h cpp hpp cc cs rs go java kt rb php swift lua sql xml yml yaml toml ini cfg",
  image: "jpg jpeg png gif webp avif bmp tif tiff heic heif svg ico jfif",
  video: "mp4 mov mkv webm avi m4v wmv mpg mpeg ogv 3gp",
};
const EXT_GROUP = new Map(Object.entries(GROUPS).flatMap(([g, list]) => list.split(" ").map((e) => [e, g])));
const GROUP_ICON = {
  image: "i-image", video: "i-video", audio: "i-audio", document: "i-document", archive: "i-archive",
  code: "i-code", file: "i-file",
};

const $ = (id) => document.getElementById(id);
const coarse = () => matchMedia("(pointer: coarse)").matches;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
// clockSkew: server time minus device time, from the resolve response's Date header.
const state = { items: [], expiresAt: 0, busy: false, share: null, timer: 0, clockSkew: 0, expired: false };

function setPhase(phase) {
  document.body.dataset.state = phase; // used by tests and CSS
}

function safeUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === "https:" && ALLOWED_HOSTS.has(u.hostname) ? u.href : null;
  } catch {
    return null;
  }
}

function formatSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatDuration(secs) {
  const s = Math.round(secs);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function zipName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `discord-files-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.zip`;
}

// "01/a.jpg" -> {folder: "01", base: "a.jpg"}: the server puts files from several messages in folders.
function splitPath(name) {
  const m = /^(\d{2})\/(.+)$/.exec(name);
  return m ? { folder: m[1], base: m[2] } : { folder: "", base: name };
}

function extOf(name) {
  const m = /\.([A-Za-z0-9]{1,16})$/.exec(name);
  return m && m.index > 0 ? m[1].toLowerCase() : "";
}

function flatName(name) {
  return name.replace("/", "-");
}

function icon(id, cls = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `${SPRITE}#${id}`);
  svg.append(use);
  return svg;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function button(label, cls, onClick) {
  const b = el("button", `btn ${cls}`, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function namesList(indices, max = 4) {
  const names = indices.slice(0, max).map((i) => splitPath(state.items[i].name).base);
  const more = indices.length - names.length;
  return names.join(", ") + (more > 0 ? ` and ${more} more` : "");
}

// --- alerts, progress, busy ----------------------------------------------------------

function showError(title, text, actions = []) {
  $("error-title").textContent = title;
  $("error-text").textContent = text;
  $("error-actions").replaceChildren(...actions);
  $("error-actions").hidden = actions.length === 0;
  $("error").hidden = false;
  setPhase("error");
}

// Fatal: nothing on this page can be used any more (no link, expired, server error).
function showFatal(title, text, actions = []) {
  clearInterval(state.timer);
  showError(title, text, actions);
  $("actions").hidden = true;
  $("progress").hidden = true;
  $("hint").hidden = true;
  if (!state.items.length) {
    $("summary").hidden = true;
    $("grid").replaceChildren();
  }
  $("grid").inert = true;
}

const TONES = { success: "i-check", danger: "i-alert", info: "i-alert" };

// The one status alert: done, partial failure, "tap again", share batches, auto-start blocked.
function notify(tone, title, text = "", actions = []) {
  const box = $("status");
  box.className = `alert alert--${tone}`;
  $("status-icon").setAttribute("href", `${SPRITE}#${TONES[tone]}`);
  $("status-title").textContent = title;
  $("status-text").textContent = text;
  $("status-text").hidden = !text;
  $("status-actions").replaceChildren(...actions);
  $("status-actions").hidden = actions.length === 0;
  box.hidden = false;
}

function setProgress(label, done, total) {
  $("progress").hidden = false;
  $("progress-label").textContent = label;
  $("progress-count").textContent = `${done} of ${total} · ${total ? Math.round((done / total) * 100) : 0}%`;
  $("bar").max = total || 1;
  $("bar").value = done;
}

function setTip(text) {
  $("tip").textContent = text || "";
  $("tip").hidden = !text;
}

const BUTTONS = ["btn-files", "btn-zip", "btn-share"];

// The active button keeps its colour and shows a spinner (aria-busy); the others are disabled.
function setBusy(active) {
  state.busy = active != null;
  for (const id of BUTTONS) {
    const b = $(id);
    if (b === active) b.setAttribute("aria-busy", "true");
    else b.removeAttribute("aria-busy");
    b.disabled = state.expired || (state.busy && b !== active);
  }
}

function setLabel(id, text) {
  $(id).querySelector(".btn__label").textContent = text;
}

function serverNow() {
  return Date.now() / 1000 + state.clockSkew;
}

function expired() {
  return serverNow() >= state.expiresAt;
}

function hasActivation() {
  return navigator.userActivation ? navigator.userActivation.isActive : true;
}

// --- rendering ---------------------------------------------------------------------

function groupOf(item) {
  if (item.kind === "image" || item.kind === "video" || item.kind === "audio") return item.kind;
  return EXT_GROUP.get(extOf(item.name)) || "file";
}

function typeIcon(item, group) {
  const box = el("span", "tile__type");
  box.append(icon(GROUP_ICON[group], "icon icon--lg"), el("span", "tile__ext", extOf(item.name).slice(0, 5) || group));
  return box;
}

function renderTile(item, i) {
  const li = el("li", "tile");
  li.id = `item-${i}`;
  const group = groupOf(item);
  li.dataset.kind = group;
  const media = el("div", "tile__media");
  if (item.thumb) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.replaceWith(typeIcon(item, group)), { once: true });
    img.src = item.thumb;
    media.append(img);
  } else {
    media.append(typeIcon(item, group));
  }
  if (item.duration && (item.kind === "video" || item.kind === "audio")) {
    const badge = el("span", "badge badge--overlay tile__corner");
    if (item.kind === "video") badge.append(icon("i-play"));
    badge.append(formatDuration(item.duration), el("span", "visually-hidden", ` ${item.kind}`));
    media.append(badge);
  } else if (item.kind === "video" && item.thumb) {
    const badge = el("span", "badge badge--overlay tile__corner");
    badge.append(icon("i-play"), el("span", "visually-hidden", "video"));
    media.append(badge);
  }
  const { folder, base } = splitPath(item.name);
  const body = el("div", "tile__body");
  const link = el("a", "tile__name", base);
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.title = base;
  const meta = [folder && `Msg ${Number(folder)}`, formatSize(item.size)].filter(Boolean).join(" · ");
  body.append(link, el("span", "tile__meta", meta));
  li.append(media, body);
  return li;
}

function renderGrid() {
  $("grid").replaceChildren(...state.items.map(renderTile));
}

function renderSummary() {
  const n = state.items.length;
  const total = state.items.reduce((s, it) => s + (it.size || 0), 0);
  const left = Math.ceil(state.expiresAt - serverNow());
  const parts = [plural(n, "file")];
  if (total) parts.push(formatSize(total));
  parts.push(expired() ? "link expired" : `link expires in ${formatDuration(left)}`);
  $("summary").textContent = parts.join(" · ");
  if (expired() && !state.busy && !state.expired) {
    state.expired = true;
    setBusy(null);
    showFatal("This link has expired", `Run ${COMMAND} on the message in Discord again to get a new link.`);
  }
}

function mark(i, ok, label) {
  const li = $(`item-${i}`);
  if (!li) return;
  li.classList.toggle("is-done", ok);
  li.classList.toggle("is-failed", !ok);
  li.querySelector(".tile__status")?.remove();
  const badge = el("span", `badge ${ok ? "badge--success" : "badge--danger"} tile__status`);
  badge.append(icon(ok ? "i-check" : "i-alert"), label);
  li.querySelector(".tile__media").append(badge);
}

// --- fetching ----------------------------------------------------------------------

async function fetchItem(item) {
  const res = await fetch(item.url, { mode: "cors", credentials: "omit", referrerPolicy: "no-referrer" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

// Yields {i, res} or {i, error} in order, keeping up to PARALLEL requests in flight.
async function* fetchInOrder(indices) {
  const queue = [];
  let next = 0;
  const pump = () => {
    while (queue.length < PARALLEL && next < indices.length) {
      const i = indices[next++];
      queue.push(fetchItem(state.items[i]).then((res) => ({ i, res }), (error) => ({ i, error })));
    }
  };
  pump();
  while (queue.length) {
    const result = await queue.shift();
    pump();
    yield result;
  }
}

function saveBlob(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
}

// counts: {ok, gone (not on Discord any more), failed (couldn't be written), renamed}
function finish(verb, counts, nextStep) {
  const { ok, gone, failed = 0 } = counts;
  const total = ok + gone + failed;
  $("hint").hidden = true;
  $("progress").hidden = true; // the alert below carries the result
  const problems = [];
  if (gone) problems.push(`${plural(gone, "file")} ${gone === 1 ? "is" : "are"} no longer available on Discord.`);
  if (failed) problems.push(`${plural(failed, "file")} couldn't be saved.`);
  if (counts.renamed) problems.push(`${plural(counts.renamed, "file")} got a “_” added to the name, because Windows blocks it.`);
  if (ok === 0) {
    notify("danger", "None of the files could be downloaded",
      `${problems.join(" ")} Run ${COMMAND} in Discord again to get fresh links.`);
  } else if (gone || failed) {
    const next = gone
      ? `If they still show in Discord, run ${COMMAND} again for fresh links.`
      : "Try again, or use Download ZIP.";
    notify("danger", `${verb} ${ok} of ${plural(total, "file")}`, [...problems, next].join(" "));
  } else {
    notify("success", `${verb} ${plural(ok, "file")}`, [nextStep, ...problems].filter(Boolean).join(" "));
  }
  setPhase("done");
}

function cancelled(text) {
  $("progress").hidden = true;
  setTip("");
  if (text) notify("info", text);
  setPhase("ready");
}

async function run(btn, fn) {
  if (state.busy || expired()) return;
  setBusy(btn);
  setPhase("busy");
  $("hint").hidden = true;
  $("error").hidden = true;
  $("status").hidden = true;
  setTip("");
  try {
    await fn();
  } catch (e) {
    $("progress").hidden = true;
    showError("Download failed", `${e.name}: ${e.message}. Try again, or use the other download button.`);
  } finally {
    setBusy(null);
  }
}

// --- Download all: one by one (<a download>) ---------------------------------------

async function saveEach(indices) {
  const total = indices.length;
  const counts = { ok: 0, gone: 0 };
  setProgress("Downloading files", 0, total);
  setTip(
    coarse()
      ? "If your browser asks to download multiple files, tap Allow."
      : "If your browser asks where to save every file, or saves only the first one, use Download ZIP instead.",
  );
  for await (const { i, res, error } of fetchInOrder(indices)) {
    try {
      if (error) throw error;
      saveBlob(await res.blob(), flatName(state.items[i].name));
      counts.ok++;
      mark(i, true, "Saved");
      await new Promise((r) => setTimeout(r, SAVE_GAP_MS));
    } catch {
      counts.gone++;
      mark(i, false, "Unavailable");
    }
    setProgress("Downloading files", counts.ok + counts.gone, total);
  }
  finish("Downloaded", counts, "Check your Downloads folder.");
}

// --- Download all: into a folder (Chromium desktop, File System Access) ------------

function canPickFolder() {
  return typeof window.showDirectoryPicker === "function" && !coarse() && window.top === window;
}

function splitExt(name) {
  const m = /^(.+?)((?:\.tar)?\.[A-Za-z0-9]{1,16})$/.exec(name);
  return m ? [m[1], m[2]] : [name, ""];
}

// A name Chrome will accept: no trailing dot/space, no reserved device name, no blocked type.
function folderSafeName(name) {
  let n = name.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").replace(/[. ]+$/, "") || "file";
  if (RESERVED.test(n.split(".")[0])) n = `_${n}`;
  if (FSA_BLOCKED_EXT.has(extOf(n)) || /\{[^}]*\}$/.test(n)) n = `${n}_`;
  return n;
}

async function exists(dir, name) {
  try {
    await dir.getFileHandle(name);
    return true;
  } catch (e) {
    return e.name !== "NotFoundError"; // TypeMismatchError: a folder has that name
  }
}

// "a.jpg" -> "a (1).jpg" while the folder (or this batch) already has the name.
async function freeName(dir, name, taken) {
  const [stem, ext] = splitExt(name);
  let candidate = name;
  for (let n = 1; taken.has(candidate.toLowerCase()) || (await exists(dir, candidate)); n++) {
    if (n > 999) throw new Error("no free name");
    candidate = `${stem} (${n})${ext}`;
  }
  taken.add(candidate.toLowerCase());
  return candidate;
}

async function saveToFolder(picker, indices) {
  let root;
  try {
    root = await picker;
  } catch (e) {
    if (e.name === "AbortError") return cancelled(); // the user closed the folder dialog
    if (e.name === "NotAllowedError") {
      return cancelled("Chrome wasn't allowed to save into that folder. Tap Download all to pick another one.");
    }
    return saveEach(indices); // SecurityError (no user activation) and the like
  }
  const total = indices.length;
  const counts = { ok: 0, gone: 0, failed: 0, renamed: 0 };
  const dirs = new Map([["", root]]);
  const taken = new Map();
  setProgress("Saving to folder", 0, total);
  for await (const { i, res, error } of fetchInOrder(indices)) {
    if (error) {
      counts.gone++;
      mark(i, false, "Unavailable");
    } else {
      try {
        const { folder, base } = splitPath(state.items[i].name);
        if (!dirs.has(folder)) dirs.set(folder, await root.getDirectoryHandle(folder, { create: true }));
        const dir = dirs.get(folder);
        if (!taken.has(folder)) taken.set(folder, new Set());
        const safe = folderSafeName(base);
        const name = await freeName(dir, safe, taken.get(folder));
        const handle = await dir.getFileHandle(name, { create: true });
        const writable = await handle.createWritable();
        if (res.body) await res.body.pipeTo(writable);
        else await writable.close();
        if (safe !== base) counts.renamed++;
        counts.ok++;
        mark(i, true, "Saved");
      } catch {
        res.body?.cancel().catch(() => {});
        counts.failed++;
        mark(i, false, "Not saved");
      }
    }
    setProgress("Saving to folder", counts.ok + counts.gone + counts.failed, total);
  }
  finish("Downloaded", counts, "They're in the folder you picked.");
}

function downloadAll(indices = state.items.map((_, i) => i), btn = $("btn-files")) {
  if (state.busy || expired()) return;
  if (canPickFolder() && hasActivation()) {
    let picker; // must be the first await-free call in the click, or Chrome refuses it
    try {
      picker = window.showDirectoryPicker({ id: "zipbot", mode: "readwrite", startIn: "downloads" });
    } catch (e) {
      picker = Promise.reject(e);
    }
    return run(btn, () => saveToFolder(picker, indices));
  }
  return run(btn, () => saveEach(indices));
}

// --- Download ZIP --------------------------------------------------------------------

function zipEntries(indices, counts) {
  const total = indices.length;
  setProgress("Zipping files", 0, total);
  return (async function* () {
    for await (const { i, res, error } of fetchInOrder(indices)) {
      if (error) {
        counts.gone++;
        mark(i, false, "Unavailable");
      } else {
        yield { name: state.items[i].name, input: res, lastModified: new Date() };
        counts.ok++;
        mark(i, true, "Zipped");
      }
      setProgress("Zipping files", counts.ok + counts.gone, total);
    }
  })();
}

async function saveZip(picker, indices, name) {
  const counts = { ok: 0, gone: 0 };
  if (picker) {
    let handle = null;
    try {
      handle = await picker;
    } catch (e) {
      if (e.name === "AbortError") return cancelled();
      // anything else: fall back to the in-memory ZIP below
    }
    if (handle) {
      await downloadZip(zipEntries(indices, counts)).body.pipeTo(await handle.createWritable());
      return finish("Zipped", counts, "The ZIP is where you saved it.");
    }
  }
  const blob = await downloadZip(zipEntries(indices, counts)).blob();
  if (counts.ok) saveBlob(blob, name);
  finish("Zipped", counts, `Check your Downloads folder for ${name}.`);
}

function downloadZipOf(indices = state.items.map((_, i) => i), btn = $("btn-zip")) {
  if (state.busy || expired()) return;
  const name = zipName();
  let picker = null;
  if (typeof window.showSaveFilePicker === "function" && !coarse() && hasActivation()) {
    try {
      picker = window.showSaveFilePicker({
        suggestedName: name,
        id: "zipbot",
        startIn: "downloads",
        types: [{ description: "ZIP archive", accept: { "application/zip": [".zip"] } }],
      });
    } catch {
      picker = null;
    }
  }
  return run(btn, () => saveZip(picker, indices, name));
}

// --- Share (Web Share Level 2) ---------------------------------------------------------

function canShareFiles() {
  try {
    return Boolean(navigator.share && navigator.canShare?.({ files: [new File([""], "a.png", { type: "image/png" })] }));
  } catch {
    return false;
  }
}

// The share sheet's own name rules: an extension, no "..", no slashes, no edge whitespace or dot.
function shareType(name) {
  if (/\.\.|[\\/]|^\s|\s$|\.$/.test(name)) return null;
  return SHARE_TYPES[extOf(name)] || null;
}

function planShare() {
  const batches = [];
  const excluded = [];
  let batch = [];
  let bytes = 0;
  state.items.forEach((item, i) => {
    const size = item.size || 0;
    if (!shareType(flatName(item.name)) || size > SHARE_MAX_BYTES) {
      excluded.push(i);
      return;
    }
    if (batch.length && (batch.length >= SHARE_MAX_FILES || bytes + size > SHARE_MAX_BYTES)) {
      batches.push(batch);
      batch = [];
      bytes = 0;
    }
    batch.push(i);
    bytes += size;
  });
  if (batch.length) batches.push(batch);
  // files/indices: the prepared batch (File objects and the items they came from)
  return { batches, excluded, next: 0, files: null, indices: [], ok: 0, gone: 0 };
}

// Fetch one batch into File objects with allowlisted types; files that turn out too big for
// this batch move into a batch of their own.
async function collectBatch(plan) {
  const indices = plan.batches[plan.next];
  const files = [];
  const used = [];
  const overflow = [];
  let bytes = 0;
  let done = 0;
  setProgress("Preparing files", 0, indices.length);
  for await (const { i, res, error } of fetchInOrder(indices)) {
    if (error) {
      plan.gone++;
      mark(i, false, "Unavailable");
    } else {
      const blob = await res.blob();
      if (blob.size > SHARE_MAX_BYTES) {
        plan.excluded.push(i);
      } else if (files.length && bytes + blob.size > SHARE_MAX_BYTES) {
        overflow.push(i);
      } else {
        const name = flatName(state.items[i].name);
        files.push(new File([blob], name, { type: shareType(name) }));
        used.push(i);
        bytes += blob.size;
      }
    }
    setProgress("Preparing files", ++done, indices.length);
  }
  if (overflow.length) plan.batches.splice(plan.next + 1, 0, overflow);
  plan.files = files.length ? files : null;
  plan.indices = used;
}

// Fetch batches until one has files to share (or none are left); true if one is ready.
async function prepare(plan) {
  while (!plan.files && plan.next < plan.batches.length) {
    await collectBatch(plan);
    if (!plan.files) plan.next++;
  }
  return Boolean(plan.files);
}

function offerOthers(indices) {
  return [
    button(`Download ${indices.length === 1 ? "it" : "them"}`, "btn--secondary btn--sm", () =>
      downloadAll(indices, $("btn-files")),
    ),
    button("Download as ZIP", "btn--ghost btn--sm", () => downloadZipOf(indices, $("btn-zip"))),
  ];
}

function excludedText(plan) {
  const n = plan.excluded.length;
  return `${plural(n, "file")} can't go to the share sheet (type or size it doesn't take): ${namesList(plan.excluded)}.`;
}

function resetShare() {
  state.share = null;
  setLabel("btn-share", "Share");
}

async function shareNext() {
  const plan = (state.share ??= planShare());
  if (!(await prepare(plan))) return shareDone(plan);
  const count = plan.batches.length;
  const batchNo = count > 1 ? ` (batch ${plan.next + 1} of ${count})` : "";
  if (!hasActivation()) {
    // share() needs a fresh tap; fetching took longer than the browser keeps the first one.
    $("progress").hidden = true;
    notify("info", "Files are ready — tap Share again.", `${plural(plan.files.length, "file")}${batchNo}.`);
    return setPhase("ready");
  }
  try {
    await navigator.share({ files: plan.files });
  } catch (e) {
    $("progress").hidden = true;
    if (e.name === "AbortError") {
      notify("info", "Sharing cancelled.", "Tap Share to try again.");
      return setPhase("ready");
    }
    if (e.name === "NotAllowedError") {
      notify("info", "Your browser didn't open the share sheet",
        "Tap Share to try again. If it still doesn't work, use Download all or Download ZIP.",
        [button("Download all", "btn--secondary btn--sm", () => downloadAll()),
          button("Download ZIP", "btn--ghost btn--sm", () => downloadZipOf())]);
      return setPhase("ready");
    }
    throw e;
  }
  for (const i of plan.indices) mark(i, true, "Shared");
  plan.ok += plan.files.length;
  plan.files = null;
  plan.next++;
  // Prepare the next batch now, so the next tap can share right away.
  if (!(await prepare(plan))) return shareDone(plan);
  setLabel("btn-share", `Share next batch (${plan.next + 1} of ${plan.batches.length})`);
  $("progress").hidden = true;
  notify("info", `Shared ${plural(plan.ok, "file")} so far`,
    `The share sheet takes up to ${SHARE_MAX_FILES} files (50 MB) at a time. Tap Share next batch to continue.`);
  setPhase("ready");
}

function shareDone(plan) {
  resetShare();
  if (plan.ok === 0 && plan.gone === 0) {
    $("progress").hidden = true;
    notify("info", "These files can't be shared", `${excludedText(plan)} Download them instead.`,
      offerOthers(plan.excluded));
    return setPhase("ready");
  }
  finish("Shared", { ok: plan.ok, gone: plan.gone });
  if (plan.excluded.length) {
    const box = $("status-text");
    box.textContent = [box.textContent, excludedText(plan)].filter(Boolean).join(" ");
    box.hidden = false;
    $("status-actions").replaceChildren(...offerOthers(plan.excluded));
    $("status-actions").hidden = false;
  }
}

// --- start -------------------------------------------------------------------------

function arrangeButtons() {
  $("btn-share").hidden = !canShareFiles() || !state.items.some((it) => shareType(flatName(it.name)));
  $("picker-note").hidden = !canPickFolder();
}

function riskyItems() {
  return state.items.flatMap((it, i) => (RISKY.has(extOf(it.name)) ? [i] : []));
}

function autoStart(action) {
  if (action === "files") {
    const risky = riskyItems();
    if (risky.length) {
      notify("info", "Tap Download all to start",
        `It didn't start by itself because some files can run code when opened: ${namesList(risky)}. ` +
          "Only open them if you trust whoever sent you this link.");
      return;
    }
  }
  if (action === "files") downloadAll();
  else downloadZipOf(); // a ZIP keeps the files inside the archive, so it may start by itself
  // run() hides the hint synchronously, so set it afterwards
  $("hint").textContent = `Your download should start automatically. If nothing happens, tap ${action === "zip" ? "Download ZIP" : "Download all"}.`;
  $("hint").hidden = false;
}

function retryButton() {
  return [button("Try again", "btn--secondary btn--sm", () => location.reload())];
}

async function main() {
  setPhase("loading");
  const [token, action] = location.hash.slice(1).split(".", 2);
  if (!token) {
    showFatal("No download link", `Open this page from the bot's message in Discord, or run ${COMMAND} on a message.`);
    return;
  }
  // Drop the action so a reload or Back doesn't start another download.
  if (action) history.replaceState(null, "", `${location.pathname}#${token}`);

  let res;
  try {
    res = await fetch("/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      cache: "no-store",
    });
  } catch {
    showFatal("Can't reach the server", "Check your connection, then try again.", retryButton());
    return;
  }
  if (res.status === 404) {
    showFatal("This link has expired or is invalid", `Run ${COMMAND} on the message in Discord again to get a new link.`);
    return;
  }
  if (res.status === 429) {
    showFatal("Too many requests", "Wait a minute, then try again.", retryButton());
    return;
  }
  if (!res.ok) {
    showFatal("Something went wrong", `The server answered ${res.status}. Try again later.`, retryButton());
    return;
  }
  const body = await res.json();
  const serverDate = Date.parse(res.headers.get("Date") || "");
  if (!Number.isNaN(serverDate)) state.clockSkew = (serverDate - Date.now()) / 1000;
  state.expiresAt = body.expires_at;
  state.items = body.items
    .map((it) => ({ ...it, url: safeUrl(it.url), thumb: it.thumb && safeUrl(it.thumb) }))
    .filter((it) => it.url);

  renderGrid();
  arrangeButtons();
  $("actions").hidden = false;
  $("btn-files").addEventListener("click", () => downloadAll());
  $("btn-zip").addEventListener("click", () => downloadZipOf());
  $("btn-share").addEventListener("click", () => run($("btn-share"), shareNext));
  renderSummary();
  if (state.expired) return;
  state.timer = setInterval(renderSummary, 1000);
  setPhase("ready");
  if (!state.items.length) {
    showFatal("No files left", `None of the files can be downloaded from here. Run ${COMMAND} in Discord again.`);
    return;
  }
  if (ACTIONS.has(action)) autoStart(action);
}

main();
