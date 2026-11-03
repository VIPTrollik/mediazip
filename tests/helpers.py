from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord

from zipbot.media import MediaItem

FIXTURES = Path(__file__).parent / "fixtures"
EX = 0x6AEA244B  # `ex` of the captured fixture URLs


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def resolved_message(interaction: dict[str, Any]) -> dict[str, Any]:
    (message,) = interaction["data"]["resolved"]["messages"].values()
    return message


def item(name: str = "a.jpg", *, source: int = 0, ctype: str | None = "image/jpeg", **kw: Any) -> MediaItem:
    values: dict[str, Any] = {
        "url": f"https://cdn.discordapp.com/attachments/1/2/{name}?ex=6aea244b&is=6ae8d2cb&hm=00&",
        "proxy_url": f"https://media.discordapp.net/attachments/1/2/{name}?ex=6aea244b&is=6ae8d2cb&hm=00&",
        "filename": name,
        "size": 100,
        "content_type": ctype,
        "width": 800,
        "height": 600,
        "duration_secs": None,
        "source_index": source,
        "cdn_expires_at": EX,
    }
    values.update(kw)
    return MediaItem(**values)


def http_error(cls: type[discord.HTTPException], status: int, code: int) -> discord.HTTPException:
    """An API error as discord.py raises it (status and JSON code)."""
    response = cast(Any, SimpleNamespace(status=status, reason="x"))
    return cls(response, {"code": code, "message": "x"})


def forbidden(code: int = 50278) -> discord.HTTPException:
    """403: 50278 the bot can't DM this user (no install, no shared server), 50007 blocked."""
    return http_error(discord.Forbidden, 403, code)


def not_found(code: int = 10008) -> discord.HTTPException:
    """404 / 10008 Unknown Message."""
    return http_error(discord.NotFound, 404, code)
