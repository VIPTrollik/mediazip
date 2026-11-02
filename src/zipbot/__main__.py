"""Entry point: config → logging → web server + bot in one event loop, graceful SIGTERM."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import discord
from aiohttp import web
from pydantic import ValidationError

from zipbot.bot import ZipBot
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.web.app import ApplicationId, create_app

log = logging.getLogger("zipbot")


SWEEP_INTERVAL_SEC = 60


async def sweeper(store: Store) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SEC)
        if removed := store.sweep():
            log.debug("swept %d expired records", removed)


async def check_application_id(bot: discord.Client, app_id: ApplicationId) -> None:
    """Once the bot is logged in, warn if DISCORD_APPLICATION_ID names another application."""
    await bot.wait_until_ready()
    app_id.check()


async def run(settings: Settings) -> None:
    store = Store.from_settings(settings)
    app_id = ApplicationId(settings.discord_application_id)  # the bot's own id once it has logged in
    app = create_app(settings, store, application_id=app_id)
    runner = web.AppRunner(app, access_log=None)  # access log would only add noise
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    log.info("web listening on %s:%d", settings.web_host, settings.web_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs: Ctrl+C still works
            loop.add_signal_handler(sig, stop.set)

    sweep_task = asyncio.create_task(sweeper(store))
    try:
        async with ZipBot(settings, store) as bot:
            app_id.bot = bot
            bot_task = asyncio.create_task(bot.start(settings.discord_token.get_secret_value()))
            check_task = asyncio.create_task(check_application_id(bot, app_id))
            stop_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            stop_task.cancel()
            check_task.cancel()
            if bot_task in done:
                bot_task.result()  # surface login/gateway errors
            log.info("shutting down")
    finally:
        sweep_task.cancel()
        await runner.cleanup()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # At DEBUG, discord.py logs HTTP bodies (reply buttons carry link tokens) and raw events
    # (CDN URLs, message content). Never let library loggers go below INFO.
    for name in ("discord", "aiohttp"):
        logging.getLogger(name).setLevel(max(logging.INFO, logging.getLogger().level))


def main() -> None:
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue] - fields come from the environment
    except ValidationError as exc:
        # Show which settings are wrong without echoing their values (one may be the token).
        problems = ", ".join(f"{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in exc.errors())
        sys.exit(f"invalid configuration: {problems}")
    configure_logging(settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
