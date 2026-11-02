"""Dev helper: serve the web app (no bot) with a link to a real Discord message's media.

Fetches the message over REST with the bot token from .env, so the CDN URLs are fresh and real.
Usage: python scripts/dev_link.py <channel_id> <message_id> [<channel_id> <message_id> ...]
Prints /d URLs (page, .zip, .files); Ctrl+C to stop. For the phone: adb reverse tcp:8080 tcp:8080.
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

from aiohttp import web

from zipbot.config import Settings
from zipbot.media import extract
from zipbot.store import Store
from zipbot.web.app import create_app


def fetch_message(settings: Settings, channel_id: str, message_id: str) -> dict:
    req = urllib.request.Request(  # noqa: S310 - fixed https Discord API URL
        f"https://discord.com/api/v10/channels/{int(channel_id)}/messages/{int(message_id)}",
        headers={
            "Authorization": f"Bot {settings.discord_token.get_secret_value()}",
            "User-Agent": "DiscordBot (zipbot dev, 0.1)",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.load(resp)


async def main(pairs: list[tuple[str, str]]) -> None:
    settings = Settings()  # pyright: ignore[reportCallIssue]
    store = Store.from_settings(settings)
    kinds = None if settings.all_media else settings.media_types
    items = [
        item
        for index, (ch, msg) in enumerate(pairs)
        for item in extract(fetch_message(settings, ch, msg), source_index=index, kinds=kinds)
    ]
    token, record = store.create_link(0, items)
    runner = web.AppRunner(create_app(settings, store))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", settings.web_port).start()
    base = f"{settings.public_base_url}/d#{token}"
    print(f"{len(record.items)} files; page: {base}\n  zip:   {base}.zip\n  files: {base}.files", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or len(args) % 2:
        sys.exit(__doc__)
    asyncio.run(main(list(zip(args[::2], args[1::2], strict=True))))
