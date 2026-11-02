"""Phase 0: capture raw Discord payloads for test fixtures.

Registers a message context-menu command "Dump payload" (guild + user install, all
contexts) and records every INTERACTION_CREATE and DM MESSAGE_CREATE as raw JSON into
tests/fixtures/raw/ (git-ignored: scrub before copying into tests/fixtures/).

Uses only the non-privileged intents the real bot will use (guilds, dm_messages), so a
captured DM also proves message content arrives without the Message Content intent.

Usage: DISCORD_TOKEN=... [DEV_GUILD_ID=...] python tests/spike/capture_payloads.py
       (or put both into .env at the repo root)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "fixtures" / "raw"


def load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


class CaptureClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.dm_messages = True
        super().__init__(intents=intents, enable_debug_events=True)
        self.tree = app_commands.CommandTree(self)
        self.count = 0

    async def setup_hook(self) -> None:
        dump = app_commands.ContextMenu(
            name="Dump payload",
            callback=self.dump,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=True, private_channel=True
            ),
        )
        self.tree.add_command(dump)
        guild_id = os.environ.get("DEV_GUILD_ID")
        if guild_id:
            guild = discord.Object(int(guild_id))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden:
                print(f"guild sync skipped: app not installed in DEV_GUILD_ID {guild_id}", flush=True)
        await self.tree.sync()
        OUT.mkdir(parents=True, exist_ok=True)

    async def dump(self, interaction: discord.Interaction, message: discord.Message) -> None:
        text = (
            f"Captured: {len(message.attachments)} attachments, {len(message.embeds)} embeds, "
            f"{len(message.message_snapshots)} snapshots."
        )
        # Link button to the spike page with this message's fresh CDN URLs in the fragment, to
        # test Discord's in-app browser. Buttons cap URLs at 512 chars, so add URLs while they fit.
        attachments = [*message.attachments, *(a for s in message.message_snapshots for a in s.attachments)]
        page = os.environ.get("SPIKE_PAGE", "http://localhost:8080/spike.html")
        picked: list[str] = []
        for a in attachments:
            if len(f"{page}#{quote(' '.join([*picked, a.url]), safe='')}") > 512:
                break
            picked.append(a.url)
        view = discord.ui.View()
        if picked:
            url = f"{page}#{quote(' '.join(picked), safe='')}"
            view.add_item(discord.ui.Button(label=f"Open spike ({len(picked)} files)", url=url))
        await interaction.response.send_message(text, view=view, ephemeral=True)

    async def on_socket_raw_receive(self, raw: str) -> None:
        payload = json.loads(raw)
        kind = payload.get("t")
        data = payload.get("d") or {}
        if kind == "INTERACTION_CREATE" or (kind == "MESSAGE_CREATE" and "guild_id" not in data):
            self.count += 1
            path = OUT / f"{int(time.time())}-{self.count:02d}-{kind.lower()}.json"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"captured {path.name}", flush=True)

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"ready as {self.user} (id {self.user.id}); writing to {OUT}", flush=True)


def main() -> None:
    load_dotenv()
    CaptureClient().run(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
