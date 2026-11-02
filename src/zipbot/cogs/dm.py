"""Trigger B: forward or upload files to the bot's DM (PLAN.md §3).

Messages arriving within DM_DEBOUNCE_SEC form one burst; the bot then replies once with a
link panel for exactly that burst (one ZIP folder per message). "Add to basket" on the panel
puts the burst into the user's basket if they want to combine it with more.

A burst is bounded (security review v0 #1): it holds at most MAX_FILES_PER_LINK items and is
sent as soon as it is full, or at most 3x DM_DEBOUNCE_SEC (capped at BURST_MAX_SEC) after its
first message, however often the user keeps sending.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
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
BURST_MAX_SEC = 30  # a burst's whole lifetime is at most min(3 * debounce, this)
HELP_COOLDOWN_SEC = 5 * 60  # text-only DMs get the help text at most this often
MAX_HELP_USERS = 10_000


@dataclass
class Burst:
    channel: discord.abc.Messageable
    started: float
    items: list[MediaItem] = field(default_factory=list)
    messages: int = 0
    offered: int = 0  # items seen, including those beyond the cap
    task: asyncio.Task[None] | None = None

    def add(self, items: list[MediaItem], cap: int) -> None:
        room = max(0, cap - len(self.items))
        self.items.extend(replace(item, source_index=self.messages) for item in items[:room])
        self.offered += len(items)
        self.messages += 1


class DmCog(commands.Cog):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        self.pending: dict[int, Burst] = {}
        self.clock = time.monotonic
        self.sending: set[asyncio.Task[None]] = set()  # full bursts, already detached from pending
        self.helped: OrderedDict[int, float] = OrderedDict()  # user → last help reply, oldest first

    async def cog_unload(self) -> None:
        for task in [*(b.task for b in self.pending.values() if b.task), *self.sending]:
            task.cancel()

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
            if burst is None and self.may_help(user_id):  # text sent alongside a burst is fine
                await message.channel.send(HELP)
            return
        now = self.clock()
        if burst is None:
            burst = self.pending[user_id] = Burst(message.channel, started=now)
        burst.add(items, settings.max_files_per_link)
        if burst.task:
            burst.task.cancel()  # only ever cancelled while still sleeping, see flush()
        if len(burst.items) >= settings.max_files_per_link:
            del self.pending[user_id]  # full: send now; the next message starts a new burst
            burst.task = asyncio.create_task(self.flush(user_id, burst, 0))
            self.sending.add(burst.task)
            burst.task.add_done_callback(self.sending.discard)
            return
        debounce = settings.dm_debounce_sec
        lifetime = max(debounce, min(3 * debounce, BURST_MAX_SEC))
        delay = max(0.0, min(debounce, burst.started + lifetime - now))
        burst.task = asyncio.create_task(self.flush(user_id, burst, delay))

    def may_help(self, user_id: int) -> bool:
        now = self.clock()
        while self.helped and next(iter(self.helped.values())) + HELP_COOLDOWN_SEC <= now:
            self.helped.popitem(last=False)
        if user_id in self.helped:
            return False
        self.helped[user_id] = now
        if len(self.helped) > MAX_HELP_USERS:
            self.helped.popitem(last=False)
        return True

    async def flush(self, user_id: int, burst: Burst, delay: float) -> None:
        async with burst.channel.typing():
            await asyncio.sleep(delay)
        # Detach before any I/O, so a message arriving now starts a new burst instead of
        # cancelling this one halfway through sending.
        if self.pending.get(user_id) is burst:
            del self.pending[user_id]
        reply = handlers.burst_reply(self.bot, user_id, burst.items, burst.messages, burst.offered)
        try:
            if isinstance(reply, str):
                await burst.channel.send(reply)
            else:
                await burst.channel.send(view=reply)
        except discord.HTTPException as exc:
            log.warning("DM reply failed: %s", type(exc).__name__)


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(DmCog(bot))
