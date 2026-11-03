"""aiohttp web app: landing + install link, download page, resolve API, health check (PLAN.md §5, §6)."""

from __future__ import annotations

import html
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

from aiohttp import web

from zipbot.config import Settings
from zipbot.contract import resolve_body
from zipbot.store import Store

STATIC = Path(__file__).parent / "static"
CDN_HOSTS = (
    "https://cdn.discordapp.com",
    "https://media.discordapp.net",
    "https://images-ext-1.discordapp.net",
    "https://images-ext-2.discordapp.net",
)
_cdn = " ".join(CDN_HOSTS)
CSP = (
    f"default-src 'self'; img-src 'self' blob: {_cdn}; media-src 'self' blob: {_cdn}; "
    f"connect-src 'self' {_cdn}; script-src 'self'; style-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# Google Analytics (GA_MEASUREMENT_ID) widens the CSP for "/", "/privacy" and "/terms" only, never for
# /d (the link token is in its URL fragment) or the API. Hosts from Google's CSP guide, "Google
# Analytics" without Ads features: https://developers.google.com/tag-platform/security/guides/csp
# (checked 2026-11-03; it lists script-src-elem www.googletagmanager.com, img-src
# www.googletagmanager.com *.google-analytics.com, connect-src www.googletagmanager.com
# *.google-analytics.com *.google.com). We add *.analytics.google.com (GA4's regional collection
# endpoints) and leave out *.google.com: with consent denied and no Google Signals or Ads
# features nothing is sent there, and a blocked request only costs a CSP report.
GA_SCRIPT = "https://www.googletagmanager.com"
GA_CONNECT = "https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com"
GA_IMG = "https://*.google-analytics.com https://www.googletagmanager.com"
ANALYTICS_CSP = (
    f"default-src 'self'; img-src 'self' blob: {_cdn} {GA_IMG}; media-src 'self' blob: {_cdn}; "
    f"connect-src 'self' {_cdn} {GA_CONNECT}; script-src 'self' {GA_SCRIPT}; style-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
ANALYTICS_PLACEHOLDER = "<!--analytics-->"
ANALYTICS_PAGES = {"/": "index.html", "/privacy": "privacy.html", "/terms": "terms.html"}
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,100}")
NOT_FOUND = {"error": "not_found"}
MAX_TRACKED_CLIENTS = 10_000

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
# integration_type 1 = user install (the default), 0 = guild install. The guild install adds the bot
# user only so members can DM it; interaction replies need no permissions (PLAN.md §Discord setup).
INSTALL_PARAMS = {
    "user": {"integration_type": "1", "scope": "applications.commands"},
    "guild": {"integration_type": "0", "scope": "applications.commands bot", "permissions": "0"},
}
INSTALL_RETRY_SEC = 30

log = logging.getLogger(__name__)

SETTINGS_KEY = web.AppKey("settings", Settings)
STORE_KEY = web.AppKey("store", Store)
# Returns the application id for /install, or None while it is not known yet.
APPLICATION_ID_KEY = web.AppKey[Callable[[], int | None]]("application_id")
UNAVAILABLE_KEY = web.AppKey("install_unavailable", bytes)  # the /install 503 page
# Set on a response that carries the Google Analytics tags: it gets ANALYTICS_CSP.
ANALYTICS_KEY = web.ResponseKey("analytics", bool)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def parse_ip(value: str) -> IPv4Address | IPv6Address | None:
    """IPv4-mapped IPv6 (`::ffff:a.b.c.d`, what a dual-stack `WEB_HOST=::` socket reports) → IPv4."""
    try:
        addr = ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def limit_key(ip: str) -> str:
    """IPv6 clients usually own a whole /64, so limit per /64 rather than per address."""
    addr = parse_ip(ip)
    if addr is None:
        return ip
    if isinstance(addr, IPv6Address):
        return str(ip_network(f"{addr}/64", strict=False))
    return str(addr)


class RateLimiter:
    """Sliding one-minute window per client; least-recently-seen clients are evicted
    once MAX_TRACKED_CLIENTS is reached, so memory and per-request work stay bounded."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, ip: str) -> bool:
        now = self.clock()
        key = limit_key(ip)
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
            while len(self._hits) > MAX_TRACKED_CLIENTS:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True


LIMITER_KEY = web.AppKey("limiter", RateLimiter)


def client_ip(request: web.Request, settings: Settings) -> str:
    peer = request.remote or ""
    addr = parse_ip(peer)
    if addr is None:
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and any(addr in net for net in settings.trusted_proxies):
        # nginx overwrites the header with $remote_addr; take the right-most hop regardless.
        candidate = parse_ip(forwarded.rsplit(",", 1)[-1].strip())
        if candidate is not None:
            return str(candidate)
    return str(addr)


@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    except Exception as exc:  # keep headers on 500s too; log the type only, never request data
        log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.path)
        response = web.json_response({"error": "internal"}, status=500)
    response.headers.update(SECURITY_HEADERS)
    if not isinstance(response, web.HTTPException) and response.get(ANALYTICS_KEY):
        response.headers["Content-Security-Policy"] = ANALYTICS_CSP
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so a deploy takes effect at once
    if isinstance(response, web.HTTPException):
        raise response
    return response


async def resolve(request: web.Request) -> web.Response:
    if not request.app[LIMITER_KEY].allow(client_ip(request, request.app[SETTINGS_KEY])):
        return web.json_response({"error": "rate_limited"}, status=429, headers={"Retry-After": "60"})
    if request.content_type != "application/json":
        return web.json_response({"error": "bad_request"}, status=415)
    try:
        payload = await request.json()
    except (ValueError, RecursionError):  # malformed JSON or bad encoding, or nesting too deep to parse
        return web.json_response({"error": "bad_request"}, status=400)
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return web.json_response(NOT_FOUND, status=404)
    record = request.app[STORE_KEY].resolve(token)
    if record is None:
        return web.json_response(NOT_FOUND, status=404)
    return web.json_response(resolve_body(record))


def _page(name: str) -> Handler:
    async def handler(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / name, headers={"Content-Type": "text/html; charset=utf-8"})

    return handler


def analytics_tags(measurement_id: str | None) -> str:
    """What replaces a page's `<!--analytics-->`: nothing, or the id and the loader. No inline
    script (the CSP forbids it): /static/analytics.js reads the id from the meta tag."""
    if measurement_id is None:
        return ""
    return (
        f'<meta name="ga-id" content="{html.escape(measurement_id)}">\n'
        '<script src="/static/analytics.js" defer></script>'
    )


def _analytics_page(name: str, measurement_id: str | None) -> Handler:
    """A page that may carry Google Analytics, rendered once at startup."""
    source = (STATIC / name).read_text(encoding="utf-8")
    if ANALYTICS_PLACEHOLDER not in source:
        raise RuntimeError(f"{name} has no {ANALYTICS_PLACEHOLDER} placeholder")
    body = source.replace(ANALYTICS_PLACEHOLDER, analytics_tags(measurement_id)).encode()

    async def handler(_: web.Request) -> web.Response:
        response = web.Response(body=body, content_type="text/html", charset="utf-8")
        response[ANALYTICS_KEY] = measurement_id is not None
        return response

    return handler


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def install_url(application_id: int, kind: str) -> str:
    return (
        f"{AUTHORIZE_URL}?{urlencode({'client_id': application_id, **INSTALL_PARAMS[kind]}, quote_via=quote)}"
    )


class HasApplicationId(Protocol):
    @property
    def application_id(self) -> int | None: ...


class ApplicationId:
    """The install link's application id: the logged-in bot's own, else DISCORD_APPLICATION_ID."""

    def __init__(self, configured: int | None) -> None:
        self.configured = configured
        self.bot: HasApplicationId | None = None  # set by __main__.run() once the client exists

    def __call__(self) -> int | None:
        actual = self.bot.application_id if self.bot is not None else None
        return actual if actual is not None else self.configured

    def check(self) -> None:
        """Run after login: a stale or mistyped DISCORD_APPLICATION_ID should not pass silently."""
        actual = self.bot.application_id if self.bot is not None else None
        if actual is not None and self.configured is not None and actual != self.configured:
            log.warning(
                "DISCORD_APPLICATION_ID %d is not this bot's application (%d); the install link uses %d",
                self.configured,
                actual,
                actual,
            )


async def install(request: web.Request) -> web.Response:
    """`/install` (user install) or `/install?type=guild` → Discord's OAuth2 install screen."""
    kind = "guild" if request.query.get("type") == "guild" else "user"
    app_id = request.app[APPLICATION_ID_KEY]()
    if app_id is None:  # bot not logged in yet (just started, or Discord unreachable)
        # A plain response, not FileResponse: no Range/ETag/304 handling on an error page.
        return web.Response(
            body=request.app[UNAVAILABLE_KEY],
            status=503,
            content_type="text/html",
            charset="utf-8",
            headers={"Retry-After": str(INSTALL_RETRY_SEC)},
        )
    raise web.HTTPFound(install_url(app_id, kind))


def create_app(
    settings: Settings, store: Store, application_id: Callable[[], int | None] | None = None
) -> web.Application:
    app = web.Application(middlewares=[security_headers], client_max_size=4096)
    app[SETTINGS_KEY] = settings
    app[STORE_KEY] = store
    app[LIMITER_KEY] = RateLimiter(settings.resolve_rate_per_min)
    app[APPLICATION_ID_KEY] = application_id or ApplicationId(settings.discord_application_id)
    app[UNAVAILABLE_KEY] = (STATIC / "install-unavailable.html").read_bytes()
    app.router.add_get("/healthz", healthz)
    for path, name in ANALYTICS_PAGES.items():  # never /d: its URL fragment holds the link token
        app.router.add_get(path, _analytics_page(name, settings.ga_measurement_id))
    app.router.add_get("/install", install)
    app.router.add_get("/d", _page("d.html"))
    app.router.add_post("/api/resolve", resolve)
    app.router.add_static("/static/", STATIC, show_index=False, follow_symlinks=False, append_version=False)
    return app
