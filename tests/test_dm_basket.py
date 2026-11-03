"""The basket as one long-lived message in the bot DM (dm_basket.py), against a fake Discord."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from helpers import (
    EX,
    FakeDiscord,
    FakeDmChannel,
    fixture,
    forbidden,
    http_error,
    item,
    resolved_message,
)
from zipbot import dm_basket, handlers, views
from zipbot.bot import ZipBot
from zipbot.config import Settings
from zipbot.dm_basket import BACKOFF_SEC, SCAN_LIMIT, UNAVAILABLE_SEC, DmBaskets
from zipbot.store import Store
from zipbot.views import (
    BASKET_EXPIRED,
    DM_FALLBACK,
    DM_POSTING,
    DYNAMIC_ITEMS,
    ID_BASKET,
    ID_JUMP,
    AddLinkToBasketButton,
    BasketClearButton,
    BasketRefreshButton,
    DmPost,
    Panels,
    Toast,
    UserCooldown,
)

JUMP = re.compile(r"^https://discord\.com/channels/@me/(\d+)/(\d+)$")
TOKEN_IN_URL = re.compile(r"/d#([A-Za-z0-9_-]{43})(\.files|\.zip)?$")


class Clock:
    def __init__(self) -> None:
        self.t = float(EX - 24 * 3600)

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def quick_toasts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "TOAST_SEC", 0)


@pytest.fixture
async def env(settings: Settings) -> AsyncIterator[Any]:
    """The bot as the DM basket sees it: store, settings, panels and a fake Discord."""
    fake = FakeDiscord()
    clock = Clock()
    store = Store.from_settings(settings)
    store.clock = clock
    ns = SimpleNamespace(
        store=store,
        settings=settings,
        panels=Panels(),
        button_cooldown=UserCooldown(),
        user=fake.user,
        create_dm=fake.create_dm,
        get_partial_messageable=fake.get_partial_messageable,
        fake=fake,
        clock=clock,
    )
    restart(ns)
    yield ns
    await ns.dm_baskets.close()


def restart(env: Any, **kw: Any) -> None:
    """A fresh DM basket (as after a bot restart), wired to the store like ZipBot does."""
    env.dm_baskets = DmBaskets(cast(Any, env), gap=0, **kw)
    env.store.on_basket_link_gone = env.dm_baskets.link_gone


_ids = itertools.count(7000)


def interaction(env: Any, *, message: Any = None, channel_id: int | None = None, in_dm: bool = False) -> Any:
    sent = SimpleNamespace(message_id=next(_ids))
    return SimpleNamespace(
        user=SimpleNamespace(id=7),
        client=env,
        data=None,
        message=message,
        channel_id=channel_id,
        context=SimpleNamespace(dm_channel=in_dm),
        sent=sent,
        response=SimpleNamespace(
            send_message=AsyncMock(return_value=sent), edit_message=AsyncMock(), defer=AsyncMock()
        ),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


async def settle(env: Any) -> None:
    """Run every background task (editor, scans, toasts) to the end."""
    dm = env.dm_baskets
    for _ in range(50):
        await asyncio.sleep(0)
        tasks = [*dm._tasks.values(), *dm._background, *env.panels._tasks]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def flat(view: Any) -> Iterator[dict[str, Any]]:
    def walk(components: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for c in components:
            yield c
            yield from walk(c.get("components", []))
            if "accessory" in c:
                yield c["accessory"]

    return walk(view.to_components())


def text(view: Any) -> str:
    return "\n".join(c["content"] for c in flat(view) if c["type"] == 10)


def link_buttons(view: Any) -> list[dict[str, Any]]:
    return [c for c in flat(view) if c["type"] == 2 and c.get("url")]


def custom_ids(view: Any) -> list[str]:
    return [c["custom_id"] for c in flat(view) if c["type"] == 2 and "custom_id" in c]


def jump_of(view: Any) -> str | None:
    return next((c["url"] for c in link_buttons(view) if c.get("id") == ID_JUMP), None)


def tokens(view: Any) -> set[str]:
    return {m.group(1) for c in link_buttons(view) if (m := TOKEN_IN_URL.search(c["url"]))}


def media_payload() -> dict[str, Any]:
    return resolved_message(fixture("interaction_context_menu_attachments.json"))


def add(env: Any, key: str = "c/1", payload: dict[str, Any] | None = None) -> views.Reply:
    return handlers.add_to_basket_reply(env, 7, payload or media_payload(), key)


def dm_channel(env: Any) -> FakeDmChannel:
    return env.fake.dm_of(7)


def live_message(env: Any) -> Any:
    jump = env.dm_baskets.jump_url(7)
    assert jump
    m = JUMP.match(jump)
    assert m
    return env.fake.channels[int(m.group(1))].messages[int(m.group(2))]


async def posted(env: Any) -> Any:
    """Add once and let the DM message be posted; returns it."""
    await views.show(interaction(env), add(env))
    await settle(env)
    return live_message(env)


# --- none → live ---------------------------------------------------------------------


async def test_first_add_posts_silently_and_the_toast_jumps_there(env: Any) -> None:
    reply = add(env)
    assert isinstance(reply, Toast)
    assert reply.text == "✓ Added 5 files — 🧺 5 files in your basket"
    inter = interaction(env)
    await views.show(inter, reply)  # answered at once: nothing waits for the DM
    first = inter.response.send_message.call_args.kwargs
    assert first["ephemeral"] is True
    assert text(first["view"]) == f"{reply.text}\n{DM_POSTING}" and jump_of(first["view"]) is None

    await settle(env)
    ch = dm_channel(env)
    ((view, silent),) = ch.sends
    assert silent is True  # SUPPRESS_NOTIFICATIONS: no push for the new DM
    message = live_message(env)
    assert message.view is view
    assert text(view).startswith("🧺 **Basket: 5 files from 1 message** · 51 KB · link expires <t:")
    assert [b["label"] for b in link_buttons(view)] == ["Download all", "Basket ZIP"]
    assert custom_ids(view) == ["zb:basketclear", "zb:dmbasket:refresh"]

    edited = inter.edit_original_response.call_args.kwargs["view"]
    assert text(edited) == reply.text
    assert jump_of(edited) == f"https://discord.com/channels/@me/{ch.id}/{message.id}"
    inter.delete_original_response.assert_awaited_once()  # the toast deleted itself
    assert len(env.panels) == 0  # ...and was never the live panel


async def test_toast_never_deletes_the_live_panel(env: Any) -> None:
    await posted(env)
    live = interaction(env)
    await views.show(live, handlers.download_reply(env, 7, media_payload(), "c/9"))  # a link panel
    toast = interaction(env)
    await views.show(toast, add(env, "c/2", fixture("message_image_embed.json")))
    await settle(env)
    live.delete_original_response.assert_not_awaited()
    toast.delete_original_response.assert_awaited_once()
    assert len(env.panels) == 1


def test_jump_url_format() -> None:
    assert views.jump_url(123, 456) == "https://discord.com/channels/@me/123/456"


async def test_later_adds_edit_the_same_message(env: Any) -> None:
    message = await posted(env)
    old = tokens(message.view)
    reply = add(env, "c/2", fixture("message_image_embed.json"))
    inter = interaction(env)
    await views.show(inter, reply)
    toast = inter.response.send_message.call_args.kwargs["view"]
    assert jump_of(toast) == env.dm_baskets.jump_url(7)  # live: the link is there at once
    assert DM_POSTING not in text(toast)
    await settle(env)
    ch = dm_channel(env)
    assert len(ch.sends) == 1 and [mid for mid, _ in ch.edits] == [message.id]
    assert "6 files from 2 messages" in text(message.view)
    assert all(env.store.resolve(t) is None for t in old)  # re-minted with the edit
    assert all(env.store.resolve(t) for t in tokens(message.view))
    assert env.store.active_links(7) == 1  # one basket link, shown only in the DM


async def test_rapid_changes_are_coalesced(env: Any) -> None:
    ch = dm_channel(env)
    ch.gate = asyncio.Event()  # the first send hangs
    add(env, "k/0")
    await asyncio.sleep(0)
    for n in range(1, 6):
        add(env, f"k/{n}", {"attachments": [media_payload()["attachments"][0]]})
    assert len(env.dm_baskets._tasks) == 1  # one editor for the user
    ch.gate.set()
    await settle(env)
    assert len(ch.sends) == 1 and len(ch.edits) == 1  # five changes, one edit: the latest state
    assert "10 files from 6 messages" in text(live_message(env).view)


async def test_unchanged_basket_is_not_edited(env: Any) -> None:
    await posted(env)
    reply = add(env)  # the same message again
    assert isinstance(reply, Toast)
    assert reply.text.startswith("That message is already in your basket.\n-# 🧺 5 files")
    await settle(env)
    assert dm_channel(env).edits == []


async def test_answers_never_wait_for_the_dm(env: Any) -> None:
    ch = dm_channel(env)
    ch.gate = asyncio.Event()  # Discord is slow
    inter = interaction(env)
    await asyncio.wait_for(views.show(inter, add(env)), 0.5)
    inter.response.send_message.assert_awaited_once()
    ch.gate.set()
    await settle(env)


# --- live → gone ----------------------------------------------------------------------


async def test_clear_deletes_the_message_and_revokes_the_link(env: Any) -> None:
    message = await posted(env)
    shown = tokens(message.view)
    handlers.basket_clear_reply(env, 7)
    await settle(env)
    ch = dm_channel(env)
    assert ch.deletes == [message.id] and message.id not in ch.messages
    assert all(env.store.resolve(t) is None for t in shown)
    assert not env.dm_baskets.live(7)
    await views.show(interaction(env), add(env))  # a new basket: a new message
    await settle(env)
    assert len(ch.sends) == 2


async def test_clear_then_add_before_the_delete_lands_keeps_one_message(env: Any) -> None:
    await posted(env)
    ch = dm_channel(env)
    ch.gate = asyncio.Event()
    handlers.basket_clear_reply(env, 7)
    await asyncio.sleep(0)  # the delete is in flight
    add(env, "c/2", fixture("message_image_embed.json"))
    ch.gate.set()
    await settle(env)
    assert len(ch.deletes) == 1 and len(ch.sends) == 2  # deleted, then posted anew
    assert "1 file from 1 message" in text(live_message(env).view)


async def test_failed_delete_falls_back_to_expired(env: Any) -> None:
    message = await posted(env)
    ch = dm_channel(env)
    ch.delete_error = aiohttp.ClientConnectionError("reset")
    handlers.basket_clear_reply(env, 7)
    await settle(env)
    assert_expired(message.view)  # not deleted, but no names or thumbnails left behind
    assert not env.dm_baskets.live(7)


async def test_failed_expiry_is_retried_by_the_tick(env: Any) -> None:
    message = await posted(env)
    ch = dm_channel(env)
    ch.edit_error = aiohttp.ClientConnectionError("reset")
    env.clock.t += env.store.basket_ttl_sec
    env.dm_baskets.tick()
    await settle(env)
    assert "Basket:" in text(message.view) and env.dm_baskets.live(7)  # kept for a retry
    ch.edit_error = None
    env.clock.t += dm_basket.TICK_SEC
    env.dm_baskets.tick()
    await settle(env)
    assert_expired(message.view)
    assert not env.dm_baskets.live(7)


async def test_expiry_retries_are_bounded(env: Any) -> None:
    await posted(env)
    dm_channel(env).edit_error = aiohttp.ClientConnectionError("reset")
    env.clock.t += env.store.basket_ttl_sec
    for _ in range(dm_basket.MAX_RETRIES + 1):
        env.dm_baskets.tick()
        await settle(env)
        env.clock.t += dm_basket.TICK_SEC
    assert not env.dm_baskets.live(7)  # given up
    edits_tried = len(dm_channel(env).edits)
    env.dm_baskets.tick()
    await settle(env)
    assert len(dm_channel(env).edits) == edits_tried


def assert_expired(view: Any) -> None:
    assert text(view) == BASKET_EXPIRED
    assert link_buttons(view) == [] and custom_ids(view) == []
    assert not any(c["type"] == 12 for c in flat(view))  # no gallery
    assert ".jpg" not in str(view.to_components())  # no file names


async def test_basket_expiry_edits_it_to_expired(env: Any) -> None:
    message = await posted(env)
    env.dm_baskets.tick()  # not due yet
    await settle(env)
    assert dm_channel(env).edits == []
    env.clock.t += env.store.basket_ttl_sec
    env.dm_baskets.tick()
    await settle(env)
    assert_expired(message.view)
    assert message.id in dm_channel(env).messages  # edited, not deleted
    assert not env.dm_baskets.live(7) and env.store.active_links(7) == 0


async def test_link_expiry_before_the_basket_redraws_with_a_fresh_link(env: Any) -> None:
    message = await posted(env)
    old = tokens(message.view)
    env.clock.t += 1800
    add(env)  # a duplicate: the basket lives on, its link doesn't
    env.clock.t += 1800
    env.dm_baskets.tick()
    await settle(env)
    assert "🧺 **Basket: 5 files from 1 message**" in text(message.view)  # still live, not expired
    new = tokens(message.view)
    assert new and new != old and all(env.store.resolve(t) for t in new)
    assert env.dm_baskets.is_live_message(7, message.id)


async def test_store_redraws_the_dm_when_the_basket_link_goes(env: Any) -> None:
    message = await posted(env)
    old = tokens(message.view)
    env.clock.t += 1800
    add(env)  # extends the basket only
    env.clock.t += 1800
    env.store.sweep()  # e.g. inside any create_link: the basket's link runs out
    await settle(env)  # no tick needed
    new = tokens(message.view)
    assert new and new != old and all(env.store.resolve(t) for t in new)


async def test_many_panels_never_evict_the_dm_basket_link(env: Any) -> None:
    message = await posted(env)
    shown = tokens(message.view)
    for n in range(3 * env.settings.max_active_links_per_user):  # Download media / DM bursts
        handlers.download_reply(env, 7, media_payload(), f"c/{100 + n}")
    assert all(env.store.resolve(t) for t in shown)  # Basket ZIP and Open basket still work
    env.dm_baskets.tick()
    await settle(env)
    assert dm_channel(env).edits == []


async def test_deleted_message_is_posted_again_at_once(env: Any) -> None:
    message = await posted(env)
    ch = dm_channel(env)
    del ch.messages[message.id]  # the user deleted it
    inter = interaction(env)
    await views.show(inter, add(env, "c/2", fixture("message_image_embed.json")))
    old_jump = jump_of(inter.response.send_message.call_args.kwargs["view"])
    await settle(env)  # the edit gets 404 / 10008, and the same run posts it anew
    assert len(ch.sends) == 2
    new = live_message(env)
    assert new.id != message.id and "6 files from 2 messages" in text(new.view)
    new_jump = jump_of(inter.edit_original_response.call_args.kwargs["view"])
    assert old_jump == views.jump_url(ch.id, message.id) and new_jump == views.jump_url(ch.id, new.id)


# --- fallback -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "window"),
    [
        (forbidden(50278), UNAVAILABLE_SEC),  # can't DM this user (not installed, no shared server)
        (forbidden(50007), UNAVAILABLE_SEC),  # blocked
        (forbidden(50001), BACKOFF_SEC),  # some other 403: not "no DMs", so retried soon
        (http_error(discord.HTTPException, 500, 0), BACKOFF_SEC),
        (aiohttp.ClientConnectionError("reset"), BACKOFF_SEC),
        (TimeoutError(), BACKOFF_SEC),
    ],
)
async def test_send_failure_falls_back_to_the_panel(env: Any, error: BaseException, window: float) -> None:
    dm_channel(env).send_error = error
    inter = interaction(env)
    await views.show(inter, add(env))
    await settle(env)
    fallback = inter.edit_original_response.call_args.kwargs["view"]
    assert text(fallback).startswith(f"✓ Added 5 files to your basket.\n{DM_FALLBACK}\n🧺 **Basket: 5 files")
    assert [b["label"] for b in link_buttons(fallback)] == ["Download all", "Basket ZIP"]
    inter.delete_original_response.assert_not_awaited()  # it is the live panel now
    assert len(env.panels) == 1
    assert not env.dm_baskets.usable(7) and not env.dm_baskets.live(7)

    again = add(env, "c/2", fixture("message_image_embed.json"))  # today's panel, no second note
    assert isinstance(again, discord.ui.LayoutView) and DM_FALLBACK not in text(again)
    await settle(env)
    assert dm_channel(env).sends == []

    env.clock.t += window - 1
    assert not env.dm_baskets.usable(7)
    env.clock.t += 1
    dm_channel(env).send_error = None
    assert env.dm_baskets.usable(7)  # tried again after the window
    assert isinstance(add(env, "c/3", {"attachments": [media_payload()["attachments"][0]]}), Toast)
    await settle(env)
    assert len(dm_channel(env).sends) == 1


async def test_a_400_is_logged_as_our_bug(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    dm_channel(env).send_error = http_error(discord.HTTPException, 400, 50035)  # invalid form body
    with caplog.at_level(logging.INFO, logger="zipbot.dm_basket"):
        add(env)
        await settle(env)
    (record,) = [r for r in caplog.records if r.name == "zipbot.dm_basket"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "DM basket send failed: HTTPException 50035"  # type and code only
    env.clock.t += BACKOFF_SEC
    assert env.dm_baskets.usable(7)


async def test_unavailable_marks_are_bounded(env: Any) -> None:
    dm = DmBaskets(env, cap=3)
    for user in range(5):
        dm.mark_unavailable(user)
    assert len(dm._unavailable) == 3 and dm.usable(0) and not dm.usable(4)


async def test_edit_403_falls_back(env: Any) -> None:
    await posted(env)
    dm_channel(env).edit_error = forbidden(50007)  # blocked since
    add(env, "c/2", fixture("message_image_embed.json"))
    await settle(env)
    assert not env.dm_baskets.live(7) and not env.dm_baskets.usable(7)


async def test_edit_network_error_keeps_it_and_redraws_later(env: Any) -> None:
    message = await posted(env)
    dm_channel(env).edit_error = aiohttp.ClientConnectionError("reset")
    add(env, "c/2", fixture("message_image_embed.json"))
    await settle(env)
    assert env.dm_baskets.live(7) and "1 message" in text(message.view)  # stale for now
    dm_channel(env).edit_error = None
    env.clock.t += 2 * dm_basket.TICK_SEC
    env.dm_baskets.tick()
    await settle(env)
    assert "6 files from 2 messages" in text(message.view)


async def test_off_switch(env: Any) -> None:
    env.settings = env.settings.model_copy(update={"basket_dm": False})
    assert isinstance(add(env), discord.ui.LayoutView)
    await settle(env)
    assert dm_channel(env).sends == [] and not env.dm_baskets.live(7)


# --- other surfaces while live ----------------------------------------------------------


async def test_link_panel_jumps_to_the_dm_basket_and_never_mints(env: Any) -> None:
    await posted(env)
    links = env.store.active_links(7)
    view = handlers.download_reply(env, 7, media_payload(), "c/9")
    assert isinstance(view, discord.ui.LayoutView)
    assert "🧺 Basket: 5 files from 1 message" in text(view)
    assert jump_of(view) == env.dm_baskets.jump_url(7)
    assert not any(b.get("id") == ID_BASKET for b in link_buttons(view))  # no Basket ZIP
    assert env.store.active_links(7) == links + 1  # just the panel's own link


async def test_add_button_edits_its_panel_and_jumps_to_the_dm(env: Any) -> None:
    await posted(env)
    view = handlers.download_reply(env, 7, fixture("message_image_embed.json"), "c/2")
    assert isinstance(view, discord.ui.LayoutView)
    link_id = custom_ids(view)[0].removeprefix("zb:basket:")
    inter = interaction(env, message=panel_message(view))
    await AddLinkToBasketButton(link_id).callback(inter)
    edited = inter.response.edit_message.call_args.kwargs["view"]
    assert "🧺 Basket: 6 files from 2 messages" in text(edited)
    assert jump_of(edited) == env.dm_baskets.jump_url(7)
    await settle(env)
    assert "6 files from 2 messages" in text(live_message(env).view)
    inter.edit_original_response.assert_not_awaited()  # nothing to redraw


async def test_add_button_during_the_first_post_redraws_with_the_jump(env: Any) -> None:
    view = handlers.download_reply(env, 7, media_payload(), "c/1")
    assert isinstance(view, discord.ui.LayoutView)
    link_id = custom_ids(view)[0].removeprefix("zb:basket:")
    inter = interaction(env, message=panel_message(view))
    await AddLinkToBasketButton(link_id).callback(inter)
    first = inter.response.edit_message.call_args.kwargs["view"]
    assert "🧺 Basket: 5 files" in text(first) and jump_of(first) is None  # being posted
    assert not any(b.get("id") == ID_BASKET for b in link_buttons(first))
    await settle(env)
    redrawn = inter.edit_original_response.call_args.kwargs["view"]
    assert jump_of(redrawn) == env.dm_baskets.jump_url(7)


async def test_add_button_falls_back_when_the_dm_fails(env: Any) -> None:
    dm_channel(env).send_error = forbidden()
    view = handlers.download_reply(env, 7, media_payload(), "c/1")
    assert isinstance(view, discord.ui.LayoutView)
    link_id = custom_ids(view)[0].removeprefix("zb:basket:")
    inter = interaction(env, message=panel_message(view))
    await AddLinkToBasketButton(link_id).callback(inter)
    await settle(env)
    redrawn = inter.edit_original_response.call_args.kwargs["view"]
    assert DM_FALLBACK in text(redrawn)
    assert any(b.get("id") == ID_BASKET for b in link_buttons(redrawn))  # today's Basket ZIP


def panel_message(view: discord.ui.LayoutView, *, ephemeral: bool = True) -> Any:
    from discord.components import _component_factory

    return SimpleNamespace(
        id=next(_ids),
        flags=SimpleNamespace(components_v2=True, ephemeral=ephemeral),
        components=[_component_factory(cast(Any, d)) for d in view.to_components()],
    )


async def test_basket_show_points_at_the_dm_message(env: Any) -> None:
    assert handlers.basket_show_reply(env, 7) == views.BASKET_EMPTY
    env.store.add_to_basket(7, "k", [item()])
    assert isinstance(handlers.basket_show_reply(env, 7), discord.ui.LayoutView)  # not live: the panel
    await posted(env)
    reply = handlers.basket_show_reply(env, 7)
    assert isinstance(reply, Toast) and reply.text.startswith("🧺 Basket: 6 files from 2 messages")
    inter = interaction(env)
    await views.show(inter, reply)
    assert jump_of(inter.response.send_message.call_args.kwargs["view"]) == env.dm_baskets.jump_url(7)


async def test_basket_command_in_the_bot_dm_becomes_the_dm_message(env: Any) -> None:
    env.store.add_to_basket(7, "k", [item()])
    reply = handlers.basket_show_reply(env, 7, in_bot_dm=True)
    assert isinstance(reply, DmPost)
    ch = dm_channel(env)
    answer = ch.put(reply.view)  # the interaction response, a normal message in the DM
    inter = interaction(env, channel_id=ch.id, in_dm=True)
    inter.sent.message_id = answer.id
    await views.show(inter, reply)
    assert "ephemeral" not in inter.response.send_message.call_args.kwargs
    assert env.dm_baskets.jump_url(7) == views.jump_url(ch.id, answer.id)
    add(env, "c/2", fixture("message_image_embed.json"))
    await settle(env)
    assert ch.sends == [] and [mid for mid, _ in ch.edits] == [answer.id]  # edited, not re-posted


async def test_add_during_the_bot_dm_answer_leaves_one_live_message(env: Any) -> None:
    env.store.add_to_basket(7, "k", [item()])
    reply = handlers.basket_show_reply(env, 7, in_bot_dm=True)
    assert isinstance(reply, DmPost)
    ch = dm_channel(env)
    answer = ch.put(reply.view)
    add(env, "c/2", fixture("message_image_embed.json"))  # lands while the answer is being sent
    inter = interaction(env, channel_id=ch.id, in_dm=True)
    inter.sent.message_id = answer.id
    await views.show(inter, reply)
    await settle(env)
    assert_expired(answer.view)  # the answer is not left behind with names and a gallery
    assert live_message(env).id != answer.id and len(ch.sends) == 1


async def test_basket_cog_answers_in_the_bot_dm(settings: Settings) -> None:
    from zipbot.cogs.basket import BasketCog

    bot = ZipBot(settings, Store.from_settings(settings))
    cog = BasketCog(bot)
    bot.store.add_to_basket(7, "k", [item()])
    inter = interaction(cast(Any, bot), in_dm=True)
    assert isinstance(cog.reply(inter), DmPost)
    inter.context.dm_channel = False
    assert isinstance(cog.reply(inter), discord.ui.LayoutView)
    await bot.close()


# --- restart --------------------------------------------------------------------------


async def test_restart_turns_old_basket_messages_into_expired(env: Any) -> None:
    old = await posted(env)
    ch = dm_channel(env)
    other_panel = ch.put(handlers.download_reply(env, 7, media_payload(), "c/9"))  # a DM burst reply
    impostor = ch.put(old.view, author_id=7)  # the user's own message: not ours to touch
    restart(env)  # restart: nothing is known
    env.store.clear_basket(7)
    edits_before = len(ch.edits)
    ch.history_calls.clear()  # the scan of the first start

    await views.show(interaction(env), add(env, "c/2", fixture("message_image_embed.json")))
    await settle(env)
    assert ch.history_calls == [SCAN_LIMIT]
    assert_expired(old.view)
    assert [mid for mid, _ in ch.edits[edits_before:]] == [old.id]  # not the user's own message
    assert "📦" in text(other_panel.view)  # untouched
    assert len(ch.sends) == 2 and live_message(env).id not in (old.id, impostor.id)

    add(env, "c/3", {"attachments": [media_payload()["attachments"][0]]})
    await settle(env)
    assert ch.history_calls == [SCAN_LIMIT]  # once per user per process


async def test_stale_refresh_revives_the_message_or_expires_it(env: Any) -> None:
    old = await posted(env)
    ch = dm_channel(env)
    restart(env)  # restart, but the basket is (again) there
    click = interaction(env, message=old, channel_id=ch.id, in_dm=True)
    await BasketRefreshButton().callback(click)
    click.response.defer.assert_awaited_once()
    await settle(env)
    assert env.dm_baskets.jump_url(7) == views.jump_url(ch.id, old.id)  # live again, where it was
    assert "🧺 **Basket: 5 files" in text(old.view)

    restart(env)
    env.store.clear_basket(7)
    click = interaction(env, message=old, channel_id=ch.id, in_dm=True)
    await BasketRefreshButton().callback(click)
    assert_expired(click.response.edit_message.call_args.kwargs["view"])
    assert not env.dm_baskets.live(7)


async def test_refresh_on_an_old_copy_takes_over(env: Any) -> None:
    first = await posted(env)
    ch = dm_channel(env)
    restart(env)
    await views.show(interaction(env), add(env, "c/2", fixture("message_image_embed.json")))
    await settle(env)  # posted a new one (and expired the first)
    second = live_message(env)
    rendered = views.dm_basket_view(env, 7)
    assert rendered
    first.view = rendered[0]  # pretend the expiry edit never landed
    click = interaction(env, message=first, channel_id=ch.id, in_dm=True)
    await BasketRefreshButton().callback(click)
    await settle(env)
    assert env.dm_baskets.is_live_message(7, first.id)
    assert_expired(second.view)  # one live message per user


async def test_refresh_on_the_live_message_redraws_it(env: Any) -> None:
    message = await posted(env)
    ch = dm_channel(env)
    click = interaction(env, message=message, channel_id=ch.id, in_dm=True)
    await BasketRefreshButton().callback(click)
    click.response.defer.assert_awaited_once()
    await settle(env)
    assert [mid for mid, _ in ch.edits] == [message.id] and len(ch.sends) == 1


async def test_clear_on_the_dm_message(env: Any) -> None:
    message = await posted(env)
    ch = dm_channel(env)
    click = interaction(env, message=message, channel_id=ch.id, in_dm=True)
    await BasketClearButton().callback(click)
    click.response.defer.assert_awaited_once()
    await settle(env)
    assert ch.deletes == [message.id] and env.store.basket(7) is None

    stale = ch.put(message.view)  # an old copy from before a restart
    click = interaction(env, message=stale, channel_id=ch.id, in_dm=True)
    await BasketClearButton().callback(click)
    assert_expired(click.response.edit_message.call_args.kwargs["view"])


# --- bounds & wiring ------------------------------------------------------------------


async def test_evicted_messages_are_expired(env: Any) -> None:
    restart(env, cap=2)
    for user in (1, 2):
        env.store.add_to_basket(user, "k", [item()])
        env.dm_baskets.changed(user)
        await settle(env)
    first = next(iter(env.fake.dm_of(1).messages.values()))
    env.store.add_to_basket(3, "k", [item()])
    env.dm_baskets.changed(3)
    await settle(env)
    assert not env.dm_baskets.live(1)
    assert_expired(first.view)


async def test_live_messages_are_bounded(env: Any) -> None:
    restart(env, cap=2)
    for user in (1, 2, 3):
        env.store.add_to_basket(user, "k", [item()])
        env.dm_baskets.changed(user)
    await settle(env)
    assert len(env.dm_baskets._refs) == 2 and not env.dm_baskets.live(1)
    # User 1 was dropped before its message went out, so nothing was posted for it.
    assert [len(env.fake.dm_of(u).sends) for u in (1, 2, 3)] == [0, 1, 1]


def test_refresh_routes_to_a_dynamic_item() -> None:
    assert BasketRefreshButton in DYNAMIC_ITEMS
    cid = "zb:dmbasket:refresh"
    assert sum(bool(d.__discord_ui_compiled_template__.fullmatch(cid)) for d in DYNAMIC_ITEMS) == 1


async def test_bot_wiring(settings: Settings) -> None:
    bot = ZipBot(settings, Store.from_settings(settings))
    assert settings.basket_dm is True  # on by default
    assert isinstance(bot.dm_baskets, DmBaskets) and bot.dm_baskets.enabled
    bot.dm_baskets.start()
    assert bot.dm_baskets._ticker is not None
    await bot.close()
    assert bot.dm_baskets._ticker is None
