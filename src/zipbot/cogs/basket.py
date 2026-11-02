"""/basket show | link | clear."""

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
    commands.GroupCog, group_name="basket", group_description="Collect attachments from several messages"
):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="show", description="Show what's in your basket")
    @per_user_cooldown
    async def show(self, interaction: discord.Interaction) -> None:
        await views.show(interaction, handlers.basket_show_reply(self.bot, interaction.user.id))

    @app_commands.command(name="link", description="Get a download link for everything in your basket")
    @per_user_cooldown
    async def link(self, interaction: discord.Interaction) -> None:
        # Same panel as `show`: it always carries a fresh basket link.
        await views.show(interaction, handlers.basket_show_reply(self.bot, interaction.user.id))

    @app_commands.command(name="clear", description="Empty your basket")
    @per_user_cooldown
    async def clear(self, interaction: discord.Interaction) -> None:
        await views.show(interaction, handlers.basket_clear_reply(self.bot, interaction.user.id))


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(BasketCog(bot))
