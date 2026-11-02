"""Trigger B: forward or upload files to the bot's DM (PLAN.md §3).

Messages arriving within DM_DEBOUNCE_SEC form one burst; the bot then replies once with a
link for exactly that burst (one ZIP folder per message). "Add to basket" on the reply puts
the burst into the user's basket if they want to combine it with more.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from zipbot import handlers
from zipbot.media import MediaItem, extract, payload_from_message

if TYPE_CHECKING:
    from zipbot.bot import ZipBot

log = logging.getLogger(__name__)

HELP = (
    "Forward me messages with attachments (or upload files here) and I'll reply with one "
    "download link for all of them. You can also use **Apps → Download attachments** on any message."
)


@dataclass
class Burst:
    channel: discord.abc.Messageable
    items: list[MediaItem] = field(default_factory=list)
    messages: int = 0
    task: asyncio.Task[None] | None = None

    def add(self, items: list[MediaItem]) -> None:
        self.items.extend(replace(item, source_index=self.messages) for item in items)
        self.messages += 1


class DmCog(commands.Cog):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        self.pending: dict[int, Burst] = {}

    async def cog_unload(self) -> None:
        for burst in self.pending.values():
            if burst.task:
                burst.task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Only DMs from people; this also skips the bot's own (ephemeral) replies (Phase 0 finding).
        if message.guild is not None or message.author.bot:
            return
        user_id = message.author.id
        settings = self.bot.settings
        kinds = None if settings.all_media else settings.media_types
        items = extract(payload_from_message(message), kinds=kinds)
        burst = self.pending.get(user_id)
        if not items:
            if burst is None:  # text sent alongside a forward burst is fine
                await message.channel.send(HELP)
            return
        if burst is None:
            burst = self.pending[user_id] = Burst(message.channel)
        burst.add(items)
        if burst.task:
            burst.task.cancel()  # only ever cancelled while still sleeping, see flush()
        burst.task = asyncio.create_task(self.flush(user_id, burst))

    async def flush(self, user_id: int, burst: Burst) -> None:
        async with burst.channel.typing():
            await asyncio.sleep(self.bot.settings.dm_debounce_sec)
        # Detach before any I/O, so a message arriving now starts a new burst instead of
        # cancelling this one halfway through sending.
        if self.pending.get(user_id) is burst:
            del self.pending[user_id]
        content, view = handlers.burst_reply(self.bot, user_id, burst.items, burst.messages)
        try:
            if view is None:
                await burst.channel.send(content)
            else:
                await burst.channel.send(content, view=view)
        except discord.HTTPException as exc:
            log.warning("DM reply failed: %s", type(exc).__name__)


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(DmCog(bot))
