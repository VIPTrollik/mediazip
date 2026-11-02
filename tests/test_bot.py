from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from helpers import EX, fixture, resolved_message
from zipbot import handlers
from zipbot.bot import EXTENSIONS, ZipBot, intents
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.views import (
    AddLinkToBasketButton,
    BasketClearButton,
    BasketLinkButton,
    RevokeButton,
    format_size,
)

ATTACHMENTS = "interaction_context_menu_attachments.json"
TOKEN_IN_URL = re.compile(r"/d#([A-Za-z0-9_-]{43})(\.files|\.zip)?$")


@pytest.fixture
def state(settings: Settings) -> Any:
    store = Store.from_settings(settings)
    store.clock = lambda: EX - 24 * 3600
    return SimpleNamespace(store=store, settings=settings)


def interaction(state: Any, data: dict[str, Any] | None = None, user_id: int = 7) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        client=state,
        data=data,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
    )


def buttons(view: discord.ui.View | None) -> list[Any]:
    assert view is not None
    return [getattr(c, "item", c) for c in view.children]  # unwrap DynamicItems


def media_payload() -> dict[str, Any]:
    return resolved_message(fixture(ATTACHMENTS))


# --- registration --------------------------------------------------------------------


def test_intents_are_not_privileged() -> None:
    i = intents()
    assert i.guilds and i.dm_messages
    assert not (i.message_content or i.members or i.presences)


async def test_commands_registered_for_every_context(settings: Settings) -> None:
    bot = ZipBot(settings, Store.from_settings(settings))
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    payloads = {c.name: c.to_dict(bot.tree) for c in bot.tree.get_commands()}
    assert set(payloads) == {"Download attachments", "Add to ZIP basket", "basket"}
    for p in payloads.values():
        assert sorted(p["integration_types"]) == [0, 1]
        assert sorted(p["contexts"]) == [0, 1, 2]
    assert payloads["Download attachments"]["type"] == 3
    assert {o["name"] for o in payloads["basket"]["options"]} == {"show", "link", "clear"}
    for ext in EXTENSIONS:
        await bot.unload_extension(ext)
    assert bot.tree.get_commands() == []
    await bot.close()


# --- handlers ------------------------------------------------------------------------


def test_download_reply(state: Any) -> None:
    content, view = handlers.download_reply(state, 7, media_payload(), "c/m")
    assert content.startswith("📦 **5 files** · 51 KB · link expires <t:")
    link_buttons = [b for b in buttons(view) if b.url]
    assert [b.label for b in link_buttons] == ["Save to phone", "Download ZIP", "Open page"]
    matches = [TOKEN_IN_URL.search(b.url) for b in link_buttons]
    assert all(matches) and len({m.group(1) for m in matches if m}) == 1
    assert [m.group(2) for m in matches if m] == [".files", ".zip", None]
    assert all(b.url.startswith("https://dl.example.com/d#") and len(b.url) <= 512 for b in link_buttons)
    token = matches[0].group(1) if matches[0] else ""
    record = state.store.resolve(token)
    assert record and record.message_key == "c/m"
    ids = [b.custom_id for b in buttons(view) if not b.url]
    assert ids == [f"zb:basket:{record.link_id}", f"zb:revoke:{record.link_id}"]


def test_download_reply_no_attachments(state: Any) -> None:
    payload = resolved_message(fixture("interaction_context_menu_no_media.json"))
    assert handlers.download_reply(state, 7, payload, "k") == (handlers.NO_ATTACHMENTS, None)


def test_download_reply_respects_media_types(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"media_types": frozenset({"video"})})
    content, _ = handlers.download_reply(state, 7, media_payload(), "k")
    assert content.startswith("📦 **1 file** · ")


def test_download_reply_takes_any_file_by_default(state: Any) -> None:
    content, _ = handlers.download_reply(state, 7, fixture("message_any_files.json"), "k")
    assert content.startswith("📦 **6 files** · ")


def test_download_reply_media_only_opt_out(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"media_types": frozenset({"image", "video"})})
    content, _ = handlers.download_reply(state, 7, fixture("message_any_files.json"), "k")
    assert content.startswith("📦 **1 file** · ")  # just photo.png
    no_media = {"attachments": fixture("message_any_files.json")["attachments"][:5]}
    assert handlers.download_reply(state, 7, no_media, "k") == (handlers.NO_ATTACHMENTS, None)


def test_download_reply_truncates(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"max_files_per_link": 2})
    state.store.max_files_per_link = 2
    content, _ = handlers.download_reply(state, 7, media_payload(), "k")
    assert "**2 files**" in content
    assert "Only the first 2 of 5 files" in content


def test_download_reply_link_error(state: Any) -> None:
    state.store.clock = lambda: EX  # CDN URLs about to expire
    content, view = handlers.download_reply(state, 7, media_payload(), "k")
    assert content.startswith("Couldn't create a link")
    assert view is None


def test_basket_handlers(state: Any) -> None:
    assert handlers.basket_show_reply(state, 7) == ("🧺 Your basket is empty.", None)
    content, view = handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    assert "Added" in content and "1 message · 5 files" in content
    assert [b.custom_id for b in buttons(view)] == ["zb:basketlink", "zb:basketclear"]
    content, _ = handlers.add_to_basket_reply(state, 7, fixture("message_image_embed.json"), "c/2")
    assert "2 messages · 6 files" in content
    content, _ = handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    assert "already in your basket" in content

    content, view = handlers.basket_link(state, 7)
    assert "📦 **6 files**" in content
    token_url = next(b.url for b in buttons(view) if b.url)
    m = TOKEN_IN_URL.search(token_url)
    assert m
    record = state.store.resolve(m.group(1))
    assert record and record.names[0] == "01/test-1.jpg" and record.names[-1].startswith("02/")
    assert not any(b.custom_id and b.custom_id.startswith("zb:basket:") for b in buttons(view))

    assert handlers.basket_clear_reply(state, 7) == ("🧺 Basket cleared.", None)
    assert handlers.basket_clear_reply(state, 7)[0] == "🧺 Your basket is already empty."
    assert "basket is empty" in handlers.basket_link(state, 7)[0]


def test_add_no_attachments_to_basket(state: Any) -> None:
    payload = resolved_message(fixture("interaction_context_menu_no_media.json"))
    content, view = handlers.add_to_basket_reply(state, 7, payload, "k")
    assert content == "That message has no attachments to download.\n🧺 Your basket is empty."
    assert view is None


def test_basket_link_error(state: Any) -> None:
    state.store.clock = lambda: EX - 300  # basket fresh, but CDN URLs expire within the safety margin
    handlers.add_to_basket_reply(state, 7, media_payload(), "k")
    content, view = handlers.basket_link(state, 7)
    assert content.startswith("Couldn't create a link")
    assert view is not None


def test_interaction_payload_prefers_raw() -> None:
    raw = media_payload()
    msg = cast(
        discord.Message, SimpleNamespace(id=int(raw["id"]), attachments=[], embeds=[], message_snapshots=[])
    )
    inter = cast(discord.Interaction, SimpleNamespace(data={"resolved": {"messages": {raw["id"]: raw}}}))
    assert handlers.interaction_payload(inter, msg) is raw
    empty = cast(discord.Interaction, SimpleNamespace(data=None))
    assert handlers.interaction_payload(empty, msg) == {
        "attachments": [],
        "embeds": [],
        "message_snapshots": [],
    }


@pytest.mark.parametrize(
    ("n", "text"), [(10, "1 KB"), (61_000, "60 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.00 GB")]
)
def test_format_size(n: int, text: str) -> None:
    assert format_size(n) == text


# --- buttons -------------------------------------------------------------------------


async def test_revoke_button(state: Any) -> None:
    token, record = state.store.create_link(7, handlers.extract(media_payload()))
    stranger = interaction(state, user_id=8)
    await RevokeButton(record.link_id).callback(stranger)
    assert "already expired" in stranger.response.edit_message.call_args.kwargs["content"]
    assert state.store.resolve(token)
    owner = interaction(state)
    await RevokeButton(record.link_id).callback(owner)
    assert owner.response.edit_message.call_args.kwargs == {
        "content": "🗑️ Link revoked — it no longer works.",
        "view": None,
    }
    assert state.store.resolve(token) is None


async def test_add_link_to_basket_button(state: Any) -> None:
    _, record = state.store.create_link(7, handlers.extract(media_payload()), message_key="c/m")
    inter = interaction(state)
    await AddLinkToBasketButton(record.link_id).callback(inter)
    args = inter.response.send_message.call_args
    assert "Added" in args.args[0] and args.kwargs["ephemeral"] is True
    basket = state.store.basket(7)
    assert basket and basket.entries[0].message_key == "c/m"
    other = interaction(state, user_id=8)
    await AddLinkToBasketButton(record.link_id).callback(other)
    assert "expired" in other.response.send_message.call_args.args[0]


async def test_basket_buttons(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "k")
    inter = interaction(state)
    await BasketLinkButton().callback(inter)
    assert "📦 **5 files**" in inter.response.edit_message.call_args.kwargs["content"]
    await BasketClearButton().callback(inter)
    assert inter.response.edit_message.call_args.kwargs == {"content": "🧺 Basket cleared.", "view": None}
    assert state.store.basket(7) is None


async def test_dynamic_items_parse_custom_ids() -> None:
    match = re.fullmatch(RevokeButton.__discord_ui_compiled_template__, "zb:revoke:abcDEF_-12")
    assert match
    item = await RevokeButton.from_custom_id(cast(Any, None), cast(Any, None), match)
    assert item.link_id == "abcDEF_-12"
    assert not re.fullmatch(RevokeButton.__discord_ui_compiled_template__, "zb:revoke:../../x")


# --- cog callbacks -------------------------------------------------------------------


async def test_context_menu_callbacks(settings: Settings, state: Any) -> None:
    from zipbot.cogs.context_menu import ContextMenus

    bot = ZipBot(settings, state.store)
    cog = ContextMenus(bot)
    raw = media_payload()
    msg = cast(Any, SimpleNamespace(id=int(raw["id"]), channel=SimpleNamespace(id=5)))
    inter = interaction(state, data={"resolved": {"messages": {raw["id"]: raw}}})
    await cog.download(inter, msg)
    kwargs = inter.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and kwargs["view"] is not None
    inter2 = interaction(state, data={"resolved": {"messages": {raw["id"]: raw}}})
    await cog.add(inter2, msg)
    assert "Added" in inter2.response.send_message.call_args.args[0]
    basket = state.store.basket(7)
    assert basket and basket.entries[0].message_key == f"5/{raw['id']}"
    await bot.close()


async def test_basket_cog_callbacks(settings: Settings, state: Any) -> None:
    from zipbot.cogs.basket import BasketCog

    bot = ZipBot(settings, state.store)
    cog = BasketCog(bot)
    for name in ("show", "link", "clear"):
        inter = interaction(state)
        await getattr(cog, name).callback(cog, inter)
        assert inter.response.send_message.call_args.kwargs["ephemeral"] is True
    await bot.close()


# --- errors & cooldown ---------------------------------------------------------------


async def test_cooldown_on_every_command(settings: Settings) -> None:
    bot = ZipBot(settings, Store.from_settings(settings))
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    menus = bot.tree.get_commands(type=discord.AppCommandType.message)
    slash = [c for c in bot.tree.walk_commands() if not isinstance(c, discord.app_commands.Group)]
    leaf = [*menus, *slash]
    assert len(leaf) == 5
    assert all(c.checks for c in leaf)
    await bot.close()


@pytest.mark.parametrize("done", [False, True])
async def test_error_handler(settings: Settings, state: Any, done: bool) -> None:
    bot = ZipBot(settings, state.store)
    inter = interaction(state)
    inter.response.is_done = lambda: done
    inter.followup = SimpleNamespace(send=AsyncMock())
    cooldown = discord.app_commands.CommandOnCooldown(discord.app_commands.Cooldown(10, 60), 12.3)
    await bot.on_app_command_error(cast(Any, inter), cooldown)
    await bot.on_app_command_error(cast(Any, inter), discord.app_commands.AppCommandError("x"))
    sender = inter.followup.send if done else inter.response.send_message
    texts = [c.args[0] for c in sender.call_args_list]
    assert texts == [
        "You're going a bit fast — try again in 12 s.",
        "Something went wrong. Please try again.",
    ]
    assert all(c.kwargs["ephemeral"] for c in sender.call_args_list)
    await bot.close()


# --- command sync --------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        None,
        (0, "description", "old text"),
        (1, "name", "Download media"),  # v0.0.0 name: Discord keeps it until we re-sync
    ],
)
async def test_sync_only_when_commands_changed(
    settings: Settings, change: tuple[int, str, str] | None
) -> None:
    bot = ZipBot(settings, Store.from_settings(settings))
    bot._connection.application_id = 1  # normally set at login
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    remote: list[Any] = cast(Any, fixture("global_commands.json"))  # Discord's view of the current commands
    if change:
        index, key, old = change
        remote[index][key] = old
    bot.http.get_global_commands = AsyncMock(return_value=remote)  # type: ignore[method-assign]
    bot.tree.sync = AsyncMock(return_value=[])  # type: ignore[method-assign]
    await bot.sync_commands()
    assert bot.tree.sync.await_count == (1 if change else 0)
    await bot.close()
