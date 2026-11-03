"""Cookieless Google Analytics in real browsers (Chromium, Firefox, WebKit), production CSP.

Google is never contacted: every Google host is a Playwright route that records the request
and aborts it, so gtag.js never runs and the checks read the page's own dataLayer queue.
Run: pytest tests/e2e (browsers: `python -m playwright install`).
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from playwright.sync_api import Browser, BrowserContext, Error, Page, Request, Route, sync_playwright

from helpers import item
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web.app import create_app

ENGINES = ["chromium", "firefox", "webkit"]
GA_ID = "G-TEST1234"
GOOGLE = re.compile(
    r"^https?://([^/]*\.)?(google-analytics\.com|analytics\.google\.com|googletagmanager\.com|google\.com"
    r"|doubleclick\.net|googlesyndication\.com|gstatic\.com)(:\d+)?/"
)
CSP_PROBE = (
    "window.__csp = [];"
    "document.addEventListener('securitypolicyviolation',"
    " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
)
# The queued gtag() calls: each entry is an Arguments object; Dates become their type name.
QUEUE = "(window.dataLayer || []).map(a => Array.from(a, v => v instanceof Date ? 'Date' : v))"


@pytest.fixture(scope="module")
def server() -> Iterator[tuple[str, Store]]:
    settings = Settings(  # pyright: ignore[reportCallIssue]
        discord_token="t", public_base_url="http://localhost", ga_measurement_id=GA_ID
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


class Google:
    """Every request to a Google host, recorded and aborted."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def handle(self, route: Route) -> None:
        self.urls.append(route.request.url)
        route.abort()


@pytest.fixture
def google() -> Google:
    return Google()


@pytest.fixture
def context(browser: Browser, google: Google) -> Iterator[BrowserContext]:
    context = browser.new_context()
    context.add_init_script(CSP_PROBE)
    context.route(GOOGLE, google.handle)
    context.route("https://discord.com/**", lambda route: route.abort())
    context.route("https://cdn.discordapp.com/**", lambda route: route.abort())
    context.route("https://media.discordapp.net/**", lambda route: route.abort())
    yield context
    context.close()


@pytest.fixture
def page(context: BrowserContext) -> Iterator[Page]:
    page = context.new_page()
    yield page
    assert page.evaluate("window.__csp") == [], "CSP violations"


def gtag_requests(google: Google) -> list[str]:
    return [u for u in google.urls if urlsplit(u).path == "/gtag/js"]


def is_gtag(request: Request) -> bool:
    return request.url.startswith("https://www.googletagmanager.com/gtag/js?")


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
def test_consent_denied_before_config_and_no_fragment(
    page: Page, server: tuple[str, Store], google: Google, path: str
) -> None:
    base, _ = server
    with page.expect_request(is_gtag):  # the created gtag.js request (routed and aborted)
        page.goto(f"{base}{path}?utm_source=x#secret-fragment")
    queue: list[list[Any]] = page.evaluate(QUEUE)
    commands = [entry[0] for entry in queue]
    assert commands[:3] == ["consent", "js", "config"]  # the consent default comes first
    assert queue[0][1:] == [
        "default",
        {
            "analytics_storage": "denied",
            "ad_storage": "denied",
            "ad_user_data": "denied",
            "ad_personalization": "denied",
        },
    ]
    _, measurement_id, params = queue[2]
    assert measurement_id == GA_ID
    assert params == {"anonymize_ip": True, "page_location": f"{base}{path}"}  # no query, no fragment
    assert "secret" not in str(queue) and "utm_source" not in str(queue)
    (loader,) = gtag_requests(google)  # gtag.js was asked for (and aborted): the CSP allows it
    assert parse_qs(urlsplit(loader).query) == {"id": [GA_ID]}
    assert all("secret" not in u for u in google.urls)
    assert page.evaluate("document.cookie") == ""  # no cookies


def test_download_page_never_talks_to_google(
    context: BrowserContext, server: tuple[str, Store], google: Google
) -> None:
    base, store = server
    token, _ = store.create_link(
        1, [item("a.jpg", cdn_expires_at=None), item("b.mp4", ctype="video/mp4", cdn_expires_at=None)]
    )
    page = context.new_page()
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url))
    for fragment in (token, f"{token}.zip", "not-a-token"):
        page.goto("about:blank")  # a full load each time, not a same-document fragment change
        page.goto(f"{base}/d#{fragment}")
        page.wait_for_function("document.body.dataset.state !== 'loading'")
        assert page.evaluate("typeof window.dataLayer") == "undefined"
        assert page.locator('meta[name="ga-id"]').count() == 0
        assert page.locator('script[src*="analytics"], script[src*="google"]').count() == 0
    assert google.urls == []  # no request to a Google host, ever
    assert not [u for u in seen if GOOGLE.match(u)]
    assert page.evaluate("window.__csp") == []


def test_landing_to_download_page_keeps_google_out(
    page: Page, server: tuple[str, Store], google: Google
) -> None:
    """Analytics on the landing page doesn't follow the visitor onto /d."""
    base, store = server
    token, _ = store.create_link(1, [item("a.jpg", cdn_expires_at=None)])
    with page.expect_request(is_gtag):
        page.goto(f"{base}/")
    before = list(google.urls)
    assert gtag_requests(google)
    page.goto(f"{base}/d#{token}")
    page.wait_for_function("document.body.dataset.state !== 'loading'")
    assert google.urls == before
    assert page.evaluate("typeof window.dataLayer") == "undefined"
