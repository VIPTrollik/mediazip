from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
from discord.components import _component_factory

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


# --- a fake Discord for the DM basket ----------------------------------------------------

BOT_ID = 4242


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


class FakeMessage:
    """A message in a fake DM channel, as the REST API returns it (components parsed back)."""

    def __init__(self, channel: FakeDmChannel, message_id: int, view: Any, author_id: int = BOT_ID) -> None:
        self.channel = channel
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.flags = SimpleNamespace(components_v2=True, ephemeral=False, suppress_notifications=False)
        self.view = view

    @property
    def components(self) -> list[Any]:
        return [_component_factory(cast(Any, d)) for d in self.view.to_components()] if self.view else []

    async def edit(self, *, view: Any) -> FakeMessage:
        return await self.channel.get_partial_message(self.id).edit(view=view)


class FakePartialMessage:
    def __init__(self, channel: FakeDmChannel, message_id: int) -> None:
        self.channel, self.id = channel, message_id

    async def edit(self, *, view: Any) -> FakeMessage:
        ch = self.channel
        await ch.wait()
        if ch.edit_error:
            raise ch.edit_error
        if self.id not in ch.messages:
            raise not_found()
        ch.edits.append((self.id, view))
        ch.messages[self.id].view = view
        return ch.messages[self.id]

    async def delete(self) -> None:
        ch = self.channel
        await ch.wait()
        if ch.delete_error:
            raise ch.delete_error
        if self.id not in ch.messages:
            raise not_found()
        del ch.messages[self.id]
        ch.deletes.append(self.id)


_ids = itertools.count(10**12)


class FakeDmChannel:
    """One user's DM with the bot: send / edit / delete / history, with switchable failures."""

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}  # oldest first
        self.sends: list[tuple[Any, bool]] = []  # (view, silent)
        self.edits: list[tuple[int, Any]] = []
        self.deletes: list[int] = []
        self.history_calls: list[int | None] = []
        self.send_error: BaseException | None = None
        self.edit_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.gate: asyncio.Event | None = None  # if set, every call waits for it

    async def wait(self) -> None:
        if self.gate:
            await self.gate.wait()

    def put(self, view: Any, author_id: int = BOT_ID) -> FakeMessage:
        """A message already in the DM (e.g. from before a restart)."""
        message = FakeMessage(self, next(_ids), view, author_id)
        self.messages[message.id] = message
        return message

    async def send(self, *, view: Any, silent: bool = False) -> FakeMessage:
        await self.wait()
        if self.send_error:
            raise self.send_error
        message = self.put(view)
        message.flags.suppress_notifications = silent
        self.sends.append((view, silent))
        return message

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(self, message_id)

    async def history(self, *, limit: int | None = 100) -> AsyncIterator[FakeMessage]:
        self.history_calls.append(limit)
        for message in list(reversed(self.messages.values()))[:limit]:  # newest first
            yield message


class FakeDiscord:
    """The parts of the client the DM basket uses: the bot user, create_dm, channels by id."""

    def __init__(self) -> None:
        self.user = SimpleNamespace(id=BOT_ID)
        self.channels: dict[int, FakeDmChannel] = {}
        self.dm_error: BaseException | None = None

    def dm_of(self, user_id: int) -> FakeDmChannel:
        return self.get_partial_messageable(500_000 + user_id)

    async def create_dm(self, user: Any) -> FakeDmChannel:
        if self.dm_error:
            raise self.dm_error
        return self.dm_of(user.id)

    def get_partial_messageable(self, channel_id: int, *, type: Any = None) -> FakeDmChannel:
        return self.channels.setdefault(channel_id, FakeDmChannel(channel_id))
