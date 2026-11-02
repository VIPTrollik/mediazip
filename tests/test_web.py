from __future__ import annotations

import json
import re
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import jsonschema
import pytest

from helpers import EX, item
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web import app as web_app
from zipbot.web.app import SECURITY_HEADERS, RateLimiter, client_ip, create_app, limit_key

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


@pytest.mark.parametrize(
    ("path", "ctype"), [("/static/icons.svg", "image/svg+xml"), ("/static/style.css", "text/css")]
)
async def test_theme_assets_served_with_security_headers(client: Any, path: str, ctype: str) -> None:
    resp = await client.get(path)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith(ctype)  # nosniff: a wrong type would break <use>/CSS
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers[name] == value


def test_icon_sprite_is_self_contained() -> None:
    svg = re.sub(r"<!--.*?-->", "", (web_app.STATIC / "icons.svg").read_text(encoding="utf-8"), flags=re.S)
    assert "<style" not in svg and "<script" not in svg and "href=" not in svg  # no CSP or external fetches
    assert 'id="i-download"' in svg


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
