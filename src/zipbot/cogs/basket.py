"""/zip show | link | clear: the basket, called "your ZIP" in the app."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from zipbot import handlers, views
from zipbot.cogs.context_menu import per_user_cooldown

if TYPE_CHECKING:
    from zipbot.bot import ZipBot


@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
class BasketCog(
    commands.GroupCog,
    group_name="zip",
    group_description="Collect images and videos from several messages into one ZIP",
):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="show", description="Show what's in your ZIP")
    @per_user_cooldown
    async def show(self, interaction: discord.Interaction) -> None:
        self.bot.stats.record("zip_show", user_id=interaction.user.id)
        await views.show(interaction, self.reply(interaction))

    @app_commands.command(name="link", description="Get a download link for your ZIP")
    @per_user_cooldown
    async def link(self, interaction: discord.Interaction) -> None:
        self.bot.stats.record("zip_link", user_id=interaction.user.id)
        # Same as `show`: the panel always carries a fresh basket link (or jumps to the DM one).
        await views.show(interaction, self.reply(interaction))

    def reply(self, interaction: discord.Interaction) -> views.Reply:
        in_bot_dm = bool(getattr(interaction, "context", None) and interaction.context.dm_channel)
        return handlers.basket_show_reply(self.bot, interaction.user.id, in_bot_dm=in_bot_dm)

    @app_commands.command(name="clear", description="Empty your ZIP")
    @per_user_cooldown
    async def clear(self, interaction: discord.Interaction) -> None:
        self.bot.stats.record("zip_clear", user_id=interaction.user.id)
        await views.show(interaction, handlers.basket_clear_reply(self.bot, interaction.user.id))


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(BasketCog(bot))
