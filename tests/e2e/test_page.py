"""Download page in real browsers (Chromium, Firefox, WebKit) against the real aiohttp app.

Discord's CDN is replaced by Playwright routes, so the page keeps its production CSP and
URL allowlist. Run: pytest tests/e2e (browsers: `python -m playwright install`).
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
        elif name in self.missing or name not in self.files:
            route.fulfill(status=404, body=b"", headers={"Access-Control-Allow-Origin": "*"})
        else:
            route.fulfill(
                status=200,
                body=self.files[name],
                headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/octet-stream"},
            )


@pytest.fixture
def cdn() -> Cdn:
    return Cdn()


@pytest.fixture
def page(browser: Browser, cdn: Cdn) -> Iterator[Page]:
    context = browser.new_context(accept_downloads=True)
    context.add_init_script(
        "window.__csp = [];"
        "document.addEventListener('securitypolicyviolation',"
        " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
    )
    context.route("https://cdn.discordapp.com/**", cdn.handle)
    context.route("https://media.discordapp.net/**", cdn.handle)
    page = context.new_page()
    yield page
    assert page.evaluate("window.__csp") == [], "CSP violations"
    context.close()


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


def zip_entries(path: str | Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {n: z.read(n) for n in z.namelist()}


def wait_state(page: Page, *states: str, timeout: float = 15_000) -> str:
    sel = ", ".join(f"body[data-state={s}]" for s in states)
    page.wait_for_selector(sel, timeout=timeout, state="attached")
    return page.evaluate("document.body.dataset.state")


# --- tests -------------------------------------------------------------------------


def test_zip_download(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("a.jpg", 0), ("a.jpg", 0), ("b.mp4", 1)])
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    assert page.locator("#grid li").count() == 3
    assert "3 files" in (page.text_content("#summary") or "")
    with page.expect_download() as dl:
        page.click("#btn-zip")
    assert dl.value.suggested_filename.startswith("discord-media-")
    entries = zip_entries(dl.value.path())
    assert entries == {"01/a.jpg": b"a.jpg", "01/a (1).jpg": b"a.jpg", "02/b.mp4": b"b.mp4"}
    wait_state(page, "done")
    assert "Zipped 3 files" in (page.text_content("#status") or "")


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


def test_auto_zip_from_fragment(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("x.png", 0)])
    with page.expect_download() as dl:
        page.goto(f"{base}/d#{token}.zip")
    assert zip_entries(dl.value.path()) == {"x.png": b"x.png"}
    wait_state(page, "done")
    assert page.is_hidden("#hint")  # the "should start automatically" hint goes away once done
    assert page.evaluate("location.hash") == f"#{token}"  # action dropped: reload won't re-download


def test_save_each_file(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("one.jpg", 0), ("two.jpg", 1), ("three.jpg", 1)])
    downloads: list[Any] = []
    page.on("download", lambda d: downloads.append(d))
    page.goto(f"{base}/d#{token}.files")
    wait_state(page, "done")
    deadline = time.time() + 5
    while len(downloads) < 3 and time.time() < deadline:
        page.wait_for_timeout(100)
    assert [d.suggested_filename for d in downloads] == ["01-one.jpg", "02-two.jpg", "02-three.jpg"]
    assert [Path(d.path()).read_bytes() for d in downloads] == [b"one.jpg", b"two.jpg", b"three.jpg"]
    assert "Saved 3 files" in (page.text_content("#status") or "")


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
    assert "1 file is no longer available" in (page.text_content("#status") or "")
    assert page.locator("#item-1.failed").count() == 1


def test_invalid_token(page: Page, server: tuple[str, Store]) -> None:
    base, _ = server
    page.goto(f"{base}/d#{'x' * 43}")
    wait_state(page, "error")
    assert "expired or is invalid" in (page.text_content("#error-title") or "")
    assert page.is_hidden("#actions")


def test_no_token(page: Page, server: tuple[str, Store]) -> None:
    page.goto(f"{server[0]}/d")
    wait_state(page, "error")
    assert "No download link" in (page.text_content("#error-title") or "")


def _stub_resolve(page: Page, body: dict[str, Any], date: str | None = None) -> None:
    headers = {"Content-Type": "application/json", **({"Date": date} if date else {})}
    page.route("**/api/resolve", lambda r: r.fulfill(status=200, body=json.dumps(body), headers=headers))


def _raw_item(name: str, url: str = "https://cdn.discordapp.com/attachments/1/1/f0-a.jpg") -> dict[str, Any]:
    return {"url": url, "thumb": None, "name": name, "kind": "image", "mime": "image/jpeg",
            "size": 1, "width": 1, "height": 1, "duration": None}  # fmt: skip


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


def test_expiry_countdown_disables_buttons(page: Page, server: tuple[str, Store]) -> None:
    _stub_resolve(page, {"expires_at": int(time.time()) + 2, "items": [_raw_item("a.jpg")]})
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "error", timeout=6000)
    assert "expired" in (page.text_content("#error-title") or "")
    assert page.is_disabled("#btn-zip") and page.is_disabled("#btn-files")


def test_expiry_uses_server_clock(page: Page, server: tuple[str, Store]) -> None:
    # Device clock 2 h behind the server: the link must still count down from server time.
    server_now = time.time() + 7200
    date = email.utils.formatdate(server_now, usegmt=True)
    _stub_resolve(page, {"expires_at": int(server_now) + 600, "items": [_raw_item("a.jpg")]}, date)
    page.goto(f"{server[0]}/d#{'y' * 43}")
    wait_state(page, "ready")
    summary = page.text_content("#summary") or ""
    assert "link expires in 10:0" in summary or "link expires in 9:5" in summary, summary


def test_share_button(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    token = make_link(store, cdn, [("p.jpg", 0), ("q.jpg", 0)], ctype="image/jpeg")
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    hidden_without_api = page.evaluate("!navigator.canShare") and page.is_hidden("#btn-share")
    page.add_init_script(
        "navigator.canShare = () => true;"
        "navigator.share = async (d) => { window.__shared = d.files.map(f => f.name + ':' + f.type); };"
    )
    page.reload()
    wait_state(page, "ready")
    assert page.is_visible("#btn-share")
    page.click("#btn-share")
    state = wait_state(page, "done", "ready")
    if state == "ready":  # activation expired while fetching: second tap shares
        page.click("#btn-share")
        wait_state(page, "done")
    assert page.evaluate("window.__shared") == ["p.jpg:image/jpeg", "q.jpg:image/jpeg"]
    assert hidden_without_api or page.evaluate("!!navigator.canShare")


def test_mobile_prefers_save_to_phone(browser: Browser, server: tuple[str, Store], cdn: Cdn) -> None:
    if browser.browser_type.name == "firefox":
        pytest.skip("Firefox has no mobile emulation")
    base, store = server
    token = make_link(store, cdn, [("m.jpg", 0)])
    ctx = browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 390, "height": 800})
    ctx.route("https://media.discordapp.net/**", cdn.handle)
    page = ctx.new_page()
    page.goto(f"{base}/d#{token}")
    wait_state(page, "ready")
    first = page.locator("#actions .buttons button").first
    assert first.get_attribute("id") == "btn-files"
    assert "primary" in (first.get_attribute("class") or "")
    ctx.close()


def test_desktop_prefers_zip(page: Page, server: tuple[str, Store], cdn: Cdn) -> None:
    base, store = server
    page.goto(f"{base}/d#{make_link(store, cdn, [('d.jpg', 0)])}")
    wait_state(page, "ready")
    assert page.locator("#actions .buttons button").first.get_attribute("id") == "btn-zip"


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
