"""`POST /api/resolve` response body (PLAN.md §5). Frozen at G2; the JSON Schema in
tests/contract/resolve.schema.json is the source of truth for both server and page."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from zipbot.media import MediaItem
from zipbot.store import LinkRecord

THUMB_BOX = 320


def _join_query(url: str, params: str) -> str:
    if not urlsplit(url).query:
        return f"{url}?{params}"
    return f"{url}{'' if url.endswith('&') else '&'}{params}"


def thumbnail(item: MediaItem) -> str | None:
    """Small preview via Discord's media proxy (verified: resize params keep CORS `*`,
    `format=webp` turns a video into its first frame)."""
    if not item.proxy_url or item.kind not in {"image", "video"}:
        return None
    params = []
    if item.kind == "video" or urlsplit(item.proxy_url).hostname == "media.discordapp.net":
        params.append("format=webp")
    if item.width and item.height:
        scale = min(1.0, THUMB_BOX / max(item.width, item.height))
        params.append(
            f"width={max(1, round(item.width * scale))}&height={max(1, round(item.height * scale))}"
        )
    return _join_query(item.proxy_url, "&".join(params)) if params else item.proxy_url


def resolve_body(record: LinkRecord) -> dict[str, Any]:
    return {
        "expires_at": int(record.expires_at),
        "items": [
            {
                "url": item.url,
                "thumb": thumbnail(item),
                "name": name,
                "kind": item.kind,
                "mime": item.content_type,
                "size": item.size,
                "width": item.width,
                "height": item.height,
                "duration": item.duration_secs,
            }
            for item, name in zip(record.items, record.names, strict=True)
        ],
    }
