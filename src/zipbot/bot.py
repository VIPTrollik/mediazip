"""discord.py client: non-privileged intents only, commands synced globally (plus the
owner-only /stats as a guild command, see zipbot.cogs.stats)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from zipbot.config import Settings
from zipbot.dm_basket import DmBaskets
from zipbot.stats import Stats
from zipbot.store import Store
from zipbot.views import COOLDOWN_TEXT, DYNAMIC_ITEMS, Panels, UserCooldown

log = logging.getLogger(__name__)

EXTENSIONS: tuple[str, ...] = (
    "zipbot.cogs.context_menu",
    "zipbot.cogs.basket",
    "zipbot.cogs.dm",
    "zipbot.cogs.stats",
)
NOT_HERE = "This command isn't available here."

# No voice: skip discord.py's startup warnings about missing voice libraries.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False


def intents() -> discord.Intents:
    # Context-menu targets and DM content are exempt from the Message Content intent (PLAN.md §2).
    i = discord.Intents.none()
    i.guilds = True
    i.dm_messages = True
    return i


SIGNATURE_KEYS = (
    "name",
    "type",
    "description",
    "options",
    "integration_types",
    "contexts",
    "nsfw",
    "min_value",
    "max_value",
)
# Discord documents `integration_types` and `contexts` as global-only, so a guild command is
# compared without them; its default permissions matter instead.
GUILD_SIGNATURE_KEYS = (
    *(k for k in SIGNATURE_KEYS if k not in ("integration_types", "contexts")),
    "default_member_permissions",
)


def command_signature(payload: Any, keys: tuple[str, ...] = SIGNATURE_KEYS) -> dict[str, Any]:
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

    sig = {k: norm(payload.get(k)) for k in keys}
    sig["description"] = sig["description"] or ""
    sig["options"] = sig["options"] or []
    sig["nsfw"] = bool(sig["nsfw"])
    if "default_member_permissions" in sig:  # local: an int; Discord: a string
        perms = sig["default_member_permissions"]
        sig["default_member_permissions"] = None if perms is None else str(perms)
    return sig


def same_commands(local: list[Any], remote: list[Any], keys: tuple[str, ...] = SIGNATURE_KEYS) -> bool:
    def sigs(payloads: list[Any]) -> list[dict[str, Any]]:
        return sorted((command_signature(p, keys) for p in payloads), key=repr)

    return sigs(local) == sigs(remote)


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
        self.stats: Stats = store.stats
        self.panels = Panels()  # each user's one live reply panel
        self.button_cooldown = UserCooldown()
        self.dm_baskets = DmBaskets(self)  # each user's basket message in the bot DM
        store.on_basket_link_gone = self.dm_baskets.link_gone
        self._background: set[asyncio.Task[Any]] = set()

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.add_dynamic_items(*DYNAMIC_ITEMS)  # buttons keep working after a restart
        self.tree.on_error = self.on_app_command_error
        self.dm_baskets.start()
        await self.sync_commands()

    async def close(self) -> None:
        await self.dm_baskets.close()
        await super().close()

    async def sync_commands(self) -> None:
        """Bulk-overwrite global commands only when they changed: syncing on every start gets
        rate limited during restart loops (seen in testing: a 40 s stall)."""
        local = [c.to_dict(self.tree) for c in self.tree.get_commands()]  # global ones only
        remote = await self.http.get_global_commands(self.application_id) if self.application_id else []
        if remote and same_commands(local, list(remote)):
            log.info("global commands up to date (%d)", len(local))
            return
        synced = await self.tree.sync()
        log.info("synced %d global commands", len(synced))

    async def sync_guild_commands(self, guild_id: int) -> bool:
        """Make this app's commands in one guild match the tree's commands for it (zero or
        more), with the same "only when they differ" check as the global ones. This only
        touches the app's own guild commands there, never the global ones. True if it synced."""
        if self.application_id is None:
            return False
        guild = discord.Object(guild_id)
        local = [c.to_dict(self.tree) for c in self.tree.get_commands(guild=guild)]
        try:
            remote = await self.http.get_guild_commands(self.application_id, guild_id)
            if same_commands(local, list(remote), GUILD_SIGNATURE_KEYS):
                log.debug("guild %d commands up to date (%d)", guild_id, len(local))
                return False
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:  # 50001: no access (not in that server any more)
            log.warning("guild %d command sync failed: %s %s", guild_id, type(exc).__name__, exc.code or "")
            return False
        log.info("synced %d commands for guild %d", len(local), guild_id)
        return True

    async def app_owners(self) -> frozenset[int]:
        """The application's owners as set in the Developer Portal: its owner, or its team's
        admins and developers (discord.py's is_owner() rules). Fetched once, then cached by
        discord.py until a restart."""
        if not self.owner_id and not self.owner_ids:
            await self.is_owner(cast(Any, discord.Object(0)))  # fills owner_id / owner_ids
        return frozenset({self.owner_id} if self.owner_id else self.owner_ids or ())

    async def is_app_owner(self, user: discord.abc.User) -> bool:
        """is_owner(), failing closed: if Discord can't be asked, nobody is an owner."""
        try:
            return await self.is_owner(cast(Any, user))
        except Exception as exc:  # any failure means "no"
            log.warning("owner check failed: %s", type(exc).__name__)
            return False

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = COOLDOWN_TEXT.format(error.retry_after)
        elif isinstance(error, app_commands.CommandNotFound):
            # A guild command this process doesn't have, e.g. /stats left behind in a server
            # whose ownership changed while the bot was offline: tidy that server up.
            text = NOT_HERE
            # (load_extension() gives the cog module a fresh module object: no isinstance.)
            refresh = getattr(self.get_cog("StatsCog"), "refresh", None)
            if interaction.guild_id and refresh is not None:
                self.spawn(refresh([interaction.guild_id]))
        else:
            log.error("command failed: %s", type(getattr(error, "original", error)).__name__)
            text = "Something went wrong. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def on_ready(self) -> None:
        log.info("gateway ready, %d guilds", len(self.guilds))
