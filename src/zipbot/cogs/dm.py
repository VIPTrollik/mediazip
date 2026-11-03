"""Trigger B: forward or upload media to the bot's DM (PLAN.md §3).

Messages arriving within DM_DEBOUNCE_SEC form one burst; the bot then replies once with a
link panel for exactly that burst (one ZIP folder per message). "Add to ZIP" on the panel
puts the burst into the user's basket ("your ZIP") if they want to combine it with more.

A burst is bounded (security review v0 #1): it holds at most MAX_FILES_PER_LINK items and is
sent as soon as it is full, or at most 3x DM_DEBOUNCE_SEC (capped at BURST_MAX_SEC) after its
first message, however often the user keeps sending. Each user starts at most
BURSTS_PER_MIN bursts a minute; beyond that one short notice is sent, not one per message.
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
from zipbot.views import UserCooldown

if TYPE_CHECKING:
    from zipbot.bot import ZipBot

log = logging.getLogger(__name__)

HELP = (
    "Forward me messages with images or videos (or upload them here) and I'll reply with one "
    "download link for all of them. You can also use **Apps → Download media** on any message, "
    "or **Apps → Add to ZIP** on several to collect them into one ZIP."
)
BURST_MAX_SEC = 30  # a burst's whole lifetime is at most min(3 * debounce, this)
BUSY = "You're sending a lot at once — wait a minute, then forward the rest."
BURSTS_PER_MIN = 5
HELP_COOLDOWN_SEC = 5 * 60  # text-only DMs get the help text at most this often
MAX_NOTICE_USERS = 10_000


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
        self.burst_cooldown = UserCooldown(BURSTS_PER_MIN, 60)
        self.helped: OrderedDict[int, float] = OrderedDict()  # user → no help text until then
        self.warned: OrderedDict[int, float] = OrderedDict()  # user → no BUSY notice until then

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
            if burst is None and self.once(self.helped, user_id, HELP_COOLDOWN_SEC):
                await message.channel.send(HELP)  # text sent alongside a burst is fine
            return
        now = self.clock()
        if burst is None:
            if retry := self.burst_cooldown.retry_after(user_id):
                if self.once(self.warned, user_id, retry):
                    await message.channel.send(BUSY)
                return
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

    def once(self, seen: OrderedDict[int, float], user_id: int, window: float) -> bool:
        """True at most once per `window` per user. `seen` (user → until) is bounded."""
        now = self.clock()
        while seen and next(iter(seen.values())) <= now:
            seen.popitem(last=False)
        if seen.get(user_id, now) > now:
            return False
        seen.pop(user_id, None)
        seen[user_id] = now + window
        if len(seen) > MAX_NOTICE_USERS:
            seen.popitem(last=False)
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
            # 50007: the user blocked the app; 50278: the bot can't DM them (no install, no server).
            log.warning("DM reply failed: %s %s", type(exc).__name__, exc.code or "")
            if isinstance(exc, discord.Forbidden):
                self.bot.dm_baskets.mark_unavailable(user_id, exc)


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(DmCog(bot))
