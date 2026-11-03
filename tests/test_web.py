from __future__ import annotations

import asyncio
import json
import logging
import re
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest

from helpers import EX, item
from zipbot.__main__ import check_application_id
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web import app as web_app
from zipbot.web.app import SECURITY_HEADERS, ApplicationId, RateLimiter, client_ip, create_app, limit_key

SCHEMA = json.loads((Path(__file__).parent / "contract" / "resolve.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store.from_settings(settings)
    s.clock = lambda: EX - 24 * 3600
    return s


@pytest.fixture
async def client(aiohttp_client: Any, settings: Settings, store: Store) -> Any:
    return await aiohttp_client(create_app(settings, store))


async def test_healthz(client: Any) -> None:
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/healthz", "/does-not-exist", "/", "/d", "/static/d.js"])
async def test_security_headers_on_every_response(client: Any, path: str) -> None:
    resp = await client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value


async def test_csp_is_strict(client: Any) -> None:
    csp = (await client.get("/healthz")).headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "unsafe" not in csp
    assert "frame-ancestors 'none'" in csp


async def test_pages_and_static(client: Any) -> None:
    pages = ["/", "/d", "/privacy", "/terms"]
    for path, ctype in [*((page, "text/html") for page in pages), ("/static/d.js", "javascript")]:
        resp = await client.get(path)
        assert resp.status == 200
        assert ctype in resp.headers["Content-Type"]
        assert resp.headers["Cache-Control"] == "no-cache"
    assert (await client.get("/static/")).status in {403, 404}
    assert (await client.get("/static/../app.py")).status == 404


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
async def test_copy_is_about_images_and_videos(client: Any, path: str) -> None:
    html = await (await client.get(path)).text()
    for old in ("any file type", "PDF", "archives", "Download attachments", "files of any type"):
        assert old not in html, old
    assert "Download media" in html or path == "/terms"


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms", "/d", "/install"])
async def test_pages_carry_the_product_name(client: Any, path: str) -> None:
    html = await (await client.get(path)).text()  # /install: the 503 page (bot not logged in)
    assert re.search(r"<title>[^<]*MediaZip[^<]*</title>", html)
    assert "MediaZip" in html
    for old in ("Discord media downloader", "basket", "Basket"):
        assert old not in html, old


@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/static/icons.svg", "image/svg+xml"),
        ("/static/style.css", "text/css"),
        ("/static/landing.css", "text/css"),
    ],
)
async def test_theme_assets_served_with_security_headers(client: Any, path: str, ctype: str) -> None:
    resp = await client.get(path)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith(ctype)  # nosniff: a wrong type would break <use>/CSS
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value


def _query(location: str) -> dict[str, list[str]]:
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://discord.com/oauth2/authorize"
    return parse_qs(parts.query)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/install", {"integration_type": ["1"], "scope": ["applications.commands"]}),
        ("/install?type=user", {"integration_type": ["1"], "scope": ["applications.commands"]}),
        ("/install?type=bogus", {"integration_type": ["1"], "scope": ["applications.commands"]}),
        (
            "/install?type=guild",
            {"integration_type": ["0"], "scope": ["applications.commands bot"], "permissions": ["0"]},
        ),
    ],
)
async def test_install_redirects_to_discord(
    aiohttp_client: Any, settings: Settings, store: Store, path: str, expected: dict[str, list[str]]
) -> None:
    client = await aiohttp_client(create_app(settings, store, application_id=lambda: 1234567890123))
    resp = await client.get(path, allow_redirects=False)
    assert resp.status == 302
    assert _query(resp.headers["Location"]) == {"client_id": ["1234567890123"], **expected}
    assert resp.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]


class StubBot:
    """Stands in for discord.Client: application_id is None until login."""

    def __init__(self) -> None:
        self.application_id: int | None = None
        self.ready = asyncio.Event()

    async def wait_until_ready(self) -> None:
        await self.ready.wait()


async def _install_location(client: Any) -> int | str:
    resp = await client.get("/install", allow_redirects=False)
    return _query(resp.headers["Location"])["client_id"][0] if resp.status == 302 else resp.status


async def test_install_uses_bot_id_once_logged_in(
    aiohttp_client: Any, settings: Settings, store: Store
) -> None:
    app_id = ApplicationId(None)
    client = await aiohttp_client(create_app(settings, store, application_id=app_id))
    assert await _install_location(client) == 503  # no client yet
    bot = StubBot()
    app_id.bot = bot
    assert await _install_location(client) == 503  # client exists, not logged in
    bot.application_id = 77
    assert await _install_location(client) == "77"


async def test_install_configured_id_is_the_pre_login_fallback(
    aiohttp_client: Any, settings: Settings, store: Store
) -> None:
    s = settings.model_copy(update={"discord_application_id": 42})
    app = create_app(s, store)  # default source: ApplicationId(DISCORD_APPLICATION_ID)
    app_id = app[web_app.APPLICATION_ID_KEY]
    assert isinstance(app_id, ApplicationId)
    client = await aiohttp_client(app)
    assert await _install_location(client) == "42"
    bot = StubBot()
    app_id.bot = bot
    assert await _install_location(client) == "42"
    bot.application_id = 77  # the real id wins after login
    assert await _install_location(client) == "77"


async def test_application_id_mismatch_warns_after_login(caplog: pytest.LogCaptureFixture) -> None:
    app_id = ApplicationId(42)
    bot = StubBot()
    app_id.bot = bot
    task = asyncio.create_task(check_application_id(cast(Any, bot), app_id))
    await asyncio.sleep(0)
    assert not task.done() and not caplog.records  # waits for login
    bot.application_id = 77
    bot.ready.set()
    with caplog.at_level(logging.WARNING, logger="zipbot.web.app"):
        await asyncio.wait_for(task, 1)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "42" in record.getMessage() and "77" in record.getMessage()


@pytest.mark.parametrize("configured", [None, 77])
async def test_application_id_match_is_silent(
    caplog: pytest.LogCaptureFixture, configured: int | None
) -> None:
    app_id = ApplicationId(configured)
    bot = StubBot()
    bot.application_id = 77
    bot.ready.set()
    app_id.bot = bot
    with caplog.at_level(logging.WARNING):
        await check_application_id(cast(Any, bot), app_id)
    assert not caplog.records
    assert app_id() == 77


async def test_install_unavailable_before_login(client: Any) -> None:
    resp = await client.get("/install?type=guild", allow_redirects=False)
    assert resp.status == 503
    assert resp.headers["Retry-After"] == "30"
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    assert "not ready" in await resp.text()
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value


@pytest.mark.parametrize(
    "headers",
    [
        {"Range": "bytes=0-10"},
        {"If-None-Match": "*"},
        {"If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"},
        {"If-Range": "x", "Range": "bytes=5-"},
    ],
)
async def test_install_503_ignores_range_and_conditional_headers(
    client: Any, headers: dict[str, str]
) -> None:
    resp = await client.get("/install", headers=headers, allow_redirects=False)
    assert resp.status == 503
    assert "ETag" not in resp.headers and "Last-Modified" not in resp.headers
    assert "Content-Range" not in resp.headers and "Accept-Ranges" not in resp.headers
    assert (await resp.read()) == (web_app.STATIC / "install-unavailable.html").read_bytes()


def test_install_url_encodes_scope_as_space() -> None:
    url = web_app.install_url(7, "guild")
    assert url == (
        "https://discord.com/oauth2/authorize"
        "?client_id=7&integration_type=0&scope=applications.commands%20bot&permissions=0"
    )


async def test_landing_page(client: Any) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value
    html = await resp.text()
    assert 'href="/install"' in html and 'href="/install?type=guild"' in html
    assert "discord.com" not in html  # the install URL is built server-side from the application id
    assert html.index('href="/static/style.css"') < html.index('href="/static/landing.css"')
    assert 'href="/privacy"' in html and 'href="/terms"' in html
    assert not re.search(r"<script|<style|\sstyle=|\son[a-z]+=", html)  # CSP: nothing inline


def test_icon_sprite_is_self_contained() -> None:
    svg = re.sub(r"<!--.*?-->", "", (web_app.STATIC / "icons.svg").read_text(encoding="utf-8"), flags=re.S)
    assert "<style" not in svg and "<script" not in svg and "href=" not in svg  # no CSP or external fetches
    assert 'id="i-download"' in svg and 'id="i-github"' in svg


async def test_resolve_ok_matches_contract(client: Any, store: Store) -> None:
    token, _ = store.create_link(1, [item("a.jpg"), item("a.jpg")])
    resp = await client.post("/api/resolve", json={"token": token})
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    body = await resp.json()
    jsonschema.validate(body, SCHEMA)
    assert [i["name"] for i in body["items"]] == ["a.jpg", "a (1).jpg"]


@pytest.mark.parametrize(
    "payload",
    [{"token": "x" * 43}, {"token": "short"}, {"token": 5}, {"nope": 1}, [], {"token": "a b" * 20}],
)
async def test_resolve_generic_404(client: Any, payload: object) -> None:
    resp = await client.post("/api/resolve", json=payload)
    assert resp.status == 404
    assert await resp.json() == {"error": "not_found"}


async def test_resolve_revoked_is_same_404(client: Any, store: Store) -> None:
    token, record = store.create_link(1, [item()])
    store.revoke(record.link_id, 1)
    resp = await client.post("/api/resolve", json={"token": token})
    assert resp.status == 404
    assert await resp.json() == {"error": "not_found"}


async def test_resolve_rejects_non_json(client: Any) -> None:
    assert (await client.post("/api/resolve", data="token=x")).status == 415
    bad = await client.post("/api/resolve", data="{", headers={"Content-Type": "application/json"})
    assert bad.status == 400
    assert (await client.get("/api/resolve")).status == 405


@pytest.mark.parametrize("body", [b"[" * 4000, b'{"token": "\xff"}', b"\xef\xbb\xbf\xff"])
async def test_resolve_unparseable_json_is_400(client: Any, body: bytes) -> None:
    # "[[[[…" makes json.loads raise RecursionError (not ValueError); it must not become a 500.
    resp = await client.post("/api/resolve", data=body, headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_request"}


async def test_resolve_body_size_limit(client: Any) -> None:
    resp = await client.post("/api/resolve", json={"token": "x" * 10_000})
    assert resp.status == 413


async def test_resolve_rate_limited(aiohttp_client: Any, settings: Settings, store: Store) -> None:
    client = await aiohttp_client(create_app(settings.model_copy(update={"resolve_rate_per_min": 3}), store))
    statuses = [(await client.post("/api/resolve", json={"token": "x" * 43})).status for _ in range(4)]
    assert statuses == [404, 404, 404, 429]


def test_rate_limiter_window() -> None:
    now = [0.0]
    rl = RateLimiter(2, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    now[0] = 61
    assert rl.allow("a")


def test_rate_limiter_evicts_least_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "MAX_TRACKED_CLIENTS", 3)
    rl = RateLimiter(1, clock=lambda: 0.0)
    for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        assert rl.allow(ip)
    assert not rl.allow("10.0.0.1")  # refreshes .1 as most recent
    assert rl.allow("10.0.0.4")  # evicts .2
    assert list(rl._hits) == ["10.0.0.3", "10.0.0.1", "10.0.0.4"]


def test_ipv6_limited_per_64() -> None:
    rl = RateLimiter(2, clock=lambda: 0.0)
    assert rl.allow("2001:db8:1:2::1") and rl.allow("2001:db8:1:2:ffff::9")
    assert not rl.allow("2001:db8:1:2:abcd::5")
    assert rl.allow("2001:db8:1:3::1")


@pytest.mark.parametrize(
    ("ip", "key"),
    [
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("203.0.113.9", "203.0.113.9"),
        ("garbage", "garbage"),
    ],
)
def test_limit_key(ip: str, key: str) -> None:
    assert limit_key(ip) == key


async def test_500_keeps_security_headers(client: Any, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: str) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(store, "resolve", boom)
    resp = await client.post("/api/resolve", json={"token": "x" * 43})
    assert resp.status == 500
    assert await resp.json() == {"error": "internal"}
    assert resp.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    assert resp.headers["Cache-Control"] == "no-store"


def _req(remote: str, xff: str | None = None) -> Any:
    headers = {"X-Forwarded-For": xff} if xff else {}
    return cast(Any, SimpleNamespace(remote=remote, headers=headers))


@pytest.mark.parametrize(
    ("remote", "xff", "expected"),
    [
        ("172.30.88.1", "203.0.113.9", "203.0.113.9"),  # compose gateway → trusted
        ("172.30.88.1", "1.1.1.1, 203.0.113.9", "203.0.113.9"),  # right-most hop
        ("172.30.88.1", "garbage", "172.30.88.1"),
        ("8.8.8.8", "203.0.113.9", "8.8.8.8"),  # untrusted peer can't spoof
        ("172.30.88.1", None, "172.30.88.1"),
        ("not-an-ip", "203.0.113.9", "not-an-ip"),
        # dual-stack socket (WEB_HOST=::) reports IPv4 peers as IPv4-mapped IPv6
        ("::ffff:172.30.88.1", "203.0.113.9", "203.0.113.9"),
        ("::ffff:172.30.88.1", "::ffff:203.0.113.9", "203.0.113.9"),
        ("::ffff:8.8.8.8", "203.0.113.9", "8.8.8.8"),
        ("::ffff:8.8.8.8", None, "8.8.8.8"),
    ],
)
def test_client_ip(settings: Settings, remote: str, xff: str | None, expected: str) -> None:
    s = settings.model_copy(
        update={"trusted_proxies": (ip_network("127.0.0.1/32"), ip_network("172.30.88.1/32"))}
    )
    assert client_ip(_req(remote, xff), s) == expected


def test_client_ip_default_trusts_loopback_only(settings: Settings) -> None:
    assert client_ip(_req("127.0.0.1", "203.0.113.9"), settings) == "203.0.113.9"
    assert client_ip(_req("::ffff:127.0.0.1", "203.0.113.9"), settings) == "203.0.113.9"
    assert client_ip(_req("172.18.0.1", "203.0.113.9"), settings) == "172.18.0.1"  # other containers


def test_client_ip_custom_proxies(settings: Settings) -> None:
    s = settings.model_copy(update={"trusted_proxies": (ip_network("10.0.0.0/8"),)})
    assert client_ip(_req("172.18.0.1", "203.0.113.9"), s) == "172.18.0.1"
    assert client_ip(_req("10.1.2.3", "203.0.113.9"), s) == "203.0.113.9"


# --- Google Analytics (cookieless, GA_MEASUREMENT_ID) ----------------------------------

GA_ID = "G-TEST1234"
GA_MARKERS = ("ga-id", "analytics.js", "googletagmanager", "google-analytics", "<!--analytics-->")
GA_PAGES = ["/", "/privacy", "/terms"]


@pytest.fixture
async def ga_client(aiohttp_client: Any, settings: Settings, store: Store) -> Any:
    return await aiohttp_client(create_app(settings.model_copy(update={"ga_measurement_id": GA_ID}), store))


@pytest.mark.parametrize("path", [*GA_PAGES, "/d", "/install"])
async def test_no_analytics_when_unset(client: Any, path: str) -> None:
    resp = await client.get(path, allow_redirects=False)
    html = await resp.text()
    for marker in GA_MARKERS:
        assert marker not in html, marker
    assert resp.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]


@pytest.mark.parametrize("path", GA_PAGES)
async def test_analytics_on_the_three_pages_when_set(ga_client: Any, path: str) -> None:
    resp = await ga_client.get(path)
    assert resp.status == 200
    html = await resp.text()
    head = html[: html.index("</head>")]
    assert f'<meta name="ga-id" content="{GA_ID}">' in head
    assert '<script src="/static/analytics.js" defer></script>' in head
    assert "<!--analytics-->" not in html
    assert not re.search(r"<script(?![^>]*\ssrc=)", html)  # no inline script: the CSP forbids it
    assert "googletagmanager" not in html  # gtag.js is loaded by analytics.js, after the consent default
    csp = resp.headers["Content-Security-Policy"]
    assert csp == web_app.ANALYTICS_CSP
    assert "script-src 'self' https://www.googletagmanager.com;" in csp
    assert "https://*.google-analytics.com https://*.analytics.google.com" in csp
    assert "unsafe" not in csp and "frame-ancestors 'none'" in csp
    for name, value in SECURITY_HEADERS.items():  # everything else unchanged
        if name != "Content-Security-Policy":
            assert resp.headers[name] == value


@pytest.mark.parametrize(
    "path",
    ["/d", "/install", "/healthz", "/static/d.js", "/static/analytics.js", "/does-not-exist", "/privacy/"],
)
async def test_csp_stays_strict_elsewhere_when_set(ga_client: Any, path: str) -> None:
    resp = await ga_client.get(path, allow_redirects=False)
    csp = resp.headers["Content-Security-Policy"]
    assert csp == SECURITY_HEADERS["Content-Security-Policy"]
    assert "google" not in csp


async def test_never_on_the_download_page_or_api(ga_client: Any) -> None:
    html = await (await ga_client.get("/d")).text()
    for marker in GA_MARKERS:
        assert marker not in html, marker
    resp = await ga_client.post("/api/resolve", json={"token": "x" * 43})
    assert "google" not in resp.headers["Content-Security-Policy"]
    resp = await ga_client.get("/install")  # the 503 page while the bot isn't logged in
    assert resp.status == 503
    assert "analytics" not in await resp.text()
    assert "google" not in resp.headers["Content-Security-Policy"]


async def test_privacy_page_discloses_analytics_only_when_on(client: Any, ga_client: Any) -> None:
    off = await (await client.get("/privacy")).text()
    on = await (await ga_client.get("/privacy")).text()
    for html in (off, on):
        assert "if-analytics" not in html and "if-no-analytics" not in html  # markers removed
        assert html.count("<h2>Website analytics</h2>") == 1
    assert "This site uses no analytics, no cookies and no third-party scripts." in off
    assert "Google" not in off and "Apart from the website analytics" not in off
    assert "No data is sold or shared with third parties." in off
    assert "no analytics" not in on
    for text in (
        "Art. 6(1)(f) GDPR",
        "right to object",
        "possibly in the United",
        'href="https://tools.google.com/dlpage/gaoptout"',
        'href="https://policies.google.com/privacy"',
        "Apart from the website analytics above",
    ):
        assert text in on, text


def test_render_analytics_blocks() -> None:
    page = (
        "a<!--analytics-->b\n"
        "<!--if-analytics-->\nON\n<!--/if-analytics-->\n"
        "<!--if-no-analytics-->\nOFF\n<!--/if-no-analytics-->\n"
        "z"
    )
    assert web_app.render_analytics(page, None) == "ab\nOFF\nz"
    assert web_app.render_analytics(page, GA_ID).startswith('a<meta name="ga-id"')
    assert web_app.render_analytics(page, GA_ID).endswith("b\nON\nz")


@pytest.mark.parametrize("path", GA_PAGES)
async def test_rendered_pages_revalidate(client: Any, ga_client: Any, path: str) -> None:
    for c in (client, ga_client):
        resp = await c.get(path)
        assert resp.headers["Cache-Control"] == "no-cache"
        assert "ETag" not in resp.headers and "Last-Modified" not in resp.headers


def test_csps_share_one_template() -> None:
    assert web_app.build_csp() == web_app.CSP
    strict = dict(d.split(" ", 1) for d in web_app.CSP.split("; "))
    wide = dict(d.split(" ", 1) for d in web_app.ANALYTICS_CSP.split("; "))
    assert strict.keys() == wide.keys()
    for name, sources in strict.items():  # the analytics CSP only adds sources, never drops one
        assert set(sources.split()) <= set(wide[name].split()), name
    added = {name: set(wide[name].split()) - set(sources.split()) for name, sources in strict.items()}
    assert added == {
        "default-src": set(),
        "img-src": {"https://*.google-analytics.com", "https://www.googletagmanager.com"},
        "media-src": set(),
        "connect-src": {
            "https://*.google-analytics.com",
            "https://*.analytics.google.com",
            "https://www.googletagmanager.com",
            "https://*.google.com",
        },
        "script-src": {"https://www.googletagmanager.com"},
        "style-src": set(),
        "frame-ancestors": set(),
        "base-uri": set(),
        "form-action": set(),
    }


def test_analytics_script_order_and_page_location() -> None:
    js = (web_app.STATIC / "analytics.js").read_text(encoding="utf-8")
    code = re.sub(r"(?m)(^|\s)//.*$", "", js)  # comments aside (not the https:// URL)
    steps = [
        "window.dataLayer = window.dataLayer || [];",
        "function gtag() { dataLayer.push(arguments); }",
        'gtag("consent", "default", {',
        'gtag("js", new Date());',
        'gtag("config", id,',
        'document.createElement("script")',
        "https://www.googletagmanager.com/gtag/js?id=",
    ]
    positions = [code.index(s) for s in steps]
    assert positions == sorted(positions)  # consent default before config, gtag.js loaded last
    consent = code[code.index('gtag("consent"') : code.index('gtag("js"')]
    for key in ("analytics_storage", "ad_storage", "ad_user_data", "ad_personalization"):
        assert f'{key}: "denied"' in consent, key
    assert "page_location: location.origin + location.pathname" in code
    for leak in ("location.href", "location.hash", "location.search", "cookie"):
        assert leak not in code, leak


async def test_analytics_script_is_served(client: Any) -> None:
    resp = await client.get("/static/analytics.js")
    assert resp.status == 200
    assert "javascript" in resp.headers["Content-Type"]


def test_page_without_placeholder_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(web_app, "STATIC", tmp_path)
    with pytest.raises(RuntimeError, match="placeholder"):
        web_app._analytics_page("index.html", settings)  # pyright: ignore[reportPrivateUsage]


def test_page_with_unknown_placeholder_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    (tmp_path / "index.html").write_text("<!--analytics--><a href='{{REPO}}'></a>", encoding="utf-8")
    monkeypatch.setattr(web_app, "STATIC", tmp_path)
    with pytest.raises(RuntimeError, match=r"unknown placeholder {{REPO}}"):
        web_app._analytics_page("index.html", settings)  # pyright: ignore[reportPrivateUsage]


# --- Branding: icon, favicons, source link ----------------------------------------------

BRAND_PAGES = ["/", "/privacy", "/terms", "/d", "/install"]  # /install: the 503 page before login
FAVICON_LINKS = (
    '<link rel="icon" href="/favicon.ico" sizes="32x32">',
    '<link rel="icon" href="/static/brand/favicon.svg" type="image/svg+xml">',
    '<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">',
)


@pytest.mark.parametrize("path", BRAND_PAGES)
async def test_pages_carry_the_icon_and_favicons(client: Any, path: str) -> None:
    html = await (await client.get(path)).text()
    head = html[: html.index("</head>")]
    for link in FAVICON_LINKS:
        assert link in head, link
    assert '<img class="brand__icon" src="/static/brand/mediazip-icon.svg" alt=""' in html
    assert "{{" not in html


@pytest.mark.parametrize(
    ("path", "ctype"),
    [
        ("/favicon.ico", "image/x-icon"),
        ("/static/brand/favicon.ico", None),
        ("/static/brand/favicon.svg", "image/svg+xml"),
        ("/static/brand/apple-touch-icon.png", "image/png"),
        ("/static/brand/mediazip-icon.svg", "image/svg+xml"),
        *((f"/static/brand/mediazip-icon-{size}.png", "image/png") for size in (128, 256, 512, 1024)),
    ],
)
async def test_brand_assets_served(client: Any, path: str, ctype: str | None) -> None:
    resp = await client.get(path)
    assert resp.status == 200
    if ctype is not None:
        assert resp.headers["Content-Type"].startswith(ctype)
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value
    body = await resp.read()
    if path.endswith(".ico"):
        assert body[:4] == b"\x00\x00\x01\x00"  # ICO header
    elif path.endswith(".png"):
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


def test_brand_svgs_are_self_contained() -> None:
    for name in ("mediazip-icon.svg", "favicon.svg"):
        svg = (web_app.STATIC / "brand" / name).read_text(encoding="utf-8")
        assert "<style" not in svg and "<script" not in svg and "href" not in svg, name
        assert "#C8F031" in svg and "#111214" in svg, name  # the icon keeps its own colours


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
async def test_source_link(client: Any, path: str) -> None:
    html = await (await client.get(path)).text()
    footer = html[html.index("<footer") :]
    assert f'<a href="{web_app.REPO_URL}">Source on GitHub</a>' in footer
    if path == "/":
        main = html[html.index("<main") : html.index("</main>")]
        assert f'<a class="repo-link" href="{web_app.REPO_URL}">' in main
        assert "Open source: read the code on GitHub" in main
        assert 'href="/static/icons.svg#i-github"' in main


async def test_landing_has_no_em_dash(client: Any) -> None:
    html = await (await client.get("/")).text()
    assert "—" not in html and "&mdash;" not in html


async def test_landing_link_preview(client: Any, settings: Settings) -> None:
    html = await (await client.get("/")).text()
    head = html[: html.index("</head>")]
    base = settings.public_base_url
    assert f'<meta property="og:image" content="{base}/static/brand/mediazip-icon-512.png">' in head
    assert f'<meta property="og:url" content="{base}/">' in head
    assert '<meta name="twitter:card" content="summary">' in head
    assert re.search(r'<meta property="og:title" content="[^"]*MediaZip', head)


async def test_landing_link_preview_follows_public_base_url(
    aiohttp_client: Any, settings: Settings, store: Store
) -> None:
    other = settings.model_copy(update={"public_base_url": "https://mediazip.download"})
    html = await (await (await aiohttp_client(create_app(other, store))).get("/")).text()
    assert 'content="https://mediazip.download/static/brand/mediazip-icon-512.png"' in html


def test_render_page_fills_and_escapes() -> None:
    out = web_app.render_page(
        '<a href="{{REPO_URL}}"></a><img src="{{BASE_URL}}/x">', None, 'https://a.example/"'
    )
    assert out == f'<a href="{web_app.REPO_URL}"></a><img src="https://a.example/&quot;/x">'
