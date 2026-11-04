"""/stats: the usage counters (zipbot.stats) for the application's owners, in Discord.

Who: every invocation checks Bot.is_owner(), i.e. the owner set in the Developer Portal, or
the team's admins and developers if a team owns the app. Anyone else gets REFUSAL and nothing
more. /stats itself is never counted.

Where: never a global command (every user install would list it). It is a guild command in
the servers the bot is in whose owner is one of the app's owners, or in STATS_GUILD_IDS if
that is set. It is visible to administrators only by default (Server Settings → Integrations
can change that). The list is re-checked on ready, on joining a server and when a server's
owner changes; each server is synced only if Discord's copy differs (ZipBot.sync_guild_commands).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from zipbot import stats, views
from zipbot.cogs.context_menu import per_user_cooldown

if TYPE_CHECKING:
    from zipbot.bot import ZipBot

log = logging.getLogger(__name__)

NAME = "stats"
REFUSAL = "This command is only for the app's owners."
APP_INFO_TIMEOUT_SEC = 2  # an interaction must be answered within 3 s


async def install_counts(bot: ZipBot) -> dict[str, Any]:
    """Discord's approximate install counts from the application info, or {"error": type}."""
    try:
        info = await asyncio.wait_for(bot.application_info(), APP_INFO_TIMEOUT_SEC)
    except Exception as exc:  # any failure: report its type only
        return {"error": type(exc).__name__}
    return {"guilds": info.approximate_guild_count, "user_installs": info.approximate_user_install_count}


def stats_view(counters: stats.Stats, installs: dict[str, Any], show_days: int) -> discord.ui.LayoutView:
    summary, daily = stats.reply_texts(
        counters.snapshot(), date.fromisoformat(counters.today()), installs, show_days
    )
    return views.layout(*summary, discord.ui.Separator(), *daily)


@app_commands.command(name=NAME, description="Usage statistics (app owners only)")
@app_commands.describe(days=f"Days to list one by one (1 to {stats.REPLY_MAX_DAYS}, default 7)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.allowed_installs(guilds=True, users=False)
@per_user_cooldown
async def stats_command(
    interaction: discord.Interaction, days: app_commands.Range[int, 1, stats.REPLY_MAX_DAYS] = 7
) -> None:
    bot = cast("ZipBot", interaction.client)
    if not await bot.is_app_owner(interaction.user):
        await interaction.response.send_message(REFUSAL, ephemeral=True)
        return
    view = stats_view(bot.stats, await install_counts(bot), days)
    await interaction.response.send_message(view=view, ephemeral=True)


class StatsCog(commands.Cog):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        self.guild_ids: set[int] = set()  # where the tree has /stats right now
        self.lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        for guild_id in self.guild_ids:
            self.bot.tree.remove_command(NAME, guild=discord.Object(guild_id))
        self.guild_ids.clear()

    async def wanted(self) -> set[int]:
        """The servers that should have /stats: STATS_GUILD_IDS, else the owners' own servers."""
        override = self.bot.settings.stats_guild_ids
        if override is not None:
            return set(override)
        owners = await self.bot.app_owners()
        return {g.id for g in self.bot.guilds if g.owner_id in owners}

    async def refresh(self, guild_ids: Iterable[int] | None = None) -> None:
        """Add /stats to (or remove it from) the given servers as `wanted()` says, and sync each
        one whose commands on Discord differ. None: every server that has or should have it."""
        async with self.lock:
            try:
                wanted = await self.wanted()
            except Exception as exc:  # try again on the next event
                log.warning("can't tell where /stats belongs: %s", type(exc).__name__)
                return
            targets = wanted | self.guild_ids if guild_ids is None else set(guild_ids)
            for guild_id in sorted(targets):
                guild = discord.Object(guild_id)
                if guild_id in wanted:
                    self.bot.tree.add_command(stats_command, guild=guild, override=True)
                    self.guild_ids.add(guild_id)
                else:
                    self.bot.tree.remove_command(NAME, guild=guild)
                    self.guild_ids.discard(guild_id)
                await self.bot.sync_guild_commands(guild_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.refresh()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.refresh([guild.id])  # also clears a /stats left there from an earlier stay

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.owner_id != after.owner_id:
            await self.refresh([after.id])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # The bot has no access to that server's commands any more, so only forget it here.
        # Its /stats goes with the app's integration if that was removed; if the app stays
        # there (applications.commands without the bot), a later use is refused and tidied up
        # (ZipBot.on_app_command_error).
        if self.bot.settings.stats_guild_ids is None and guild.id in self.guild_ids:
            self.bot.tree.remove_command(NAME, guild=guild)
            self.guild_ids.discard(guild.id)


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(StatsCog(bot))
