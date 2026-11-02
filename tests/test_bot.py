from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from discord.components import _component_factory

from helpers import EX, fixture, item, resolved_message
from zipbot import handlers, views
from zipbot.bot import EXTENSIONS, ZipBot, intents
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.views import (
    BASKET_CLEARED,
    BASKET_EMPTY,
    DYNAMIC_ITEMS,
    ID_BASKET,
    ID_FILES,
    ID_ZIP,
    LINK_EXPIRED,
    LINK_GONE,
    PANEL_TOKEN_SEC,
    REVOKED,
    AddLinkToBasketButton,
    BasketClearButton,
    BasketLinkButton,
    Panels,
    RevokeButton,
    UserCooldown,
    format_size,
    safe_name,
)

ATTACHMENTS = "interaction_context_menu_attachments.json"
NO_MEDIA = "This message has no images or videos."
TOKEN_IN_URL = re.compile(r"/d#([A-Za-z0-9_-]{43})(\.files|\.zip)?$")


@pytest.fixture
def state(settings: Settings) -> Any:
    store = Store.from_settings(settings)
    store.clock = lambda: EX - 24 * 3600
    return SimpleNamespace(store=store, settings=settings, panels=Panels(), button_cooldown=UserCooldown())


_message_ids = iter(range(1000, 10**6))


def interaction(state: Any, data: dict[str, Any] | None = None, user_id: int = 7, message: Any = None) -> Any:
    sent = SimpleNamespace(message_id=next(_message_ids))
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        client=state,
        data=data,
        message=message,
        sent=sent,  # what send_message returns: the new message's id
        response=SimpleNamespace(send_message=AsyncMock(return_value=sent), edit_message=AsyncMock()),
        delete_original_response=AsyncMock(),
    )


def message_of(view: discord.ui.LayoutView, *, cv2: bool = True, ephemeral: bool = True) -> Any:
    """The panel as Discord sends it back on a click (parsed components, flags)."""
    return SimpleNamespace(
        id=next(_message_ids),
        flags=SimpleNamespace(components_v2=cv2, ephemeral=ephemeral),
        components=[_component_factory(cast(Any, d)) for d in view.to_components()],
    )


def flat(view: discord.ui.LayoutView) -> Iterator[dict[str, Any]]:
    """Every component of the payload, nested ones included (what Discord's limit of 40 counts)."""

    def walk(components: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for c in components:
            yield c
            yield from walk(c.get("components", []))
            if "accessory" in c:
                yield c["accessory"]

    return walk(view.to_components())


def texts(view: Any) -> list[str]:
    return [c["content"] for c in flat(view) if c["type"] == 10]


def text(view: Any) -> str:
    return "\n".join(texts(view))


def gallery(view: Any) -> list[dict[str, Any]]:
    galleries = [c for c in flat(view) if c["type"] == 12]
    assert len(galleries) <= 1
    return galleries[0]["items"] if galleries else []


def link_buttons(view: Any) -> list[dict[str, Any]]:
    return [c for c in flat(view) if c["type"] == 2 and c.get("url")]


def custom_ids(view: Any) -> list[str]:
    return [c["custom_id"] for c in flat(view) if c["type"] == 2 and "custom_id" in c]


def panel(reply: views.Reply) -> discord.ui.LayoutView:
    assert isinstance(reply, discord.ui.LayoutView), reply
    return reply


def token_of(url: str) -> str:
    m = TOKEN_IN_URL.search(url)
    assert m
    return m.group(1)


def main_urls(view: Any) -> tuple[str, str]:
    by_id = {c.get("id"): c["url"] for c in link_buttons(view)}
    return by_id[ID_FILES], by_id[ID_ZIP]


def basket_zip_url(view: Any) -> str | None:
    return next((c["url"] for c in link_buttons(view) if c.get("id") == ID_BASKET), None)


def media_payload() -> dict[str, Any]:
    return resolved_message(fixture(ATTACHMENTS))


def link_of(state: Any, files: int = 2, **kw: Any) -> tuple[discord.ui.LayoutView, Any]:
    reply = panel(handlers.link_reply(state, 7, [item(f"{i}.jpg") for i in range(files)], "c/m", **kw))
    record = state.store.resolve(token_of(main_urls(reply)[0]))
    return reply, record


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
    assert set(payloads) == {"Download media", "Add to ZIP basket", "basket"}
    for p in payloads.values():
        assert sorted(p["integration_types"]) == [0, 1]
        assert sorted(p["contexts"]) == [0, 1, 2]
    assert payloads["Download media"]["type"] == 3
    assert {o["name"] for o in payloads["basket"]["options"]} == {"show", "link", "clear"}
    for ext in EXTENSIONS:
        await bot.unload_extension(ext)
    assert bot.tree.get_commands() == []
    await bot.close()


# --- the link panel ------------------------------------------------------------------


async def test_download_reply_panel(state: Any) -> None:
    view = panel(handlers.download_reply(state, 7, media_payload(), "c/m"))
    assert view.is_finished()  # detached (needs a running loop): buttons are handled by DynamicItems
    (container,) = view.to_components()
    assert container["type"] == 17  # one Container; Components V2 carries no `content`
    assert view.has_components_v2()
    assert texts(view)[0].startswith("📦 **5 files** · 51 KB · link expires <t:")

    links = link_buttons(view)
    assert [b["label"] for b in links] == ["Download all", "Download ZIP"]  # "Open page" is gone
    files_url, zip_url = main_urls(view)
    assert files_url.endswith(".files") and zip_url.endswith(".zip")
    assert token_of(files_url) == token_of(zip_url)
    assert all(b["url"].startswith("https://dl.example.com/d#") and len(b["url"]) <= 512 for b in links)
    record = state.store.resolve(token_of(files_url))
    assert record and record.message_key == "c/m"
    assert custom_ids(view) == [f"zb:basket:{record.link_id}", f"zb:revoke:{record.link_id}"]
    assert "Basket" not in text(view)  # no basket yet

    thumbs = gallery(view)
    assert len(thumbs) == 5  # images and the video's first frame
    assert all(t["media"]["url"].startswith("https://media.discordapp.net/") for t in thumbs)
    assert "format=webp" in thumbs[-1]["media"]["url"]  # clip.mp4
    assert not any("📎" in t for t in texts(view))  # every file is in the gallery


def test_download_reply_no_media(state: Any) -> None:
    payload = resolved_message(fixture("interaction_context_menu_no_media.json"))
    assert handlers.download_reply(state, 7, payload, "k") == "This message has no images or videos."


@pytest.mark.parametrize(
    ("media_types", "notice"),
    [
        ({"image", "video"}, "That message has no images or videos."),
        ({"video"}, "That message has no videos."),
        ({"image", "video", "audio"}, "That message has no images, videos or audio files."),
        ({"all"}, "That message has nothing to download."),
        ({"all", "image"}, "That message has nothing to download."),
    ],
)
def test_nothing_to_download_follows_media_types(
    settings: Settings, media_types: set[str], notice: str
) -> None:
    s = settings.model_copy(update={"media_types": frozenset(media_types)})
    assert views.nothing_to_download(s) == notice


def test_download_reply_respects_media_types(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"media_types": frozenset({"video"})})
    view = panel(handlers.download_reply(state, 7, media_payload(), "k"))
    assert texts(view)[0].startswith("📦 **1 file** · ")


def test_download_reply_takes_any_file_with_all(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"media_types": frozenset({"all"})})
    view = panel(handlers.download_reply(state, 7, fixture("message_any_files.json"), "k"))
    assert texts(view)[0].startswith("📦 **6 files** · ")
    assert len(gallery(view)) == 1  # photo.png
    assert "-# 📎 report.pdf · notes.txt · photos.zip · Makefile · backup.tar.gz" in texts(view)


def test_download_reply_images_and_videos_by_default(state: Any) -> None:
    view = panel(handlers.download_reply(state, 7, fixture("message_any_files.json"), "k"))
    assert texts(view)[0].startswith("📦 **1 file** · ")  # just photo.png
    no_media = {"attachments": fixture("message_any_files.json")["attachments"][:5]}
    assert handlers.download_reply(state, 7, no_media, "k") == "This message has no images or videos."


def test_download_reply_truncates(state: Any) -> None:
    state.settings = state.settings.model_copy(update={"max_files_per_link": 2})
    state.store.max_files_per_link = 2
    view = panel(handlers.download_reply(state, 7, media_payload(), "k"))
    assert "**2 files**" in texts(view)[0]
    assert "Only the first 2 of 5 files" in texts(view)[0]


def test_download_reply_link_error(state: Any) -> None:
    state.store.clock = lambda: EX  # CDN URLs about to expire
    reply = handlers.download_reply(state, 7, media_payload(), "k")
    assert isinstance(reply, str) and reply.startswith("Couldn't create a link")


def test_panel_stays_within_discord_limits(state: Any) -> None:
    state.store.add_to_basket(7, "b/1", [item("x.jpg")])
    many = [item(f"p{i}.jpg") for i in range(60)] + [
        item(f"d{i}.pdf", ctype="application/pdf", proxy_url=None) for i in range(40)
    ]
    view = panel(handlers.link_reply(state, 7, many, "k"))
    components = list(flat(view))
    assert len(components) <= 40
    rows = [c for c in components if c["type"] == 1]
    assert rows and all(len(r["components"]) <= 5 for r in rows)
    assert len(gallery(view)) == 10
    assert all(len(b["url"]) <= 512 for b in link_buttons(view))
    assert view.content_length() <= 4000
    hidden, names = next(t for t in texts(view) if "📎" in t).split("\n")
    assert hidden == "-# 🖼️ +50 more images and videos"  # beyond the gallery's 10
    assert names.endswith(" · +35 more") and names.count(" · ") == 5


def test_gallery_only_shows_allowed_thumbnails(state: Any) -> None:
    items = [
        item("a.jpg"),
        item("b.jpg", proxy_url=None),  # no proxy: no preview, listed by name instead
        item("song.mp3", ctype="audio/mpeg"),
        item("SPOILER_c.jpg"),
    ]
    view = panel(handlers.link_reply(state, 7, items, "k"))
    thumbs = gallery(view)
    assert [t.get("spoiler", False) for t in thumbs] == [False, True]  # spoilers stay blurred
    assert "-# 📎 b.jpg · song.mp3" in texts(view)


def test_gallery_leaves_out_formats_discord_cannot_render(state: Any) -> None:
    items = [
        item("logo.svg", ctype="image/svg+xml"),  # a broken tile on Android (seen live)
        item("photo.png", ctype="image/png"),
        item("icon.ico", ctype="image/x-icon"),
        item("clip.mov", ctype="video/quicktime"),
        item("scan.tiff", ctype="image/tiff"),
    ]
    view = panel(handlers.link_reply(state, 7, items, "k"))
    assert len(gallery(view)) == 2
    assert all("photo.png" in t["media"]["url"] or "clip.mov" in t["media"]["url"] for t in gallery(view))
    assert "-# 📎 logo.svg · icon.ico · scan.tiff" in texts(view)


@pytest.mark.parametrize(
    ("name", "ctype", "embed", "shown"),
    [
        *((f"a{ext}", None, False, True) for ext in sorted(views.GALLERY_EXTS)),
        *((f"A{ext.upper()}", None, False, True) for ext in sorted(views.GALLERY_EXTS)),
        *(("upload", ctype, False, True) for ctype in sorted(views.GALLERY_TYPES)),
        ("a.bin", "image/png", False, False),  # application/octet-stream by its extension
        ("a.jpg", "image/JPEG; charset=binary", False, True),
        ("photo.jpg_large", "image/jpeg", False, True),  # unknown extension: the type decides
        ("logo.svg", "image/svg+xml", False, False),
        ("logo.svg", None, False, False),
        ("logo.svg", "image/png", False, False),  # a known extension outside the list wins
        ("logo.png", "image/svg+xml", False, False),
        ("icon.ico", "image/vnd.microsoft.icon", False, False),
        ("scan.tif", None, False, False),
        ("scan.tiff", "image/tiff", False, False),
        ("img.heic", "image/heic", False, False),
        ("img.heic", None, False, False),
        ("pic.bmp", "image/bmp", False, False),
        ("clip.mkv", "video/x-matroska", False, False),
        ("clip.avi", None, False, False),
        ("embed-1", None, True, True),  # an untyped embed: Discord's proxy made the preview
        ("embed-1", None, False, False),
        ("pic.svg", None, True, False),
    ],
)
def test_in_gallery(name: str, ctype: str | None, embed: bool, shown: bool) -> None:
    kw: dict[str, Any] = {"fallback_kind": "image"} if embed else {}
    assert views.in_gallery(item(name, ctype=ctype, **kw)) is shown


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("plain.jpg", "plain.jpg"),
        ("01/in_folder.jpg", "in\\_folder.jpg"),
        (
            "**bold** ~~x~~ ||spoiler|| `code`",
            "\\*\\*bold\\*\\* \\~\\~x\\~\\~ \\|\\|spoiler\\|\\| \\`code\\`",
        ),
        ("a\nb\r\nc\td", "a b c d"),
        ("@everyone", "@​everyone"),
        ("<@123456789012345678>", "<@​123456789012345678>"),
        ("[click](x)", "\\[click](x)"),  # a masked link
        ("evil‮gpj.exe", "evilgpj.exe"),  # bidi override dropped
        ("x" * 60, "x" * 39 + "…"),
    ],
)
def test_safe_name(name: str, shown: str) -> None:
    assert safe_name(name) == shown


def test_hostile_filenames_in_panel(state: Any) -> None:
    hostile = "# @here **x** [y](z) <@&123456789012345678>\n-# hi.txt"
    view = panel(handlers.link_reply(state, 7, [item(hostile, ctype="text/plain", proxy_url=None)], "k"))
    names = next(t for t in texts(view) if t.startswith("-# 📎"))
    assert "\n" not in names
    assert "@here" not in names and "<@&1" not in names
    assert "**x**" not in names


@pytest.mark.parametrize(
    ("n", "text"), [(10, "1 KB"), (61_000, "60 KB"), (5 * 1024**2, "5.0 MB"), (3 * 1024**3, "3.00 GB")]
)
def test_format_size(n: int, text: str) -> None:
    assert format_size(n) == text


# --- basket --------------------------------------------------------------------------


def test_basket_handlers(state: Any) -> None:
    assert handlers.basket_show_reply(state, 7) == BASKET_EMPTY
    view = panel(handlers.add_to_basket_reply(state, 7, media_payload(), "c/1"))
    assert texts(view)[0] == "✓ Added 5 files to your basket."
    assert texts(view)[1].startswith("🧺 **Basket: 5 files from 1 message** · 51 KB · link expires <t:")
    assert [b["label"] for b in link_buttons(view)] == ["Download all", "Basket ZIP"]
    assert custom_ids(view) == ["zb:basketclear"]
    first_token = token_of(main_urls(view)[0])

    view = panel(handlers.add_to_basket_reply(state, 7, fixture("message_image_embed.json"), "c/2"))
    assert "6 files from 2 messages" in text(view)
    assert state.store.resolve(first_token) is None  # one live basket link per user
    record = state.store.resolve(token_of(main_urls(view)[1]))
    assert record and record.names[0] == "01/test-1.jpg" and record.names[-1].startswith("02/")
    assert len(gallery(view)) == 6

    view = panel(handlers.add_to_basket_reply(state, 7, media_payload(), "c/1"))
    assert texts(view)[0] == "That message is already in your basket."
    assert state.store.active_links(7) == 1

    cleared = panel(handlers.basket_clear_reply(state, 7))
    assert texts(cleared) == [BASKET_CLEARED]
    assert state.store.active_links(7) == 0  # clearing revokes the basket link
    assert handlers.basket_clear_reply(state, 7) == "🧺 Your basket is already empty."
    assert handlers.basket_show_reply(state, 7) == BASKET_EMPTY


def test_add_no_media_to_basket(state: Any) -> None:
    payload = resolved_message(fixture("interaction_context_menu_no_media.json"))
    reply = handlers.add_to_basket_reply(state, 7, payload, "k")
    assert reply == f"That message has no images or videos.\n{BASKET_EMPTY}"


def test_basket_link_error(state: Any) -> None:
    state.store.clock = lambda: EX - 300  # basket fresh, but CDN URLs expire within the safety margin
    view = panel(handlers.add_to_basket_reply(state, 7, media_payload(), "k"))
    assert "Couldn't create a link" in text(view)
    assert link_buttons(view) == [] and custom_ids(view) == ["zb:basketclear"]


def test_link_panel_shows_the_basket(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    view, record = link_of(state)
    assert "🧺 Basket: 5 files from 1 message" in texts(view)
    second_row = [c for c in flat(view) if c["type"] == 1][1]["components"]
    labels = [b["label"] for b in second_row]
    assert labels == ["Add to basket", "Basket ZIP", "Clear basket", "Revoke"]
    basket_url = basket_zip_url(view)
    assert basket_url and basket_url.endswith(".zip")
    basket_record = state.store.resolve(token_of(basket_url))
    assert basket_record is not None and basket_record is not record and len(basket_record.items) == 5
    link_of(state)  # the next panel re-mints the basket link and revokes this one
    assert state.store.resolve(token_of(basket_url)) is None


# --- buttons -------------------------------------------------------------------------


async def test_revoke_button_edits_panel_in_place(state: Any) -> None:
    view, record = link_of(state)
    stranger = interaction(state, user_id=8, message=message_of(view))
    await RevokeButton(record.link_id).callback(stranger)
    assert texts(stranger.response.edit_message.call_args.kwargs["view"]) == [LINK_GONE]
    assert state.store.resolve(token_of(main_urls(view)[0]))

    owner = interaction(state, message=message_of(view))
    await RevokeButton(record.link_id).callback(owner)
    edited = owner.response.edit_message.call_args.kwargs["view"]
    assert texts(edited) == [REVOKED]
    assert "can't be opened again" in REVOKED and "no longer works" not in REVOKED  # v0 #4
    assert link_buttons(edited) == [] and custom_ids(edited) == []
    assert state.store.resolve(token_of(main_urls(view)[0])) is None
    owner.response.send_message.assert_not_awaited()
    assert len(state.panels) == 2  # each click now owns the panel (fresh token)


async def test_add_link_to_basket_button(state: Any) -> None:
    view, record = link_of(state)
    inter = interaction(state, message=message_of(view))
    await AddLinkToBasketButton(record.link_id).callback(inter)
    edited = inter.response.edit_message.call_args.kwargs["view"]
    assert texts(edited)[0] == "✓ Added 2 files to your basket."
    assert main_urls(edited) == main_urls(view)  # the same link, taken back from the message
    assert "🧺 Basket: 2 files from 1 message" in texts(edited)
    assert basket_zip_url(edited)
    basket = state.store.basket(7)
    assert basket and basket.entries[0].message_key == "c/m"

    again = interaction(state, message=message_of(edited))
    await AddLinkToBasketButton(record.link_id).callback(again)
    assert texts(again.response.edit_message.call_args.kwargs["view"])[0] == (
        "That message is already in your basket."
    )

    other = interaction(state, user_id=8, message=message_of(view))
    await AddLinkToBasketButton(record.link_id).callback(other)
    other.response.send_message.assert_awaited_once_with(LINK_EXPIRED, ephemeral=True)
    other.response.edit_message.assert_not_awaited()


async def test_add_button_without_the_panel_link_shows_the_basket(state: Any) -> None:
    _, record = link_of(state)
    bare = SimpleNamespace(id=1, flags=SimpleNamespace(components_v2=True, ephemeral=True), components=[])
    inter = interaction(state, message=bare)  # the link buttons' URLs can't be recovered
    await AddLinkToBasketButton(record.link_id).callback(inter)
    edited = inter.response.edit_message.call_args.kwargs["view"]
    assert texts(edited)[1].startswith("🧺 **Basket: 2 files from 1 message**")


async def test_clear_button(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    view, _ = link_of(state)
    inter = interaction(state, message=message_of(view))
    await BasketClearButton().callback(inter)
    edited = inter.response.edit_message.call_args.kwargs["view"]
    assert texts(edited)[0] == BASKET_CLEARED and main_urls(edited) == main_urls(view)
    assert "Basket:" not in text(edited) and basket_zip_url(edited) is None
    assert state.store.basket(7) is None

    handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    basket_view = panel(handlers.basket_show_reply(state, 7))
    inter = interaction(state, message=message_of(basket_view))
    await BasketClearButton().callback(inter)
    assert texts(inter.response.edit_message.call_args.kwargs["view"]) == [BASKET_CLEARED]


async def test_legacy_basket_link_button(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "k")
    legacy = SimpleNamespace(id=1, flags=SimpleNamespace(components_v2=False, ephemeral=True), components=[])
    inter = interaction(state, message=legacy)
    await BasketLinkButton().callback(inter)
    # A v0.0.0 reply can't turn into a panel: the panel is sent as a new reply instead.
    inter.response.edit_message.assert_not_awaited()
    kwargs = inter.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and "5 files from 1 message" in text(kwargs["view"])


async def test_dm_panel_is_edited_but_not_tracked(state: Any) -> None:
    view, record = link_of(state)
    inter = interaction(state, message=message_of(view, ephemeral=False))
    await RevokeButton(record.link_id).callback(inter)
    inter.response.edit_message.assert_awaited_once()
    assert len(state.panels) == 0  # an ordinary DM message is never deleted


async def test_button_cooldown(state: Any) -> None:
    state.button_cooldown = UserCooldown(rate=2, per=60, cap=2)
    button = BasketClearButton()
    first = interaction(state)
    assert await button.interaction_check(first) and await button.interaction_check(first)
    assert not await button.interaction_check(first)
    text = first.response.send_message.call_args.args[0]
    assert text.startswith("You're going a bit fast — try again in ")
    assert await AddLinkToBasketButton("abcdef").interaction_check(interaction(state, user_id=8))
    assert await RevokeButton("abcdef").interaction_check(interaction(state, user_id=9))
    assert len(state.button_cooldown._buckets) == 2  # bounded


async def test_dynamic_items_parse_custom_ids() -> None:
    match = re.fullmatch(RevokeButton.__discord_ui_compiled_template__, "zb:revoke:abcDEF_-12")
    assert match
    item = await RevokeButton.from_custom_id(cast(Any, None), cast(Any, None), match)
    assert item.link_id == "abcDEF_-12"
    assert not re.fullmatch(RevokeButton.__discord_ui_compiled_template__, "zb:revoke:../../x")


def test_every_rendered_button_routes_to_a_dynamic_item(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    view, _ = link_of(state)
    ids = custom_ids(view) + custom_ids(panel(handlers.basket_show_reply(state, 7)))
    assert len(ids) == 4
    for cid in ids:
        assert sum(bool(d.__discord_ui_compiled_template__.fullmatch(cid)) for d in DYNAMIC_ITEMS) == 1


async def test_click_routes_through_discord_py(settings: Settings, state: Any) -> None:
    """discord.py rebuilds the clicked panel from the message, swaps in our DynamicItem,
    runs interaction_check (cooldown), then the callback."""
    bot = ZipBot(settings, state.store)
    view, record = link_of(state)
    message = message_of(view)
    custom_id = f"zb:revoke:{record.link_id}"
    inter = interaction(bot, data={"custom_id": custom_id, "component_type": 2}, message=message)
    match = RevokeButton.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert match
    store = bot._connection._view_store
    await store.schedule_dynamic_item_call(2, RevokeButton, inter, custom_id, match)
    assert texts(inter.response.edit_message.call_args.kwargs["view"]) == [REVOKED]
    await bot.close()


# --- the live panel ------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def drain() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_new_panel_deletes_the_previous_one(state: Any) -> None:
    clock = Clock()
    panels = Panels(clock=clock)
    first, second, third = interaction(state), interaction(state), interaction(state)
    panels.opened(7, first, 1, panels.ticket())
    clock.t += PANEL_TOKEN_SEC - 1
    panels.opened(7, second, 2, panels.ticket())
    await drain()
    first.delete_original_response.assert_awaited_once()
    clock.t += PANEL_TOKEN_SEC  # too old: its token can't delete any more
    panels.opened(7, third, 3, panels.ticket())
    await drain()
    second.delete_original_response.assert_not_awaited()
    assert len(panels) == 1


async def test_click_on_the_live_panel_refreshes_it(state: Any) -> None:
    panels = Panels()
    command, click = interaction(state), interaction(state)
    panels.opened(7, command, 5, panels.ticket())
    panels.clicked(7, click, 5, panels.ticket())  # the button edited message 5 in place
    later = interaction(state)
    panels.opened(7, later, 6, panels.ticket())
    await drain()
    command.delete_original_response.assert_not_awaited()
    click.delete_original_response.assert_awaited_once()  # deleted with the fresh token


async def test_click_on_an_older_panel_keeps_the_newer_one(state: Any) -> None:
    panels = Panels()
    newer = interaction(state)
    panels.opened(7, newer, 2, panels.ticket())
    stale_click = interaction(state)
    panels.clicked(7, stale_click, 1, panels.ticket())  # message 1: >14 min old, or from before a restart
    panels.opened(7, interaction(state), 3, panels.ticket())
    await drain()
    newer.delete_original_response.assert_awaited_once()  # the live one, not message 1
    stale_click.delete_original_response.assert_not_awaited()
    # With nothing live, a click on any panel takes over.
    lone = Panels()
    lone.clicked(8, stale_click, 1, lone.ticket())
    assert len(lone) == 1


async def test_click_on_a_panel_being_deleted_is_ignored(state: Any) -> None:
    panels = Panels()
    a, b = interaction(state), interaction(state)
    panels.opened(7, a, 1, panels.ticket())
    panels.opened(7, b, 2, panels.ticket())  # A's delete is now in flight
    panels.clicked(7, interaction(state), 1, panels.ticket())  # the user clicks A before it lands
    await drain()
    b.delete_original_response.assert_not_awaited()  # B survives
    a.delete_original_response.assert_awaited_once()


async def test_out_of_order_replies_keep_the_newest(state: Any) -> None:
    panels = Panels()
    older, newer = interaction(state), interaction(state)
    t_old, t_new = panels.ticket(), panels.ticket()  # taken before each send
    panels.opened(7, newer, 2, t_new)  # the newer reply's response came back first
    panels.opened(7, older, 1, t_old)
    await drain()
    older.delete_original_response.assert_awaited_once()
    newer.delete_original_response.assert_not_awaited()
    late_click = interaction(state)
    panels.clicked(7, late_click, 2, t_old)  # a click whose edit started before the newer reply
    panels.opened(7, interaction(state), 3, panels.ticket())
    await drain()
    newer.delete_original_response.assert_awaited_once()
    late_click.delete_original_response.assert_not_awaited()


async def test_delete_failure_is_swallowed(state: Any) -> None:
    panels = Panels()
    old = interaction(state)
    response = cast(Any, SimpleNamespace(status=404, reason="Not Found"))
    old.delete_original_response.side_effect = discord.NotFound(response, "Unknown Message")
    panels.opened(7, old, 1, panels.ticket())
    panels.opened(7, interaction(state), 2, panels.ticket())
    await drain()
    old.delete_original_response.assert_awaited_once()
    assert not panels._tasks  # finished and released


async def test_registry_is_bounded(state: Any) -> None:
    clock = Clock()
    panels = Panels(clock=clock, cap=3)
    for user in range(5):
        panels.opened(user, interaction(state), user, panels.ticket())
        panels.opened(user, interaction(state), 100 + user, panels.ticket())
    assert len(panels) == 3 and len(panels._doomed) == 5  # doomed is capped at 2 x cap = 6
    panels.opened(5, interaction(state), 6, panels.ticket())
    for n in range(7, 12):
        panels.opened(5, interaction(state), n, panels.ticket())
    assert len(panels._doomed) == 6
    clock.t += PANEL_TOKEN_SEC
    panels.opened(99, interaction(state), 99, panels.ticket())
    assert len(panels) == 1  # stale entries are dropped on access
    await drain()


async def test_button_on_an_old_panel_edits_it_without_taking_over(state: Any) -> None:
    old_view, old_record = link_of(state)  # e.g. still visible from before a restart
    live = interaction(state)
    await views.show(live, panel(link_of(state)[0]))
    click = interaction(state, message=message_of(old_view))
    await RevokeButton(old_record.link_id).callback(click)
    await drain()
    click.response.edit_message.assert_awaited_once()
    live.delete_original_response.assert_not_awaited()  # the newer panel keeps its URLs
    nxt = interaction(state)
    await views.show(nxt, panel(link_of(state)[0]))
    await drain()
    live.delete_original_response.assert_awaited_once()  # it is still the live one
    click.delete_original_response.assert_not_awaited()


async def test_revoke_keeps_the_shown_basket_link(state: Any) -> None:
    handlers.add_to_basket_reply(state, 7, media_payload(), "c/1")
    view, record = link_of(state)
    shown = basket_zip_url(view)
    assert shown
    inter = interaction(state, message=message_of(view))
    await RevokeButton(record.link_id).callback(inter)
    edited = inter.response.edit_message.call_args.kwargs["view"]
    assert basket_zip_url(edited) == shown  # basket unchanged: Basket ZIP elsewhere keeps working
    assert state.store.resolve(token_of(shown))


async def test_reply_is_sent_before_the_old_panel_is_deleted(state: Any) -> None:
    order: list[str] = []
    old, new = interaction(state), interaction(state)
    old.delete_original_response.side_effect = lambda: order.append("delete")
    new.response.send_message.side_effect = lambda *a, **kw: order.append("send") or new.sent
    await views.show(old, panel(link_of(state)[0]))
    await views.show(new, panel(link_of(state)[0]))
    await drain()
    assert order == ["send", "delete"]


async def test_notices_leave_the_live_panel_alone(state: Any) -> None:
    live = interaction(state)
    await views.show(live, panel(link_of(state)[0]))
    notice = interaction(state)
    await views.show(notice, NO_MEDIA)
    await drain()
    notice.response.send_message.assert_awaited_once_with(NO_MEDIA, ephemeral=True)
    live.delete_original_response.assert_not_awaited()


# --- cog callbacks -------------------------------------------------------------------


async def test_context_menu_callbacks(settings: Settings, state: Any) -> None:
    from zipbot.cogs.context_menu import ContextMenus

    bot = ZipBot(settings, state.store)
    cog = ContextMenus(bot)
    raw = media_payload()
    msg = cast(Any, SimpleNamespace(id=int(raw["id"]), channel=SimpleNamespace(id=5)))
    inter = interaction(bot, data={"resolved": {"messages": {raw["id"]: raw}}})
    await cog.download(inter, msg)
    kwargs = inter.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and isinstance(kwargs["view"], discord.ui.LayoutView)
    assert inter.response.send_message.call_args.args == ()  # no `content` next to a V2 layout
    inter2 = interaction(bot, data={"resolved": {"messages": {raw["id"]: raw}}})
    await cog.add(inter2, msg)
    assert texts(inter2.response.send_message.call_args.kwargs["view"])[0].startswith("✓ Added 5 files")
    basket = state.store.basket(7)
    assert basket and basket.entries[0].message_key == f"5/{raw['id']}"
    await drain()
    inter.delete_original_response.assert_awaited_once()  # one live panel
    await bot.close()


async def test_basket_cog_callbacks(settings: Settings, state: Any) -> None:
    from zipbot.cogs.basket import BasketCog

    bot = ZipBot(settings, state.store)
    cog = BasketCog(bot)
    state.store.add_to_basket(7, "k", [item()])
    for name in ("show", "link", "clear"):
        inter = interaction(bot)
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
        (1, "name", "Download attachments"),  # the any-file name: Discord keeps it until we re-sync
        (0, "description", "Collect attachments from several messages"),  # ditto
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
