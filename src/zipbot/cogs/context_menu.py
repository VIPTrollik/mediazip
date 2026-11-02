"""Message context menus: "Download attachments" (A) and "Add to ZIP basket" (A2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from zipbot import handlers, views

if TYPE_CHECKING:
    from zipbot.bot import ZipBot

# Usable everywhere: servers with or without the bot (user install), DMs, group DMs.
INSTALLS = app_commands.AppInstallationType(guild=True, user=True)
CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
# Per-user rate limit shared by all commands (see ZipBot.on_app_command_error for the reply).
per_user_cooldown = app_commands.checks.cooldown(10, 60, key=lambda i: i.user.id)


class ContextMenus(commands.Cog):
    def __init__(self, bot: ZipBot) -> None:
        self.bot = bot
        self.menus = [
            app_commands.ContextMenu(
                name="Download attachments",
                callback=self.download,
                allowed_installs=INSTALLS,
                allowed_contexts=CONTEXTS,
            ),
            app_commands.ContextMenu(
                name="Add to ZIP basket",
                callback=self.add,
                allowed_installs=INSTALLS,
                allowed_contexts=CONTEXTS,
            ),
        ]
        for menu in self.menus:
            bot.tree.add_command(menu)

    async def cog_unload(self) -> None:
        for menu in self.menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    @per_user_cooldown
    async def download(self, interaction: discord.Interaction, message: discord.Message) -> None:
        payload = handlers.interaction_payload(interaction, message)
        reply = handlers.download_reply(self.bot, interaction.user.id, payload, handlers.message_key(message))
        await views.show(interaction, reply)

    @per_user_cooldown
    async def add(self, interaction: discord.Interaction, message: discord.Message) -> None:
        payload = handlers.interaction_payload(interaction, message)
        reply = handlers.add_to_basket_reply(
            self.bot, interaction.user.id, payload, handlers.message_key(message)
        )
        await views.show(interaction, reply)


async def setup(bot: ZipBot) -> None:
    await bot.add_cog(ContextMenus(bot))
