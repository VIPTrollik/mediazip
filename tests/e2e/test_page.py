"""Download page in real browsers (Chromium, Firefox, WebKit) against the real aiohttp app.

Discord's CDN is replaced by Playwright routes, so the page keeps its production CSP and
URL allowlist. Run: pytest tests/e2e (browsers: `python -m playwright install`).

The File System Access pickers are removed by default (as in Firefox/Safari, and so headless
Chromium never opens a native dialog); the picker tests install fakes that record writes.
"""

from __future__ import annotations

import asyncio
import email.utils
import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from playwright.sync_api import Browser, Error, Page, Route, sync_playwright

from helpers import item
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web.app import create_app

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf0000030101008a5d2b0d0000000049454e44ae426082"
)
ENGINES = ["chromium", "firefox", "webkit"]
CSP_PROBE = (
    "window.__csp = [];"
    "document.addEventListener('securitypolicyviolation',"
    " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
)
NO_PICKERS = (
    "for (const k of ['showDirectoryPicker', 'showSaveFilePicker']) {"
    " delete Window.prototype[k]; delete window[k]; }"
)
# A fake folder for showDirectoryPicker, modelled on Chrome: `tree` is nested objects (folders)
# and strings (files); getFileHandle refuses the names Chrome refuses and creates an empty file
# as soon as it's called with create; a writable only replaces the file on close, and an error
# leaves it as it was. opts: breakWrite (a substring of names whose writes fail), deny (every
# lookup throws NotAllowedError, as after the permission was revoked).
FAKE_FS = """
window.__fs = { calls: [], tree: %(tree)s, written: [], removed: [], probes: 0 };
const OPTS = %(opts)s;
const notFound = () => new DOMException("not found", "NotFoundError");
function fakeDir(path, node) {
  return {
    kind: "directory",
    async getDirectoryHandle(name, opts) {
      if (!(name in node)) { if (!opts?.create) throw notFound(); node[name] = {}; }
      if (typeof node[name] !== "object") throw new DOMException("file", "TypeMismatchError");
      return fakeDir(path + name + "/", node[name]);
    },
    async getFileHandle(name, opts) {
      if (!opts?.create) window.__fs.probes++;
      if (OPTS.deny) throw new DOMException("denied", "NotAllowedError");
      if (/\\.(lnk|url|scf|local|ini|cfg|manifest|dll)$|\\.$/i.test(name)) {
        throw new TypeError("Name is not allowed.");
      }
      if (!(name in node)) { if (!opts?.create) throw notFound(); node[name] = ""; }
      if (typeof node[name] === "object") throw new DOMException("folder", "TypeMismatchError");
      return {
        kind: "file",
        async createWritable() {
          const parts = [];
          return new WritableStream({
            write(chunk) {
              if (OPTS.breakWrite && name.includes(OPTS.breakWrite)) {
                throw new DOMException("disk full", "QuotaExceededError");
              }
              parts.push(chunk);
            },
            async close() {
              node[name] = await new Blob(parts).text();
              window.__fs.written.push(path + name);
            },
          });
        },
      };
    },
    async removeEntry(name) {
      if (!(name in node)) throw notFound();
      delete node[name];
      window.__fs.removed.push(path + name);
    },
  };
}
window.showDirectoryPicker = async (opts) => {
  window.__fs.calls.push(opts);
  const fail = %(fail)s;
  if (fail) throw new DOMException("picker", fail);
  return fakeDir("", window.__fs.tree);
};
"""
# showSaveFilePicker: the picked file exists (empty) once the picker resolves, like in Chrome.
# opts: failWrite (every write fails).
FAKE_SAVE_PICKER = """
window.__save = { calls: [], bytes: null, aborted: false, removed: false };
const SAVE = %(opts)s;
window.showSaveFilePicker = async (opts) => {
  window.__save.calls.push(opts);
  return {
    async createWritable() {
      const parts = [];
      return new WritableStream({
        write(chunk) {
          if (SAVE.failWrite) throw new DOMException("disk full", "QuotaExceededError");
          parts.push(chunk);
        },
        async close() {
          window.__save.bytes = Array.from(new Uint8Array(await new Blob(parts).arrayBuffer()));
        },
        abort() { window.__save.aborted = true; },
      });
    },
    async remove() { window.__save.removed = true; },
  };
};
"""
# fail: null, an error name (every call fails) or {"once": name} (only the first call fails)
FAKE_SHARE = """
window.__shared = [];
let shareFail = %(fail)s;
navigator.canShare = () => true;
navigator.share = async (d) => {
  if (typeof shareFail === "string") throw new DOMException("Permission denied", shareFail);
  if (shareFail?.once) {
    const name = shareFail.once;
    shareFail = null;
    throw new DOMException("share failed", name);
  }
  window.__shared.push(d.files.map((f) => f.name + ":" + f.type));
};
"""
NO_SHARE = "delete Navigator.prototype.share; delete Navigator.prototype.canShare;"


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, Store]]:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        discord_token="t", public_base_url="http://localhost", resolve_rate_per_min=10_000
    )
    store = Store.from_settings(settings)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(create_app(settings, store))

    async def start() -> int:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield f"http://127.0.0.1:{port}", store
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture(scope="module", params=ENGINES)
def browser(request: pytest.FixtureRequest) -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            b = getattr(p, request.param).launch()
        except Error as e:
            pytest.skip(f"{request.param} not installed: {e}")
        yield b
        b.close()


class Cdn:
    """Fake cdn.discordapp.com / media.discordapp.net with CORS `*`, like the real one."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.missing: set[str] = set()
        self.requests: list[str] = []
        self.hold: set[str] = set()
        self.held: list[Route] = []

    def handle(self, route: Route) -> None:
        url = route.request.url
        self.requests.append(url)
        name = url.split("?")[0].rsplit("/", 1)[-1]
        if route.request.url.startswith("https://media.discordapp.net/"):
            route.fulfill(
                status=200,
                body=PNG,
                headers={"Access-Control-Allow-Origin": "*", "Content-Type": "image/png"},
            )
        elif name in self.hold:
            self.held.append(route)
        elif name in self.missing or name not in self.files:
            route.fulfill(status=404, body=b"", headers={"Access-Control-Allow-Origin": "*"})
        else:
            route.fulfill(
                status=200,
                body=self.files[name],
                headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/octet-stream"},
            )

    def release(self) -> None:
        for route in self.held:
            name = route.request.url.split("?")[0].rsplit("/", 1)[-1]
            route.fulfill(status=200, body=self.files[name], headers={"Access-Control-Allow-Origin": "*"})
        self.held.clear()


@pytest.fixture
def cdn() -> Cdn:
    return Cdn()


def _context_page(browser: Browser, cdn: Cdn, **kw: Any) -> Iterator[Page]:
    context = browser.new_context(accept_downloads=True, **kw)
    context.add_init_script(CSP_PROBE)
    context.add_init_script(NO_PICKERS)
    context.route("https://cdn.discordapp.com/**", cdn.handle)
    context.route("https://media.discordapp.net/**", cdn.handle)
    page = context.new_page()
    yield page
    assert page.evaluate("window.__csp") == [], "CSP violations"
    context.close()


@pytest.fixture
def page(browser: Browser, cdn: Cdn) -> Iterator[Page]:
    yield from _context_page(browser, cdn)


@pytest.fixture
def phone(browser: Browser, cdn: Cdn) -> Iterator[Page]:
    """A touch phone (pointer: coarse)."""
    if browser.browser_type.name == "firefox":
        pytest.skip("Firefox has no mobile emulation")
    yield from _context_page(
        browser, cdn, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 800}
    )


def make_link(store: Store, cdn: Cdn, names: list[tuple[str, int]], **kw: Any) -> str:
    """names: (filename, source message index). File body = its own name, for easy checks."""
    items = []
    for n, (name, source) in enumerate(names):
        key = f"f{n}-{name}"
        cdn.files[key] = name.encode()
        items.append(
            item(
                name,
                source=source,
                url=f"https://cdn.discordapp.com/attachments/1/{n}/{key}?ex=1&is=1&hm=0&",
                proxy_url=f"https://media.discordapp.net/attachments/1/{n}/{key}?ex=1&is=1&hm=0&",
                cdn_expires_at=None,
                size=len(name),
                **kw,
            )
        )
    token, _ = store.create_link(1, items)
    return token


def zip_entries(path: str | Path | bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(path) if isinstance(path, bytes) else path) as z:
        assert z.testzip() is None
        return {n: z.read(n) for n in z.namelist()}


def wait_state(page: Page, *states: str, timeout: float = 15_000) -> str:
    sel = ", ".join(f"body[data-state={s}]" for s in states)
    page.wait_for_selector(sel, timeout=timeout, state="attached")
    return page.evaluate("document.body.dataset.state")


def collect_downloads(page: Page) -> list[Any]:
    downloads: list[Any] = []
    page.on("download", lambda d: downloads.append(d))
    return downloads


def wait_downloads(page: Page, downloads: list[Any], n: int) -> None:
    deadline = time.time() + 5
    while len(downloads) < n and time.time() < deadline:
        page.wait_for_timeout(100)


def status(page: Page) -> str:
    return page.text_content("#status") or ""


# --- ZIP -------------------------------------------------------------------------


def test_zip_download(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0), ("a.jpg", 0), ("b.mp4", 1)])
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert page.locator("#grid li").count() == 3
    assert "3 files" in (page.text_content("#summary") or "")
    with page.expect_download() as dl:
        page.click("#btn-zip")
    assert dl.value.suggested_filename.startswith("discord-files-")
    entries = zip_entries(dl.value.path())
    assert entries == {"01/a.jpg": b"a.jpg", "01/a (1).jpg": b"a.jpg", "02/b.mp4": b"b.mp4"}
    wait_state(page, "done")
    assert "Zipped 3 files" in status(page)
    assert page.locator("#grid .tile.is-done .tile__status").count() == 3


def test_zip_of_any_file_type(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    names = ["report.pdf", "notes.txt", "photos.zip", "Makefile", "backup.tar.gz", ".env"]
    token = make_link(store, cdn, [(n, 0) for n in names], ctype=None)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert page.locator("#grid li").count() == 6
    assert page.locator("#grid img").count() == 0  # files get no thumbnail
    with page.expect_download() as dl:
        page.click("#btn-zip")
    entries = zip_entries(dl.value.path())
    assert entries == {
        "report.pdf": b"report.pdf",
        "notes.txt": b"notes.txt",
        "photos.zip": b"photos.zip",
        "Makefile": b"Makefile",
        "backup.tar.gz": b"backup.tar.gz",
        "env": b".env",
    }


def test_zip_streams_into_save_file_picker(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0), ("b.pdf", 1)])
    _fake_save_picker(page)
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-zip")
    wait_state(page, "done")
    (opts,) = page.evaluate("window.__save.calls")
    assert opts["suggestedName"].startswith("discord-files-") and opts["startIn"] == "downloads"
    assert zip_entries(bytes(page.evaluate("window.__save.bytes"))) == {
        "01/a.jpg": b"a.jpg",
        "02/b.pdf": b"b.pdf",
    }
    assert downloads == []  # no Blob download on top
    assert "Zipped 2 files" in status(page)


def test_auto_zip_from_fragment(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("x.png", 0)])
    with page.expect_download() as dl:
        page.goto(f"{base}/d#{token}.zip")
    assert zip_entries(dl.value.path()) == {"x.png": b"x.png"}
    wait_state(page, "done")
    assert page.is_hidden("#hint")  # the "should start automatically" hint goes away once done
    assert page.evaluate("location.hash") == f"#{token}"  # action dropped: reload won't re-download


def test_auto_zip_starts_even_with_risky_types(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    # Inside a ZIP nothing runs by itself, so the .zip action keeps auto-starting.
    base, store = server
    token = make_link(store, cdn, [("setup.exe", 0), ("page.html", 0)], ctype=None)
    with page.expect_download() as dl:
        page.goto(f"{base}/d#{token}.zip")
    assert zip_entries(dl.value.path()) == {"setup.exe": b"setup.exe", "page.html": b"page.html"}


def test_missing_cdn_file_is_reported(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("ok.jpg", 0), ("gone.jpg", 0)])
    cdn.missing.add("f1-gone.jpg")
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    with page.expect_download() as dl:
        page.click("#btn-zip")
    assert zip_entries(dl.value.path()) == {"ok.jpg": b"ok.jpg"}
    wait_state(page, "done")
    assert "1 file is no longer available" in status(page)
    assert "alert--danger" in (page.get_attribute("#status", "class") or "")
    assert page.text_content("#item-1.is-failed .tile__status") == "Unavailable"  # not colour-only


def test_busy_marks_the_active_button(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("slow.jpg", 0)])
    cdn.hold.add("f0-slow.jpg")
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-zip")
    wait_state(page, "busy")
    page.wait_for_function("document.querySelector('#btn-zip').getAttribute('aria-busy') === 'true'")
    assert not page.is_disabled("#btn-zip")  # keeps its colour; clicks are ignored while busy
    assert page.is_disabled("#btn-files")
    assert page.is_visible("#progress") and "0 of 1" in (page.text_content("#progress-count") or "")
    with page.expect_download():
        cdn.release()
    wait_state(page, "done")
    assert page.get_attribute("#btn-zip", "aria-busy") is None and not page.is_disabled("#btn-files")


# --- Download all ----------------------------------------------------------------


def test_save_each_file(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("one.jpg", 0), ("two.jpg", 1), ("three.jpg", 1)])
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}.files")
    wait_state(page, "done")
    wait_downloads(page, downloads, 3)
    assert [d.suggested_filename for d in downloads] == ["01-one.jpg", "02-two.jpg", "02-three.jpg"]
    assert [Path(d.path()).read_bytes() for d in downloads] == [b"one.jpg", b"two.jpg", b"three.jpg"]
    assert "Downloaded 3 files" in status(page)


def _fake_fs(page: Page, tree: dict[str, Any] | None = None, fail: str | None = None, **opts: Any) -> None:
    fake = FAKE_FS % {"tree": json.dumps(tree or {}), "fail": json.dumps(fail), "opts": json.dumps(opts)}
    page.add_init_script(fake)


def _fake_save_picker(page: Page, **opts: Any) -> None:
    page.add_init_script(FAKE_SAVE_PICKER % {"opts": json.dumps(opts)})


def test_download_all_into_picked_folder(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    names = [("a.jpg", 0), ("a.jpg", 0), ("desktop.ini", 1), ("gone.jpg", 1), ("b.pdf", 1)]
    token = make_link(store, cdn, names, ctype=None)
    cdn.missing.add("f3-gone.jpg")
    _fake_fs(page, {"01": {"a.jpg": "old"}})
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert page.is_visible("#picker-note")  # "pick a folder inside Downloads"
    page.click("#btn-files")
    wait_state(page, "done")
    (opts,) = page.evaluate("window.__fs.calls")
    assert opts == {"id": "zipbot", "mode": "readwrite", "startIn": "downloads"}
    assert page.evaluate("window.__fs.tree") == {
        "01": {"a.jpg": "old", "a (1).jpg": "a.jpg", "a (1) (1).jpg": "a.jpg"},  # never overwrites
        "02": {"desktop.ini_": "desktop.ini", "b.pdf": "b.pdf"},  # same layout as the ZIP
    }
    assert downloads == []
    text = status(page)
    assert "Downloaded 4 of 5 files" in text and "1 file is no longer available" in text, text
    assert "1 file got a “_” added" in text, text
    assert page.locator("#item-3.is-failed").count() == 1
    assert page.locator("#grid .is-done").count() == 4


def test_download_all_folder_picker_cancelled(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0)])
    _fake_fs(page, fail="AbortError")
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-files")
    page.wait_for_function("window.__fs.calls.length === 1")
    assert wait_state(page, "ready", "done", "error") == "ready"
    page.wait_for_timeout(300)
    assert downloads == [] and not [u for u in cdn.requests if u.startswith("https://cdn.discordapp.com/")]
    assert page.is_hidden("#error") and page.is_hidden("#status") and page.is_hidden("#progress")
    assert not page.is_disabled("#btn-files") and not page.is_disabled("#btn-zip")


@pytest.mark.parametrize("why", ["missing", "SecurityError"])
def test_download_all_falls_back_to_downloads(
    page: Page, server: tuple[str, Store], cdn: Cdn, why: str
) -> None:
    base, store = server
    token = make_link(store, cdn, [("one.jpg", 0), ("two.jpg", 0)])
    if why != "missing":
        _fake_fs(page, fail=why)
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert page.is_visible("#picker-note") == (why != "missing")
    page.click("#btn-files")
    wait_state(page, "done")
    wait_downloads(page, downloads, 2)
    assert [d.suggested_filename for d in downloads] == ["one.jpg", "two.jpg"]
    assert "Download ZIP" in (page.text_content("#tip") or "") and page.is_visible("#tip")


def test_auto_start_needs_a_tap_for_risky_types(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("photo.jpg", 0), ("setup.exe", 0), ("page.html", 0)], ctype=None)
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}.files")
    wait_state(page, "ready")
    page.wait_for_timeout(500)
    assert downloads == []
    assert page.evaluate("location.hash") == f"#{token}"
    text = status(page)
    assert "Tap Download all" in text and "setup.exe" in text and "page.html" in text, text
    page.click("#btn-files")  # one tap and it goes
    wait_state(page, "done")
    wait_downloads(page, downloads, 3)
    assert len(downloads) == 3


def test_download_all_on_phone_uses_downloads(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    # Android has a directory picker too, but it can't pick Download/: phones keep <a download>.
    base, store = server
    token = make_link(store, cdn, [("m.jpg", 0), ("n.jpg", 0)])
    _fake_fs(phone)
    downloads = collect_downloads(phone)
    phone.goto(f"{base}/d#{token}")
    wait_state(phone, "ready")
    assert phone.is_hidden("#picker-note")
    phone.click("#btn-files")
    wait_state(phone, "done")
    wait_downloads(phone, downloads, 2)
    assert len(downloads) == 2 and phone.evaluate("window.__fs.calls") == []


def test_folder_write_failure_leaves_no_empty_file(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("good.jpg", 0), ("bad.jpg", 0)])
    _fake_fs(page, breakWrite="bad")
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-files")
    wait_state(page, "done")
    assert page.evaluate("window.__fs.tree") == {"good.jpg": "good.jpg"}  # the empty bad.jpg is gone
    assert page.evaluate("window.__fs.removed") == ["bad.jpg"]
    assert "1 file couldn't be saved" in status(page)
    assert page.text_content("#item-1 .tile__status") == "Not saved"


def test_folder_permission_revoked_fails_fast(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0), ("b.jpg", 0)])
    _fake_fs(page, deny=True)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-files")
    wait_state(page, "done")
    assert page.evaluate("window.__fs.probes") == 2  # one lookup per file, not 999 retries
    assert "None of the files could be downloaded" in status(page)


def test_zip_picker_with_nothing_to_zip(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("gone.jpg", 0)])
    cdn.missing.add("f0-gone.jpg")
    _fake_save_picker(page)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-zip")
    wait_state(page, "done")
    assert page.evaluate("window.__save.bytes") is None  # never closed: no empty ZIP
    assert page.evaluate("window.__save.aborted && window.__save.removed")
    assert "None of the files could be downloaded" in status(page)


def test_zip_picker_write_error(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0)])
    _fake_save_picker(page, failWrite=True)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    page.click("#btn-zip")
    wait_state(page, "done")
    assert page.evaluate("window.__save.bytes") is None and page.evaluate("window.__save.removed")
    text = status(page)
    assert "couldn't be finished" in text and "Nothing was saved" in text, text


@pytest.mark.parametrize(
    ("mime", "risky"),
    [("application/x-msdownload", True), ("text/html", True), (None, True), ("image/png", False)],
)
def test_auto_start_checks_type_of_extensionless_files(
    page: Page, server: tuple[str, Store], cdn: Cdn, mime: str | None, risky: bool
) -> None:
    base, store = server
    token = make_link(store, cdn, [("setup", 0)], ctype=mime)
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}.files")
    if risky:
        wait_state(page, "ready")
        page.wait_for_timeout(500)
        assert downloads == [] and "setup" in status(page)
        page.click("#btn-files")
    wait_state(page, "done")
    wait_downloads(page, downloads, 1)
    # saved as octet-stream: the browser must not add .exe/.html from the CDN's type
    assert [d.suggested_filename for d in downloads] == ["setup"]


def test_auto_start_blocked_for_more_risky_types(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0), ("app.appref-ms", 0), ("tool.py", 0)], ctype=None)
    downloads = collect_downloads(page)
    page.goto(f"{base}/d#{token}.files")
    wait_state(page, "ready")
    page.wait_for_timeout(500)
    text = status(page)
    assert downloads == [] and "app.appref-ms" in text and "tool.py" in text, text


def test_auto_start_sees_through_disguised_names(page: Page, server: tuple[str, Store]) -> None:
    # Bypasses the server sanitizer: "evil.exe." and a zero-width space are still .exe files.
    items = [_raw_item("evil.exe."), _raw_item("tricky.e​xe"), _raw_item("fine.jpg")]
    _stub_resolve(page, {"expires_at": int(time.time()) + 600, "items": items})
    downloads = collect_downloads(page)
    page.goto(f"{server[0]}/d#{'y' * 43}.files")
    wait_state(page, "ready")
    page.wait_for_timeout(500)
    text = status(page)
    assert downloads == [] and "evil.exe." in text and "tricky" in text, text


# --- rendering -------------------------------------------------------------------


def test_mixed_file_types_render_tiles(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    names = ["photo.jpg", "clip.mov", "report.pdf", "src.zip", "song.mp3", "bot.py", "blob.bin", "Makefile"]
    token = make_link(store, cdn, [(n, 0 if i < 2 else 1) for i, n in enumerate(names)], ctype=None)
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    tiles = page.evaluate(
        """[...document.querySelectorAll('#grid .tile')].map(li => ({
            kind: li.dataset.kind,
            img: !!li.querySelector('.tile__media img'),
            icon: li.querySelector('.tile__type use')?.getAttribute('href').split('#')[1] ?? null,
            ext: li.querySelector('.tile__ext')?.textContent ?? null,
            name: li.querySelector('.tile__name').textContent,
            title: li.querySelector('.tile__name').title,
            meta: li.querySelector('.tile__meta').textContent,
        }))"""
    )
    by_name = {t["name"]: t for t in tiles}
    assert by_name["photo.jpg"]["img"] and by_name["clip.mov"]["img"]
    assert page.locator("#item-1 .tile__corner use").get_attribute("href") == "/static/icons.svg#i-play"
    expected = {
        "report.pdf": ("i-document", "pdf"),
        "src.zip": ("i-archive", "zip"),
        "song.mp3": ("i-audio", "mp3"),
        "bot.py": ("i-code", "py"),
        "blob.bin": ("i-file", "bin"),
        "Makefile": ("i-file", "file"),
    }
    assert {n: (by_name[n]["icon"], by_name[n]["ext"]) for n in expected} == expected
    assert (
        page.evaluate("getComputedStyle(document.querySelector('.tile__ext')).textTransform") == "uppercase"
    )
    # the 01/ folder prefix is a quiet "Msg 1" meta line, not part of the name
    assert by_name["photo.jpg"]["title"] == "photo.jpg"
    assert by_name["photo.jpg"]["meta"].startswith("Msg 1 · ") and by_name["bot.py"]["meta"].startswith(
        "Msg 2"
    )


def test_invalid_token(page: Page, server: tuple[str, Store]) -> None:
    base, _ = server
    page.goto(f"{base}/d#{'x' * 43}")
    wait_state(page, "error")
    assert "expired or is invalid" in (page.text_content("#error-title") or "")
    assert "Download attachments" in (page.text_content("#error-text") or "")  # says what to do
    assert page.is_hidden("#actions")
    assert page.locator("#grid li").count() == 0  # skeleton tiles gone
    assert "Loading" not in (page.text_content("#summary") or "") or page.is_hidden("#summary")


def test_no_token(page: Page, server: tuple[str, Store]) -> None:
    page.goto(f"{server[0]}/d")
    wait_state(page, "error")
    assert "No download link" in (page.text_content("#error-title") or "")
    assert page.is_hidden("#summary")


def test_server_error_offers_retry(page: Page, server: tuple[str, Store]) -> None:
    page.route("**/api/resolve", lambda r: r.fulfill(status=503, body="{}"))
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "error")
    assert page.is_hidden("#summary")
    assert page.is_visible("#error-actions button:has-text('Try again')")


def test_loading_shows_skeleton(page: Page, server: tuple[str, Store]) -> None:
    held: list[Route] = []
    page.route("**/api/resolve", lambda r: held.append(r))
    page.goto(f"{server[0]}/d#{'y' * 43}")
    assert page.evaluate("document.body.dataset.state") == "loading"
    assert page.locator("#grid .tile--skeleton").count() > 0
    assert "Loading" in (page.text_content("#summary") or "")
    for r in held:
        r.fulfill(status=404, body="{}")


def _stub_resolve(page: Page, body: dict[str, Any], date: str | None = None) -> None:
    headers = {"Content-Type": "application/json", **({"Date": date} if date else {})}
    page.route("**/api/resolve", lambda r: r.fulfill(status=200, body=json.dumps(body), headers=headers))


def _raw_item(
    name: str, url: str = "https://cdn.discordapp.com/attachments/1/1/f0-a.jpg", **kw: Any
) -> dict[str, Any]:
    return {"url": url, "thumb": None, "name": name, "kind": "image", "mime": "image/jpeg",
            "size": 1, "width": 1, "height": 1, "duration": None, **kw}  # fmt: skip


def test_hostile_names_render_as_text(page: Page, server: tuple[str, Store]) -> None:
    # Bypasses the server sanitizer on purpose: the page must be safe on its own.
    evil = '<img src=x onerror="window.__xss=1">'
    _stub_resolve(page, {"expires_at": int(time.time()) + 600, "items": [_raw_item(evil)]})
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "ready")
    assert page.text_content("#grid a") == evil
    assert page.locator("#grid img").count() == 0
    assert page.evaluate("window.__xss") is None


def test_foreign_urls_are_dropped(page: Page, server: tuple[str, Store]) -> None:
    items = [
        _raw_item("ok.jpg"),
        _raw_item("evil.jpg", "https://evil.example/x.jpg"),
        _raw_item("js", "javascript:alert(1)"),
    ]
    _stub_resolve(page, {"expires_at": int(time.time()) + 600, "items": items})
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "ready")
    assert page.locator("#grid li").count() == 1


def test_expired_link_hides_actions(page: Page, server: tuple[str, Store]) -> None:
    _stub_resolve(page, {"expires_at": int(time.time()) + 2, "items": [_raw_item("a.jpg")]})
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "ready")
    page.focus("#btn-files")
    wait_state(page, "error", timeout=6000)
    assert page.evaluate("document.activeElement.id") == "error"  # focus isn't lost with the buttons
    assert "expired" in (page.text_content("#error-title") or "")
    assert "Download attachments" in (page.text_content("#error-text") or "")  # what to do next
    assert page.is_hidden("#actions")
    assert page.is_disabled("#btn-zip") and page.is_disabled("#btn-files")
    assert page.evaluate("document.getElementById('grid').inert") is True
    assert "link expired" in (page.text_content("#summary") or "")


def test_expiry_uses_server_clock(page: Page, server: tuple[str, Store]) -> None:
    # Device clock 2 h behind the server: the link must still count down from server time.
    server_now = time.time() + 7200
    date = email.utils.formatdate(server_now, usegmt=True)
    _stub_resolve(page, {"expires_at": int(server_now) + 600, "items": [_raw_item("a.jpg")]}, date)
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "ready")
    summary = page.text_content("#summary") or ""
    assert "link expires in 10:0" in summary or "link expires in 9:5" in summary, summary


# --- share -----------------------------------------------------------------------


def share_until_done(page: Page, max_taps: int = 8) -> list[str]:
    """Tap Share until the page is done; returns the button labels seen before each tap."""
    labels = []
    for _ in range(max_taps):
        labels.append((page.text_content("#btn-share") or "").strip())
        page.click("#btn-share")
        if wait_state(page, "done", "ready") == "done":
            return labels
    raise AssertionError(f"share never finished: {labels}")


def test_share_button(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("p.jpg", 0), ("q.jpg", 0)], ctype="image/jpeg")
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    # without the fake: visible exactly when this engine can share a PNG file
    supported = page.evaluate(
        "!!(navigator.share && navigator.canShare"
        " && navigator.canShare({files: [new File([''], 'a.png', {type: 'image/png'})]}))"
    )
    assert page.is_visible("#btn-share") == supported
    page.add_init_script(NO_SHARE)
    page.reload()
    wait_state(page, "ready")
    assert page.is_hidden("#btn-share")  # no Web Share API: no button
    page.add_init_script(FAKE_SHARE % {"fail": "null"})
    page.reload()
    wait_state(page, "ready")
    assert page.is_visible("#btn-share")
    share_until_done(page)
    assert page.evaluate("window.__shared") == [["p.jpg:image/jpeg", "q.jpg:image/jpeg"]]


def test_share_retry_after_failure_counts_once(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("p.jpg", 0), ("gone.jpg", 0), ("q.jpg", 0)])
    cdn.missing.add("f1-gone.jpg")
    phone.add_init_script(FAKE_SHARE % {"fail": '{"once": "DataError"}'})
    phone.goto(f"{base}/d#{token}")
    wait_state(phone, "ready")
    phone.click("#btn-share")
    for _ in range(3):  # the first real share() attempt fails unexpectedly
        if wait_state(phone, "error", "ready") == "error":
            break
        phone.click("#btn-share")  # "tap Share again"
    assert phone.is_visible("#actions")  # a failed attempt leaves the buttons usable
    share_until_done(phone)
    assert phone.evaluate("window.__shared") == [["p.jpg:image/jpeg", "q.jpg:image/jpeg"]]
    text = status(phone)
    assert "Shared 2 of 3 files" in text and "1 file is no longer available" in text, text


def test_share_in_batches_skips_unshareable(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    names = ["notes.txt", *[f"p{n:02d}.jpg" for n in range(12)], "photos.zip", "clip.mov"]
    token = make_link(store, cdn, [(n, 0) for n in names], ctype=None)
    phone.add_init_script(FAKE_SHARE % {"fail": "null"})
    phone.goto(f"{base}/d#{token}")
    wait_state(phone, "ready")
    labels = share_until_done(phone)
    shared = phone.evaluate("window.__shared")
    assert [len(b) for b in shared] == [10, 3]  # at most 10 files per share sheet
    flat = [f for b in shared for f in b]
    assert flat[0] == "notes.txt:text/plain"  # exact allowlisted type, no charset
    assert all(f.endswith(":image/jpeg") for f in flat[1:])
    assert not [f for f in flat if "zip" in f or "mov" in f]
    assert "Share next batch (2 of 2)" in labels
    text = status(phone)
    assert "Shared 13 files" in text and "2 files can't go to the share sheet" in text, text
    assert "photos.zip" in text and "clip.mov" in text
    assert phone.is_visible("#status-actions button:has-text('Download them')")
    assert (phone.text_content("#btn-share") or "").strip() == "Share"


def test_share_batches_by_size(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    mib = 1024 * 1024
    items = []
    for n in range(3):
        url = f"https://cdn.discordapp.com/attachments/1/{n}/big{n}.mp4"
        cdn.files[f"big{n}.mp4"] = b"x"
        items.append(_raw_item(f"big{n}.mp4", url, kind="video", mime="video/mp4", size=30 * mib))
    items.append(_raw_item("huge.mp4", "https://cdn.discordapp.com/attachments/1/9/huge.mp4", size=60 * mib))
    _stub_resolve(phone, {"expires_at": int(time.time()) + 600, "items": items})
    phone.add_init_script(FAKE_SHARE % {"fail": "null"})
    phone.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(phone, "ready")
    share_until_done(phone)
    assert phone.evaluate("window.__shared") == [
        [f"big{n}.mp4:video/mp4"] for n in range(3)
    ]  # <= 50 MiB each
    assert "huge.mp4" in status(phone)


def test_share_skips_files_too_big_by_content_length(
    phone: Page, server: tuple[str, Store], cdn: Cdn
) -> None:
    # No size from Discord: the Content-Length tells the page not to load 51 MiB for sharing.
    cdn.files["big.mp4"] = b"\0" * (51 * 1024 * 1024)
    cdn.files["small.mp4"] = b"x"
    items = [
        _raw_item(
            n, f"https://cdn.discordapp.com/attachments/1/1/{n}", kind="video", mime="video/mp4", size=None
        )
        for n in ("big.mp4", "small.mp4")
    ]
    _stub_resolve(phone, {"expires_at": int(time.time()) + 600, "items": items})
    phone.add_init_script(FAKE_SHARE % {"fail": "null"})
    phone.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(phone, "ready")
    share_until_done(phone)
    assert phone.evaluate("window.__shared") == [["small.mp4:video/mp4"]]
    assert "big.mp4" in status(phone)


def test_share_refused_is_explained(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("p.jpg", 0)])
    phone.add_init_script(FAKE_SHARE % {"fail": '"NotAllowedError"'})
    phone.goto(f"{base}/d#{token}")
    wait_state(phone, "ready")
    phone.click("#btn-share")
    for _ in range(3):
        if "share sheet" in status(phone):
            break
        wait_state(phone, "ready")
        phone.click("#btn-share")  # "tap Share again" when the first tap's activation ran out
    wait_state(phone, "ready")
    assert phone.is_hidden("#error")
    assert "didn't open the share sheet" in status(phone)
    assert phone.is_visible("#status-actions button:has-text('Download ZIP')")


# --- buttons, theme, privacy -----------------------------------------------------


def test_phone_buttons(phone: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    phone.goto(f"{base}/d#{make_link(store, cdn, [('m.jpg', 0)])}")
    wait_state(phone, "ready")
    first = phone.locator("#actions .actions button").first
    assert first.get_attribute("id") == "btn-files"
    assert "btn--primary" in (first.get_attribute("class") or "")
    assert (first.text_content() or "").strip() == "Download all"


def test_desktop_buttons(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    # Download all is primary on desktop as well now; ZIP is the secondary button.
    base, store = server
    page.goto(f"{base}/d#{make_link(store, cdn, [('d.jpg', 0)])}")
    wait_state(page, "ready")
    ids = page.eval_on_selector_all("#actions .actions button:not([hidden])", "bs => bs.map(b => b.id)")
    assert ids[:2] == ["btn-files", "btn-zip"]
    assert "btn--primary" in (page.get_attribute("#btn-files", "class") or "")
    assert "btn--secondary" in (page.get_attribute("#btn-zip", "class") or "")


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms", "/d"])
def test_themed_pages_render_without_csp_violations(page: Page, server: tuple[str, Store], path: str) -> None:
    # The `page` fixture fails on any CSP violation; here we also check the theme and sprite actually load.
    statuses: dict[str, int] = {}
    page.on("response", lambda r: statuses.update({r.url.split("#")[0]: r.status}))
    page.goto(server[0] + path)
    page.wait_for_load_state("networkidle")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(246, 247, 249)"
    assert statuses.get(f"{server[0]}/static/style.css") == 200
    if path == "/d":
        assert statuses.get(f"{server[0]}/static/icons.svg") == 200


def test_no_third_party_requests(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto(f"{base}/d#{make_link(store, cdn, [('t.jpg', 0)])}")
    wait_state(page, "ready")
    page.wait_for_load_state("networkidle")
    allowed = (base, "https://cdn.discordapp.com/", "https://media.discordapp.net/")
    assert seen and all(u.startswith(allowed) for u in seen), seen
    assert all("#" not in u for u in seen if u.startswith(base + "/api"))


def test_token_not_sent_in_urls(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("s.jpg", 0)])
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url + " " + (r.headers.get("referer") or "")))
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert not [u for u in seen if token in u]


def test_zip_of_large_file(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("big.bin", 0)], ctype="video/mp4")
    big = bytes(range(256)) * (8 * 1024 * 1024 // 256)
    cdn.files["f0-big.bin"] = big
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    with page.expect_download() as dl:
        page.click("#btn-zip")
    with zipfile.ZipFile(io.BytesIO(Path(dl.value.path()).read_bytes())) as z:
        assert z.read("big.bin") == big
