"""discord.py client: non-privileged intents only, commands synced globally."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from zipbot.config import Settings
from zipbot.store import Store
from zipbot.views import COOLDOWN_TEXT, DYNAMIC_ITEMS, ButtonCooldown, Panels

log = logging.getLogger(__name__)

EXTENSIONS: tuple[str, ...] = ("zipbot.cogs.context_menu", "zipbot.cogs.basket", "zipbot.cogs.dm")

# No voice: skip discord.py's startup warnings about missing voice libraries.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False


def intents() -> discord.Intents:
    # Context-menu targets and DM content are exempt from the Message Content intent (PLAN.md §2).
    i = discord.Intents.none()
    i.guilds = True
    i.dm_messages = True
    return i


SIGNATURE_KEYS = ("name", "type", "description", "options", "integration_types", "contexts", "nsfw")


def command_signature(payload: Any) -> dict[str, Any]:
    """The parts of a command payload that matter for "does Discord have this already?"."""

    def norm(value: Any) -> Any:
        if isinstance(value, dict):  # an option: compare the keys that define it, with defaults
            out = {k: norm(v) for k, v in sorted(value.items()) if k in SIGNATURE_KEYS}
            out["required"] = bool(value.get("required"))
            out["options"] = norm(value.get("options") or [])  # Discord omits empty lists
            return out
        if isinstance(value, list):
            return sorted((norm(v) for v in value), key=repr)
        return value

    sig = {k: norm(payload.get(k)) for k in SIGNATURE_KEYS}
    sig["description"] = sig["description"] or ""
    sig["options"] = sig["options"] or []
    sig["nsfw"] = bool(sig["nsfw"])
    return sig


class ZipBot(commands.Bot):
    def __init__(self, settings: Settings, store: Store) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.store = store
        self.panels = Panels()  # each user's one live reply panel
        self.button_cooldown = ButtonCooldown()

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.add_dynamic_items(*DYNAMIC_ITEMS)  # buttons keep working after a restart
        self.tree.on_error = self.on_app_command_error
        await self.sync_commands()

    async def sync_commands(self) -> None:
        """Bulk-overwrite global commands only when they changed: syncing on every start gets
        rate limited during restart loops (seen in testing: a 40 s stall)."""
        local = [command_signature(c.to_dict(self.tree)) for c in self.tree.get_commands()]
        remote = await self.http.get_global_commands(self.application_id) if self.application_id else []
        if remote and sorted(local, key=repr) == sorted((command_signature(c) for c in remote), key=repr):
            log.info("global commands up to date (%d)", len(local))
            return
        synced = await self.tree.sync()
        log.info("synced %d global commands", len(synced))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = COOLDOWN_TEXT.format(error.retry_after)
        else:
            log.error("command failed: %s", type(getattr(error, "original", error)).__name__)
            text = "Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def on_ready(self) -> None:
        log.info("gateway ready, %d guilds", len(self.guilds))
