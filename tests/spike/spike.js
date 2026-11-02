// Phase 0 spike: can the browser fetch Discord CDN files, ZIP them locally
// and save them (blob download) or hand them to the OS share sheet?
import { downloadZip } from "./client-zip.js";

const $ = (id) => document.getElementById(id);
const logEl = $("log");

function log(msg) {
  const line = `[spike] ${new Date().toISOString().slice(11, 23)} ${msg}`;
  console.log(line);
  logEl.textContent += line + "\n";
}

function urls() {
  return $("urls").value.split(/\s+/).filter(Boolean);
}

function nameOf(url, i) {
  const last = new URL(url).pathname.split("/").pop() || `file-${i}`;
  return decodeURIComponent(last);
}

// Same name twice -> "name (1).ext", mirroring the planned naming.py.
function dedupe(names) {
  const seen = new Map();
  return names.map((n) => {
    const count = seen.get(n) ?? 0;
    seen.set(n, count + 1);
    if (count === 0) return n;
    const dot = n.lastIndexOf(".");
    return dot > 0 ? `${n.slice(0, dot)} (${count})${n.slice(dot)}` : `${n} (${count})`;
  });
}

async function fetchAll() {
  const list = urls();
  const names = dedupe(list.map(nameOf));
  const t0 = performance.now();
  const files = await Promise.all(
    list.map(async (url, i) => {
      const res = await fetch(url, { mode: "cors", credentials: "omit", referrerPolicy: "no-referrer" });
      if (!res.ok) throw new Error(`HTTP ${res.status} for file #${i}`);
      const blob = await res.blob();
      return new File([blob], names[i], { type: blob.type || "application/octet-stream" });
    }),
  );
  const total = files.reduce((s, f) => s + f.size, 0);
  log(`fetched ${files.length} files, ${total} bytes in ${Math.round(performance.now() - t0)} ms`);
  return files;
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

function zipName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `discord-media-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.zip`;
}

async function buildZip() {
  const files = await fetchAll();
  const blob = await downloadZip(files.map((f) => ({ name: f.name, input: f, lastModified: new Date() }))).blob();
  log(`zip built: ${blob.size} bytes`);
  return new File([blob], zipName(), { type: "application/zip" });
}

function activation() {
  return navigator.userActivation ? `userActivation.isActive=${navigator.userActivation.isActive}` : "userActivation=n/a";
}

async function share(files) {
  const data = { files };
  if (!navigator.canShare?.(data)) {
    log(`canShare(files)=false for ${files.map((f) => f.type).join(",")}`);
    return;
  }
  log(`calling navigator.share (${activation()})`);
  try {
    await navigator.share(data);
    log("share resolved OK");
  } catch (e) {
    log(`share rejected: ${e.name}: ${e.message}`);
  }
}

// Fetching can outlast the transient user activation that share() needs,
// so the share buttons prefetch on first tap and share on the second if needed.
let prepared = { files: null, zip: null };

function guard(fn) {
  return async () => {
    try {
      await fn();
    } catch (e) {
      log(`ERROR ${e.name}: ${e.message}`);
    }
  };
}

$("zip").addEventListener("click", guard(async () => {
  const zip = await buildZip();
  saveBlob(zip, zip.name);
  log(`download triggered: ${zip.name}`);
}));

$("share-files").addEventListener("click", guard(async () => {
  if (!prepared.files || navigator.userActivation?.isActive === false) {
    prepared.files ??= await fetchAll();
    if (navigator.userActivation && !navigator.userActivation.isActive) {
      log("activation expired while fetching; tap Share files again");
      return;
    }
  }
  await share(prepared.files);
}));

$("share-zip").addEventListener("click", guard(async () => {
  if (!prepared.zip) {
    prepared.zip = await buildZip();
    if (navigator.userActivation && !navigator.userActivation.isActive) {
      log("activation expired while zipping; tap Share ZIP again");
      return;
    }
  }
  await share([prepared.zip]);
}));

// Individual files land in Downloads, which Android galleries index (unlike a ZIP).
$("singles").addEventListener("click", guard(async () => {
  const files = await fetchAll();
  for (const f of files) {
    saveBlob(f, f.name);
    await new Promise((r) => setTimeout(r, 300));
  }
  log(`individual downloads triggered: ${files.length}`);
}));

$("mock").addEventListener("click", () => {
  const base = `${location.protocol}//${location.hostname}:8081/files`;
  $("urls").value = ["photo.png", "photo.png", "clip.mp4", "big.bin"].map((n) => `${base}/${n}`).join("\n");
  prepared = { files: null, zip: null };
});

$("urls").addEventListener("input", () => {
  prepared = { files: null, zip: null };
});

const probe = [new File(["x"], "a.jpg", { type: "image/jpeg" })];
$("env").textContent = [
  `userAgent: ${navigator.userAgent}`,
  `isSecureContext: ${window.isSecureContext}`,
  `navigator.share: ${typeof navigator.share}`,
  `canShare(image file): ${navigator.canShare?.({ files: probe }) ?? "n/a"}`,
  `canShare(zip file): ${navigator.canShare?.({ files: [new File(["x"], "a.zip", { type: "application/zip" })] }) ?? "n/a"}`,
  `showSaveFilePicker: ${typeof window.showSaveFilePicker}`,
  `ReadableStream: ${typeof ReadableStream}`,
].join("\n");
// ?mock preloads the mock URLs; #<url> <url> ... preloads real ones (for adb-driven runs without CDP).
if (new URLSearchParams(location.search).has("mock")) $("mock").click();
if (location.hash.length > 1) $("urls").value = decodeURIComponent(location.hash.slice(1)).split(" ").join("\n");
log("ready");
