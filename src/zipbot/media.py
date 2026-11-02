"""Extract downloadable media from Discord message payloads (PLAN.md §4).

The core works on raw API dicts (interaction `resolved.messages`, gateway payloads, test
fixtures). `payload_from_message` adapts discord.py `Message` objects to the same shape.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import parse_qs, unquote, urlsplit

import discord

# Only these hosts ever reach a link record / the browser (Phase 0: all send CORS `*`, all signed).
ALLOWED_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "images-ext-1.discordapp.net",
        "images-ext-2.discordapp.net",
    }
)
MEDIA_KINDS = ("image", "video", "audio")

Payload = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MediaItem:
    url: str
    proxy_url: str | None
    filename: str
    size: int | None
    content_type: str | None
    width: int | None
    height: int | None
    duration_secs: float | None
    source_index: int
    cdn_expires_at: int | None

    @property
    def kind(self) -> str | None:
        ctype = self.content_type or mimetypes.guess_type(self.filename)[0] or ""
        kind = ctype.split("/", 1)[0]
        return kind if kind in MEDIA_KINDS else None


def is_allowed_url(url: object) -> TypeGuard[str]:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS and not parts.username


def cdn_expiry(url: str) -> int | None:
    """Unix time from the signed URL's `ex` (hex) parameter, if present."""
    values = parse_qs(urlsplit(url).query).get("ex")
    if not values:
        return None
    try:
        return int(values[0], 16)
    except ValueError:
        return None


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0 else None


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _name_from_url(url: str, fallback: str) -> str:
    last = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return last or fallback


def _from_attachment(a: Payload, source_index: int) -> MediaItem | None:
    url = a.get("url")
    if not is_allowed_url(url):
        return None
    proxy = a.get("proxy_url")
    return MediaItem(
        url=url,
        proxy_url=proxy if is_allowed_url(proxy) else None,
        filename=_str(a.get("filename")) or _name_from_url(url, "file"),
        size=_int(a.get("size")),
        content_type=_str(a.get("content_type")),
        width=_int(a.get("width")),
        height=_int(a.get("height")),
        duration_secs=_num(a.get("duration_secs")),
        source_index=source_index,
        cdn_expires_at=cdn_expiry(url),
    )


def _embed_media(e: Payload) -> Payload | None:
    """The sub-object holding an embed's own media; link-preview thumbnails are not media."""
    kind = e.get("type")
    if kind == "image":
        return e.get("thumbnail") or e.get("image")
    if kind == "gifv":  # Tenor & co: the proxied mp4 is the actual GIF
        return e.get("video") or e.get("thumbnail")
    if kind == "video":
        return e.get("video")  # YouTube etc. have no proxy_url and get dropped by the allowlist
    return e.get("image")  # rich/article: only an explicit image counts


def _from_embed(e: Payload, source_index: int, n: int) -> MediaItem | None:
    media = _embed_media(e)
    if not isinstance(media, Mapping):
        return None
    proxy = media.get("proxy_url")
    if not is_allowed_url(proxy):
        return None
    original = _str(media.get("url")) or _str(e.get("url")) or proxy
    filename = _name_from_url(original, f"embed-{n}")
    ctype = _str(media.get("content_type"))
    if e.get("type") == "gifv" and media is e.get("video"):
        ctype, filename = "video/mp4", f"{filename.rsplit('.', 1)[0]}.mp4"
    return MediaItem(
        url=proxy,
        proxy_url=proxy,
        filename=filename,
        size=None,
        content_type=ctype,
        width=_int(media.get("width")),
        height=_int(media.get("height")),
        duration_secs=None,
        source_index=source_index,
        cdn_expires_at=cdn_expiry(proxy),
    )


def _layers(message: Payload) -> Iterator[Payload]:
    yield message
    for snap in message.get("message_snapshots") or ():
        inner = snap.get("message") if isinstance(snap, Mapping) else None
        if isinstance(inner, Mapping):
            yield inner


def extract(
    message: Payload, *, source_index: int = 0, kinds: Iterable[str] | None = None
) -> list[MediaItem]:
    """Media of one message, in order: attachments, forwarded attachments, embeds.

    `kinds` limits to e.g. {"image", "video"}; None means every file (MEDIA_TYPES=all).
    """
    wanted = None if kinds is None else frozenset(kinds)
    layers = list(_layers(message))
    found: list[MediaItem] = []
    for layer in layers:
        for a in layer.get("attachments") or ():
            if isinstance(a, Mapping) and (item := _from_attachment(a, source_index)):
                found.append(item)
    for layer in layers:
        for e in layer.get("embeds") or ():
            if isinstance(e, Mapping) and (item := _from_embed(e, source_index, len(found) + 1)):
                found.append(item)

    seen: set[str] = set()
    result: list[MediaItem] = []
    for item in found:
        key = urlsplit(item.url).path
        if key in seen:
            continue
        seen.add(key)
        if wanted is None or item.kind in wanted:
            result.append(item)
    return result


def _attachment_payload(a: discord.Attachment) -> dict[str, Any]:
    return {
        "url": a.url,
        "proxy_url": a.proxy_url,
        "filename": a.filename,
        "size": a.size,
        "content_type": a.content_type,
        "width": a.width,
        "height": a.height,
        "duration_secs": a.duration,
    }


def payload_from_message(message: discord.Message) -> dict[str, Any]:
    """discord.py Message → the raw shape `extract` reads (used for DM messages)."""
    return {
        "attachments": [_attachment_payload(a) for a in message.attachments],
        "embeds": [e.to_dict() for e in message.embeds],
        "message_snapshots": [
            {
                "message": {
                    "attachments": [_attachment_payload(a) for a in s.attachments],
                    "embeds": [e.to_dict() for e in s.embeds],
                }
            }
            for s in message.message_snapshots
        ],
    }
