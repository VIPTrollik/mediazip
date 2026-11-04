"""/stats: owner check, where it is registered, guild sync, the reply (zipbot.cogs.stats)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from helpers import fixture, http_error
from zipbot import stats
from zipbot.bot import EXTENSIONS, NOT_HERE, ZipBot
from zipbot.cogs import stats as stats_cog
from zipbot.cogs.stats import REFUSAL, StatsCog, stats_command
from zipbot.config import Settings
from zipbot.store import Store

OWNER, MEMBER, READ_ONLY, STRANGER = 101, 102, 103, 104
OWNED, OTHER, TEAM_OWNED = 1, 2, 3
GLOBAL_NAMES = {"Download media", "Add to ZIP", "zip"}


def app_info(*, team: bool = False, guilds: int = 3, users: int | None = 5) -> Any:
    members = [
        SimpleNamespace(id=OWNER, role=discord.TeamMemberRole.admin),
        SimpleNamespace(id=MEMBER, role=discord.TeamMemberRole.developer),
        SimpleNamespace(id=READ_ONLY, role=discord.TeamMemberRole.read_only),
    ]
    return SimpleNamespace(
        owner=SimpleNamespace(id=OWNER),
        team=SimpleNamespace(members=members) if team else None,
        approximate_guild_count=guilds,
        approximate_user_install_count=users,
    )


def guild(guild_id: int, owner_id: int) -> Any:
    return SimpleNamespace(id=guild_id, owner_id=owner_id)


@pytest.fixture
async def bot(settings: Settings) -> AsyncIterator[Any]:
    b = ZipBot(settings, Store.from_settings(settings))
    b._connection.application_id = 1  # normally set at login
    for ext in EXTENSIONS:
        await b.load_extension(ext)
    b._connection._guilds = {g.id: g for g in (guild(OWNED, OWNER), guild(OTHER, STRANGER))}
    b.application_info = AsyncMock(return_value=app_info())
    b.http.get_guild_commands = AsyncMock(return_value=[])
    b.http.get_global_commands = AsyncMock(return_value=fixture("global_commands.json"))
    b.tree.sync = AsyncMock(return_value=[])
    yield b
    await b.close()


def cog(bot: Any) -> StatsCog:
    c = bot.get_cog("StatsCog")
    assert type(c).__name__ == "StatsCog"  # loaded as an extension: its own module object
    return cast(StatsCog, c)


def interaction(bot: Any, user_id: int, guild_id: int | None = OWNED) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        client=bot,
        guild_id=guild_id,
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
    )


def texts(view: discord.ui.LayoutView) -> list[str]:
    def walk(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in components:
            out.append(c)
            out.extend(walk(c.get("components", [])))
        return out

    return [c["content"] for c in walk(view.to_components()) if c["type"] == 10]


def components(view: discord.ui.LayoutView) -> int:
    def count(cs: list[dict[str, Any]]) -> int:
        return sum(1 + count(c.get("components", [])) for c in cs)

    return count(view.to_components())


async def run(bot: Any, user_id: int, days: int = 7) -> Any:
    inter = interaction(bot, user_id)
    await cast(Any, stats_command.callback)(inter, days)
    send = inter.response.send_message
    send.assert_awaited_once()
    assert send.call_args.kwargs["ephemeral"] is True
    return send.call_args


def stats_names(bot: Any, guild_id: int) -> list[str]:
    return [c.name for c in bot.tree.get_commands(guild=discord.Object(guild_id))]


def synced_guilds(bot: Any) -> list[int]:
    return [c.kwargs["guild"].id for c in bot.tree.sync.call_args_list]


def as_discord_returns(command: Any, bot: Any) -> dict[str, Any]:
    """The command as Discord's GET returns it: permissions as a string, no contexts."""
    payload = command.to_dict(bot.tree)
    payload.update(id="9", application_id="1", version="1", default_member_permissions="8")
    payload.pop("contexts")
    payload.pop("integration_types")
    return payload


# --- who may use it -----------------------------------------------------------------


async def test_owner_gets_the_stats(bot: Any) -> None:
    bot.stats.record("download_media", user_id=5)
    call = await run(bot, OWNER)
    view = call.kwargs["view"]
    assert isinstance(view, discord.ui.LayoutView)
    body = "\n".join(texts(view))
    assert "Download media" in body and "Discord installs (approximate): 3 servers, 5 users" in body
    assert "user-days" in body
    assert bot.stats.snapshot()[bot.stats.today()] == {"download_media": 1, "users": 1}  # /stats not counted


async def test_team_member_allowed_read_only_member_refused(bot: Any) -> None:
    bot.application_info = AsyncMock(return_value=app_info(team=True))
    assert isinstance((await run(bot, MEMBER)).kwargs["view"], discord.ui.LayoutView)
    assert isinstance((await run(bot, OWNER)).kwargs["view"], discord.ui.LayoutView)
    assert (await run(bot, READ_ONLY)).args == (REFUSAL,)


@pytest.mark.parametrize("team", [False, True])
async def test_non_owner_is_refused_without_numbers(bot: Any, team: bool) -> None:
    bot.application_info = AsyncMock(return_value=app_info(team=team))
    bot.stats.record("download_media", 42, user_id=5)
    call = await run(bot, STRANGER)
    assert call.args == (REFUSAL,) and "view" not in call.kwargs
    assert not re.search(r"\d", REFUSAL)
    bot.application_info.assert_awaited_once()  # the owner lookup only: no install counts
    assert bot.stats.snapshot()[bot.stats.today()] == {"download_media": 42, "users": 1}


async def test_owner_lookup_is_cached(bot: Any) -> None:
    await run(bot, OWNER)  # owner lookup + install counts
    await run(bot, STRANGER)
    await run(bot, OWNER)
    assert bot.application_info.await_count == 3


async def test_owner_check_fails_closed(bot: Any, caplog: pytest.LogCaptureFixture) -> None:
    bot.application_info = AsyncMock(side_effect=http_error(discord.HTTPException, 500, 0))
    assert (await run(bot, OWNER)).args == (REFUSAL,)
    assert "owner check failed: HTTPException" in caplog.text


def test_command_payload(bot: Any) -> None:
    payload = stats_command.to_dict(bot.tree)
    assert payload["default_member_permissions"] == discord.Permissions(administrator=True).value
    assert payload["contexts"] == [0] and payload["integration_types"] == [0]
    assert payload["dm_permission"] is False
    (option,) = payload["options"]
    assert (option["name"], option["min_value"], option["max_value"]) == ("days", 1, 90)
    assert option["required"] is False
    assert stats_command.checks  # the shared per-user cooldown


# --- where it exists ----------------------------------------------------------------


async def test_only_in_servers_the_owners_own(bot: Any) -> None:
    await cog(bot).refresh()
    assert stats_names(bot, OWNED) == ["stats"]
    assert stats_names(bot, OTHER) == []
    assert synced_guilds(bot) == [OWNED]  # Discord had nothing there yet
    bot.http.get_guild_commands.assert_awaited_once_with(1, OWNED)  # no call for other servers


async def test_team_owned_servers(bot: Any) -> None:
    bot.application_info = AsyncMock(return_value=app_info(team=True))
    bot._connection._guilds[TEAM_OWNED] = guild(TEAM_OWNED, MEMBER)
    bot._connection._guilds[4] = guild(4, READ_ONLY)
    await cog(bot).refresh()
    assert cog(bot).guild_ids == {OWNED, TEAM_OWNED}


async def test_never_a_global_command(bot: Any) -> None:
    await cog(bot).on_ready()
    assert {c.name for c in bot.tree.get_commands()} == GLOBAL_NAMES
    await bot.sync_commands()  # the global list still matches Discord's: no global sync
    assert all(c.kwargs.get("guild") for c in bot.tree.sync.call_args_list)
    bot.http.get_global_commands = AsyncMock(return_value=[])
    await bot.sync_commands()
    assert bot.tree.sync.call_args_list[-1].kwargs == {}  # a global sync, without /stats
    assert "stats" not in {c.name for c in bot.tree.get_commands()}


async def test_override_replaces_detection(bot: Any) -> None:
    bot.settings = bot.settings.model_copy(update={"stats_guild_ids": frozenset({OTHER, 99})})
    await cog(bot).refresh()
    assert cog(bot).guild_ids == {OTHER, 99}
    assert stats_names(bot, OWNED) == []
    assert synced_guilds(bot) == [OTHER, 99]
    bot.application_info.assert_not_awaited()  # no owner lookup needed
    await cog(bot).on_guild_remove(cast(Any, guild(OTHER, STRANGER)))
    assert cog(bot).guild_ids == {OTHER, 99}  # the override is the list


async def test_unknown_owners_leave_everything_alone(bot: Any, caplog: pytest.LogCaptureFixture) -> None:
    bot.application_info = AsyncMock(side_effect=TimeoutError())
    await cog(bot).refresh()
    assert cog(bot).guild_ids == set()
    bot.http.get_guild_commands.assert_not_awaited()
    assert "can't tell where /stats belongs: TimeoutError" in caplog.text


# --- guild sync ---------------------------------------------------------------------


async def test_sync_skipped_when_discord_already_has_it(bot: Any) -> None:
    bot.http.get_guild_commands = AsyncMock(return_value=[as_discord_returns(stats_command, bot)])
    await cog(bot).refresh()
    await cog(bot).refresh()  # a second ready (reconnect)
    assert stats_names(bot, OWNED) == ["stats"]
    bot.tree.sync.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"description": "old text"},
        {"default_member_permissions": None},
        {"options": []},
    ],
)
async def test_sync_when_discord_differs(bot: Any, change: dict[str, Any]) -> None:
    remote = as_discord_returns(stats_command, bot) | change
    bot.http.get_guild_commands = AsyncMock(return_value=[remote])
    await cog(bot).refresh()
    assert synced_guilds(bot) == [OWNED]


async def test_option_range_change_is_noticed(bot: Any) -> None:
    remote = as_discord_returns(stats_command, bot)
    remote["options"] = [dict(remote["options"][0], max_value=30)]
    bot.http.get_guild_commands = AsyncMock(return_value=[remote])
    await cog(bot).refresh()
    assert synced_guilds(bot) == [OWNED]


async def test_ownership_moves(bot: Any) -> None:
    c = cog(bot)
    await c.on_ready()
    bot.http.get_guild_commands = AsyncMock(return_value=[as_discord_returns(stats_command, bot)])
    bot.tree.sync.reset_mock()
    # The owners' server changes hands: /stats is taken out (bulk overwrite with nothing).
    bot._connection._guilds[OWNED] = after = guild(OWNED, STRANGER)
    await c.on_guild_update(cast(Any, guild(OWNED, OWNER)), cast(Any, after))
    assert stats_names(bot, OWNED) == [] and c.guild_ids == set()
    assert synced_guilds(bot) == [OWNED]
    # ...and another one comes to them.
    bot.http.get_guild_commands = AsyncMock(return_value=[])
    bot._connection._guilds[OTHER] = after = guild(OTHER, OWNER)
    await c.on_guild_update(cast(Any, guild(OTHER, STRANGER)), cast(Any, after))
    assert c.guild_ids == {OTHER}
    # A name change is not an ownership change: nothing to do.
    calls = bot.http.get_guild_commands.await_count
    await c.on_guild_update(cast(Any, after), cast(Any, after))
    assert bot.http.get_guild_commands.await_count == calls


async def test_joining_servers(bot: Any) -> None:
    c = cog(bot)
    bot._connection._guilds[5] = new = guild(5, OWNER)
    await c.on_guild_join(cast(Any, new))
    assert c.guild_ids == {5} and synced_guilds(bot) == [5]
    # Joining someone else's server: one check, and a /stats left from an earlier stay goes.
    bot.http.get_guild_commands = AsyncMock(return_value=[as_discord_returns(stats_command, bot)])
    bot._connection._guilds[6] = new = guild(6, STRANGER)
    await c.on_guild_join(cast(Any, new))
    assert c.guild_ids == {5} and synced_guilds(bot) == [5, 6]


async def test_leaving_a_server_makes_no_api_call(bot: Any) -> None:
    c = cog(bot)
    await c.refresh()
    bot.http.get_guild_commands.reset_mock()
    await c.on_guild_remove(cast(Any, guild(OWNED, OWNER)))
    assert stats_names(bot, OWNED) == [] and c.guild_ids == set()
    bot.http.get_guild_commands.assert_not_awaited()


async def test_sync_failure_is_logged(bot: Any, caplog: pytest.LogCaptureFixture) -> None:
    bot.http.get_guild_commands = AsyncMock(side_effect=http_error(discord.Forbidden, 403, 50001))
    assert await bot.sync_guild_commands(OWNED) is False
    assert "guild 1 command sync failed: Forbidden 50001" in caplog.text
    bot._connection.application_id = None
    assert await bot.sync_guild_commands(OWNED) is False


async def test_sync_reports_success(bot: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    await cog(bot).refresh()
    assert "synced 1 commands for guild 1" in caplog.text
    assert await bot.sync_guild_commands(OTHER) is False  # nothing either side


async def test_leftover_command_is_refused_and_tidied(bot: Any) -> None:
    """/stats used in a server whose ownership changed while the bot was offline."""
    bot.http.get_guild_commands = AsyncMock(return_value=[as_discord_returns(stats_command, bot)])
    inter = interaction(bot, STRANGER, guild_id=OTHER)
    await bot.on_app_command_error(inter, app_commands.CommandNotFound("stats", []))
    assert inter.response.send_message.call_args.args == (NOT_HERE,)
    for _ in range(5):
        await asyncio.sleep(0)
    assert synced_guilds(bot) == [OTHER] and cog(bot).guild_ids == set()
    dm = interaction(bot, STRANGER, guild_id=None)
    await bot.on_app_command_error(dm, app_commands.CommandNotFound("stats", []))
    assert dm.response.send_message.call_args.args == (NOT_HERE,)


async def test_unload_removes_guild_commands(bot: Any) -> None:
    await cog(bot).refresh()
    await bot.unload_extension("zipbot.cogs.stats")
    assert stats_names(bot, OWNED) == []


# --- the reply ----------------------------------------------------------------------

TODAY = date(2026, 11, 4)


def busy_days(n: int, value: int) -> dict[str, dict[str, int]]:
    names = {k for k, _ in stats.COLUMNS}
    return {(TODAY - timedelta(days=i)).isoformat(): dict.fromkeys(names, value) for i in range(n)}


def reply_length(summary: list[str], daily: list[str]) -> int:
    return sum(map(len, summary + daily))


def test_reply_with_no_data() -> None:
    summary, daily = stats.reply_texts({}, TODAY, {"guilds": 0, "user_installs": None}, 7)
    table = summary[1]
    assert table.startswith("```\n") and table.endswith("\n```")
    assert "today  7 days  30 days  all time" in table
    assert re.search(r"^429s\s+0\s+0\s+0\s+0$", table, re.M)
    assert summary[2] == "Discord installs (approximate): 0 servers, ? users"
    assert daily[0] == "**Last 7 days**" and daily[1].count("\n") == 9  # fences, header, 7 days
    assert daily[-1] == stats.REPLY_FOOTER


def test_summary_adds_up_windows() -> None:
    days = busy_days(40, 1)
    table = stats.summary_table(days, TODAY)
    assert re.search(r"^users\s+1\s+7\s+30\s+40$", table, re.M)
    assert re.search(r"^/zip\s+3\s+21\s+90\s+120$", table, re.M)  # show + link + clear
    daily = stats.daily_table(days, TODAY, 2)
    assert daily.splitlines()[1].split() == ["11-04", "1", "1", "1", "3", "1", "1", "1", "1"]


@pytest.mark.parametrize("show_days", [1, 7, 30, 90, 500])
def test_reply_stays_within_discord_limits(show_days: int) -> None:
    days = busy_days(stats.KEEP_DAYS, 9_999_999)
    installs = {"guilds": 10**9, "user_installs": 10**9}
    summary, daily = stats.reply_texts(days, TODAY, installs, show_days)
    assert reply_length(summary, daily) <= stats.REPLY_TEXT_MAX < 4000
    shown = daily[1].count("\n") - 2
    assert 1 <= shown <= min(show_days, stats.REPLY_MAX_DAYS)
    if shown < min(show_days, stats.REPLY_MAX_DAYS):
        assert "more would not fit" in daily[0]
    if show_days == 1:
        assert daily[0] == "**Today**"


async def test_rendered_view_within_discord_limits(bot: Any) -> None:
    bot.stats.days = {d: stats.Counter(c) for d, c in busy_days(stats.KEEP_DAYS, 123_456_789).items()}
    for show_days in (1, 90):
        view = stats_cog.stats_view(bot.stats, {"error": "TimeoutError"}, show_days)
        assert sum(map(len, texts(view))) <= 4000
        assert components(view) <= 40
        assert "install counts unavailable (TimeoutError)" in "\n".join(texts(view))


# --- install counts -------------------------------------------------------------------


async def test_install_counts(bot: Any) -> None:
    assert await stats_cog.install_counts(bot) == {"guilds": 3, "user_installs": 5}
    bot.application_info = AsyncMock(return_value=app_info(users=None))
    counts = await stats_cog.install_counts(bot)
    assert stats.install_line(counts) == "Discord installs (approximate): 3 servers, ? users"


async def test_install_counts_failures(bot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot.application_info = AsyncMock(side_effect=http_error(discord.HTTPException, 500, 0))
    assert await stats_cog.install_counts(bot) == {"error": "HTTPException"}

    async def slow() -> Any:
        await asyncio.sleep(1)

    monkeypatch.setattr(stats_cog, "APP_INFO_TIMEOUT_SEC", 0.01)
    bot.application_info = slow
    assert await stats_cog.install_counts(bot) == {"error": "TimeoutError"}


async def test_owner_reply_when_install_counts_fail(bot: Any) -> None:
    await bot.app_owners()  # cached from an earlier call
    bot.application_info = AsyncMock(side_effect=OSError("reset"))
    view = (await run(bot, OWNER, days=3)).kwargs["view"]
    body = "\n".join(texts(view))
    assert "install counts unavailable (OSError)" in body and "**Last 3 days**" in body
