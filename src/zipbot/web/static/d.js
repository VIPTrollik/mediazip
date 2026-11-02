// Download page: resolve the token, then fetch every file straight from Discord's CDN
// and either save them one by one, ZIP them locally, or hand them to the share sheet.
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

const $ = (id) => document.getElementById(id);
// clockSkew: server time minus device time, from the resolve response's Date header.
const state = { items: [], expiresAt: 0, busy: false, shareFiles: null, timer: 0, clockSkew: 0 };

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
  return `discord-media-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.zip`;
}

function showError(title, text) {
  $("error-title").textContent = title;
  $("error-text").textContent = text;
  $("error").hidden = false;
  setPhase("error");
}

function setStatus(text, done = null, total = null) {
  $("progress").hidden = false;
  $("status").textContent = text;
  if (done != null && total) $("bar").value = done / total;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["btn-files", "btn-zip", "btn-share"]) $(id).disabled = busy || expired();
}

function serverNow() {
  return Date.now() / 1000 + state.clockSkew;
}

function expired() {
  return serverNow() >= state.expiresAt;
}

// --- rendering ---------------------------------------------------------------------

function renderGrid() {
  const grid = $("grid");
  grid.replaceChildren();
  state.items.forEach((item, i) => {
    const li = document.createElement("li");
    li.id = `item-${i}`;
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    if (item.thumb) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.src = item.thumb;
      thumb.append(img);
    }
    if (item.kind === "video") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.duration ? `▶ ${formatDuration(item.duration)}` : "▶";
      thumb.append(badge);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = item.name;
    meta.append(link);
    if (item.size != null) meta.append(` · ${formatSize(item.size)}`);
    li.append(thumb, meta);
    grid.append(li);
  });
}

function renderSummary() {
  const n = state.items.length;
  const total = state.items.reduce((s, it) => s + (it.size || 0), 0);
  const left = Math.ceil(state.expiresAt - serverNow());
  const parts = [`${n} file${n === 1 ? "" : "s"}`];
  if (total) parts.push(formatSize(total));
  parts.push(expired() ? "link expired" : `link expires in ${formatDuration(left)}`);
  $("summary").textContent = parts.join(" · ");
  if (expired() && !state.busy) {
    clearInterval(state.timer);
    setBusy(false);
    showError("This link has expired", "Run “Download attachments” in Discord again to get a new link.");
  }
}

function arrangeButtons() {
  const mobile = matchMedia("(pointer: coarse)").matches;
  const primary = $(mobile ? "btn-files" : "btn-zip");
  primary.classList.add("primary");
  primary.parentElement.prepend(primary);
  $("btn-share").hidden = !(navigator.canShare && navigator.share);
}

function mark(i, cls) {
  $(`item-${i}`)?.classList.add(cls);
}

// --- fetching ----------------------------------------------------------------------

async function fetchItem(item) {
  const res = await fetch(item.url, { mode: "cors", credentials: "omit", referrerPolicy: "no-referrer" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

// Yields {i, res} or {i, error} in order, keeping up to PARALLEL requests in flight.
async function* fetchInOrder(items) {
  const queue = [];
  let next = 0;
  const pump = () => {
    while (queue.length < PARALLEL && next < items.length) {
      const i = next++;
      queue.push(fetchItem(items[i]).then((res) => ({ i, res }), (error) => ({ i, error })));
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

function finish(ok, failed, verb) {
  const total = ok + failed;
  $("hint").hidden = true;
  if (failed === 0) {
    setStatus(`${verb} ${ok} file${ok === 1 ? "" : "s"}.`, total, total);
  } else {
    setStatus(`${verb} ${ok} of ${total}. ${failed} file${failed === 1 ? " is" : "s are"} no longer available on Discord.`, total, total);
  }
  setPhase("done");
}

async function run(fn) {
  if (state.busy || expired()) return;
  setBusy(true);
  setPhase("busy");
  $("hint").hidden = true;
  try {
    await fn();
  } catch (e) {
    showError("Download failed", `${e.name}: ${e.message}`);
  } finally {
    setBusy(false);
  }
}

async function saveFiles() {
  const total = state.items.length;
  let ok = 0;
  let failed = 0;
  setStatus(`Saving 0 of ${total}…`, 0, total);
  for await (const { i, res, error } of fetchInOrder(state.items)) {
    try {
      if (error) throw error;
      saveBlob(await res.blob(), state.items[i].name.replace("/", "-"));
      ok++;
      mark(i, "done");
      await new Promise((r) => setTimeout(r, SAVE_GAP_MS));
    } catch {
      failed++;
      mark(i, "failed");
    }
    setStatus(`Saving ${ok + failed} of ${total}…`, ok + failed, total);
  }
  finish(ok, failed, "Saved");
}

async function zipBlob() {
  const total = state.items.length;
  let ok = 0;
  let failed = 0;
  setStatus(`Zipping 0 of ${total}…`, 0, total);
  async function* entries() {
    for await (const { i, res, error } of fetchInOrder(state.items)) {
      if (error) {
        failed++;
        mark(i, "failed");
      } else {
        yield { name: state.items[i].name, input: res, lastModified: new Date() };
        ok++;
        mark(i, "done");
      }
      setStatus(`Zipping ${ok + failed} of ${total}…`, ok + failed, total);
    }
  }
  const blob = await downloadZip(entries()).blob();
  return { blob, ok, failed };
}

async function saveZip() {
  const { blob, ok, failed } = await zipBlob();
  if (ok === 0) throw new Error("none of the files could be downloaded");
  saveBlob(blob, zipName());
  finish(ok, failed, "Zipped");
}

async function collectShareFiles() {
  const files = [];
  let failed = 0;
  for await (const { i, res, error } of fetchInOrder(state.items)) {
    if (error) {
      failed++;
      mark(i, "failed");
      continue;
    }
    const blob = await res.blob();
    const name = state.items[i].name.replace("/", "-");
    files.push(new File([blob], name, { type: state.items[i].mime || blob.type || "application/octet-stream" }));
  }
  return { files, failed };
}

// share() needs a fresh user gesture; if fetching took too long, ask for a second tap.
async function share() {
  if (!state.shareFiles) {
    state.shareFiles = await collectShareFiles();
    if (state.shareFiles.files.length === 0) {
      const { failed } = state.shareFiles;
      state.shareFiles = null;
      finish(0, failed, "Shared");
      return;
    }
    if (navigator.userActivation && !navigator.userActivation.isActive) {
      setStatus("Files are ready — tap Share again.");
      setPhase("ready");
      return;
    }
  }
  const { files, failed } = state.shareFiles;
  if (!navigator.canShare({ files })) {
    setStatus("This browser cannot share these files; use Save to phone instead.");
    setPhase("done");
    return;
  }
  try {
    await navigator.share({ files });
    finish(files.length, failed, "Shared");
    if (failed) state.shareFiles = null; // a later tap retries the files that failed
  } catch (e) {
    if (e.name !== "AbortError") throw e;
    setStatus("Sharing cancelled.");
    setPhase("ready");
  }
}

const ACTION_FN = { files: saveFiles, zip: saveZip };

// --- start -------------------------------------------------------------------------

async function main() {
  setPhase("loading");
  const [token, action] = location.hash.slice(1).split(".", 2);
  if (!token) {
    showError("No download link", "Open this page from the bot's message in Discord.");
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
    showError("Can't reach the server", "Check your connection and reload the page.");
    return;
  }
  if (res.status === 404) {
    showError("This link has expired or is invalid", "Run “Download attachments” in Discord again to get a new link.");
    return;
  }
  if (res.status === 429) {
    showError("Too many requests", "Wait a minute, then reload the page.");
    return;
  }
  if (!res.ok) {
    showError("Something went wrong", `The server answered ${res.status}. Try again later.`);
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
  $("btn-files").addEventListener("click", () => run(saveFiles));
  $("btn-zip").addEventListener("click", () => run(saveZip));
  $("btn-share").addEventListener("click", () => run(share));
  renderSummary();
  state.timer = setInterval(renderSummary, 1000);
  setPhase("ready");
  if (expired()) return;

  if (ACTIONS.has(action)) {
    run(ACTION_FN[action]); // hides the hint synchronously, so set it afterwards
    $("hint").textContent = "Your download should start automatically. If nothing happens, tap the button above.";
    $("hint").hidden = false;
  }
}

main();
