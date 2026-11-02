"""Landing page and install link in real browsers (Chromium, Firefox, WebKit), production CSP.

Discord itself is never contacted: discord.com is a Playwright route that records the request.
Run: pytest tests/e2e (browsers: `python -m playwright install`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from playwright.sync_api import Browser, BrowserContext, Error, Page, Route, sync_playwright

from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web.app import create_app

ENGINES = ["chromium", "firefox", "webkit"]
APP_ID = 100000000000000008  # fake snowflake


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    settings = Settings(discord_token="t", public_base_url="http://localhost")  # pyright: ignore[reportCallIssue]
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(create_app(settings, Store.from_settings(settings), application_id=lambda: APP_ID))

    async def start() -> int:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield f"http://127.0.0.1:{port}"
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


def _context(browser: Browser, **kw: Any) -> BrowserContext:
    context = browser.new_context(**kw)
    context.add_init_script(
        "window.__csp = [];"
        "document.addEventListener('securitypolicyviolation',"
        " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
    )
    context.route("https://discord.com/**", lambda route: route.abort())  # never reach the real Discord
    return context


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = _context(browser)
    page = context.new_page()
    yield page
    assert page.evaluate("window.__csp") == [], "CSP violations"
    context.close()


def test_landing_renders_themed_without_csp_violations(page: Page, base_url: str) -> None:
    statuses: dict[str, int] = {}
    page.on("response", lambda r: statuses.update({r.url.split("#")[0]: r.status}))
    page.goto(base_url + "/")
    page.wait_for_load_state("networkidle")
    for asset in ("style.css", "landing.css", "icons.svg"):
        assert statuses.get(f"{base_url}/static/{asset}") == 200, asset
    assert all(u.startswith(base_url) for u in statuses), statuses  # no third-party requests
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(246, 247, 249)"
    primary = page.get_by_role("link", name="Add to Discord")
    assert primary.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(71, 82, 196)"  # --accent
    art = page.locator(".hero__art svg")
    assert art.evaluate("e => e.getBoundingClientRect().width") > 200
    assert art.locator(".art-hill").evaluate("e => getComputedStyle(e).fill") == "rgb(123, 134, 232)"


def test_landing_structure_and_keyboard(page: Page, base_url: str) -> None:
    page.goto(base_url + "/")
    assert page.locator("main").count() == 1
    assert page.locator("h1").count() == 1
    assert page.get_by_role("heading", level=2, name="How it works").is_visible()
    assert page.locator(".steps > li").count() == 3
    assert page.get_by_role("img", name="A message with three attachments").is_visible()
    assert page.get_by_role("navigation", name="Site").is_visible()
    assert page.get_by_role("contentinfo").count() == 1  # footer is a sibling of <main>, not inside it
    assert page.get_by_text("Not affiliated with or endorsed by Discord.").count() == 2  # hero + footer
    # The first Tab stop is the primary call to action, with a visible focus ring.
    # WebKit, like Safari's default setting, skips links on Tab, so there it is focused directly.
    browser = page.context.browser
    if browser is not None and browser.browser_type.name == "webkit":
        page.get_by_role("link", name="Add to Discord").focus()
    else:
        page.keyboard.press("Tab")
    active = "document.activeElement"
    focused = page.evaluate(f"[{active}.textContent.trim(), getComputedStyle({active}).outlineStyle]")
    assert focused == ["Add to Discord", "solid"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Add to Discord", {"integration_type": ["1"], "scope": ["applications.commands"]}),
        (
            "Add to a server",
            {"integration_type": ["0"], "scope": ["applications.commands bot"], "permissions": ["0"]},
        ),
    ],
)
def test_install_buttons_redirect_to_discord_authorize(
    page: Page, base_url: str, name: str, expected: dict[str, list[str]]
) -> None:
    # Browsers follow redirects past Playwright routes, so the /install response is fetched
    # unfollowed and replaced by a stub: the real Discord is never contacted.
    redirects: list[tuple[int, str]] = []

    def capture(route: Route) -> None:
        resp = route.fetch(max_redirects=0)
        redirects.append((resp.status, resp.headers.get("location", "")))
        route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>captured</title>")

    page.route(f"{base_url}/install**", capture)
    page.goto(base_url + "/")
    page.get_by_role("link", name=name).click()
    page.wait_for_url(f"{base_url}/install**")
    [(status, location)] = redirects
    assert status == 302
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://discord.com/oauth2/authorize"
    assert parse_qs(parts.query) == {"client_id": [str(APP_ID)], **expected}


@pytest.mark.parametrize(("width", "scheme"), [(360, "light"), (360, "dark"), (1280, "dark")])
def test_landing_fits_viewport(
    browser: Browser, base_url: str, width: int, scheme: Literal["light", "dark"]
) -> None:
    context = _context(browser, viewport={"width": width, "height": 800})
    page = context.new_page()
    page.goto(base_url + "/")
    # Set after the first load: Firefox drops colour-scheme emulation on the about:blank → http switch.
    page.emulate_media(color_scheme=scheme)
    page.wait_for_load_state("networkidle")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "horizontal scroll"
    # the footer sits in main's content column
    edges = "e => { const r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; }"
    assert page.locator("body > footer").evaluate(edges) == page.locator(".steps").evaluate(edges)
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == ("rgb(30, 31, 34)" if scheme == "dark" else "rgb(246, 247, 249)")
    assert page.evaluate("window.__csp") == []
    context.close()
