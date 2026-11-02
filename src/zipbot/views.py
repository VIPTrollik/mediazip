"""Reply panels and buttons (PLAN.md §3, "Design A": one live panel per user).

Every reply that carries a link is one Components V2 panel: a summary line, a strip of
thumbnails, the link buttons (`#<token>.files`, `#<token>.zip`; each opens the page and
starts that action right away) and a row for the basket. A user has one live ephemeral panel:
a new one deletes the previous one (`Panels`), and a button edits the panel it sits on.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import mimetypes
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol, cast

import discord
from discord import app_commands

from zipbot.config import Settings
from zipbot.contract import thumbnail
from zipbot.media import FILE_KIND, MediaItem, is_allowed_url
from zipbot.store import AddResult, Basket, LinkError, LinkRecord, Store

LINK_ID = r"(?P<link_id>[A-Za-z0-9_-]{6,32})"

GALLERY_MAX = 10  # Discord's limit per media gallery
# What Discord's media proxy renders as a gallery tile. Anything else (SVG: a broken tile on
# Android, seen live; .ico, .tiff, .heic...) is listed by name with the other files.
GALLERY_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/avif",
        "video/mp4",
        "video/webm",
        "video/quicktime",
    }
)
GALLERY_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".mp4", ".webm", ".mov"})
NAMES_MAX = 5  # other files are listed by name, then "+N more"
NAME_CHARS = 40
# Ids of a panel's own link buttons. A click re-renders the panel and takes their URLs back
# from the message, because the store keeps only token hashes. Far above Discord's auto ids.
ID_FILES, ID_ZIP, ID_BASKET = 901, 902, 903

PANEL_TOKEN_SEC = 14 * 60  # an interaction token can edit/delete its message for 15 min
MAX_PANELS = 1_000  # each entry holds an Interaction (a few KB of payload)

COOLDOWN_TEXT = "You're going a bit fast — try again in {:.0f} s."
REVOKED = (
    "🗑️ Link revoked — it can't be opened again. Pages already open keep working until "
    "Discord's file links expire."
)
LINK_GONE = "This link has already expired or was revoked."
LINK_EXPIRED = "This link has expired — run the command again."
BASKET_EMPTY = "🧺 Your basket is empty — use **Apps → Add to ZIP basket** on a message first."
BASKET_CLEARED = "🧺 Basket cleared."

# A panel, or a short notice that leaves the user's live panel alone.
Reply = discord.ui.LayoutView | str


class AppState(Protocol):
    store: Store
    settings: Settings
    panels: Panels
    button_cooldown: UserCooldown


def app(interaction: discord.Interaction) -> AppState:
    return cast(AppState, interaction.client)


def detached[V: discord.ui.view.BaseView](view: V) -> V:
    """Every button is handled globally via DynamicItem (or is a link), so the view need not
    be kept: a stopped view is never added to discord.py's view store (bounded memory)."""
    view.stop()
    return view


# --- the live panel --------------------------------------------------------------------


@dataclass(slots=True)
class _Panel:
    interaction: discord.Interaction
    message_id: int | None
    at: float
    ticket: int


class Panels:
    """user id → their live ephemeral panel, kept as the interaction that last touched it: its
    token can still delete the message for 15 minutes. In memory only; after a restart old
    panels just stay until the user dismisses them.

    Every reply takes a ticket before its (awaited) send or edit, so a reply that lands late
    never replaces a newer panel; panels being deleted ignore clicks."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, cap: int = MAX_PANELS) -> None:
        self.clock = clock
        self.cap = cap
        self._live: OrderedDict[int, _Panel] = OrderedDict()  # oldest first
        self._doomed: OrderedDict[int, None] = OrderedDict()  # message ids deleted or being deleted
        self._tickets = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()  # pending deletes (keep a reference)

    def ticket(self) -> int:
        return next(self._tickets)

    def opened(
        self, user_id: int, interaction: discord.Interaction, message_id: int | None, ticket: int
    ) -> None:
        """A new panel was sent: it replaces the live one, which is deleted in the background.
        If a newer panel landed first, this one lost and is deleted instead."""
        live = self._get(user_id)
        if live is not None and live.ticket > ticket:
            self._delete(interaction, message_id)
            return
        self._set(user_id, _Panel(interaction, message_id, self.clock(), ticket))
        if live is not None and live.message_id != message_id:
            self._delete(live.interaction, live.message_id)

    def clicked(self, user_id: int, interaction: discord.Interaction, message_id: int, ticket: int) -> None:
        """A button edited `message_id` in place. Only the live panel (or any panel, if none is
        live) takes the fresh token: a click on an older panel never deletes the newer one."""
        if message_id in self._doomed:
            return
        live = self._get(user_id)
        if live is not None and (live.message_id != message_id or live.ticket > ticket):
            return
        self._set(user_id, _Panel(interaction, message_id, self.clock(), ticket))

    def _get(self, user_id: int) -> _Panel | None:
        now = self.clock()
        while self._live and next(iter(self._live.values())).at + PANEL_TOKEN_SEC <= now:
            self._live.popitem(last=False)  # token too old to delete with: forget it
        return self._live.get(user_id)

    def _set(self, user_id: int, panel: _Panel) -> None:
        self._live.pop(user_id, None)
        self._live[user_id] = panel
        if len(self._live) > self.cap:
            self._live.popitem(last=False)

    def _delete(self, interaction: discord.Interaction, message_id: int | None) -> None:
        if message_id is not None:
            self._doomed[message_id] = None
            if len(self._doomed) > 2 * self.cap:
                self._doomed.popitem(last=False)
        task = asyncio.create_task(self._delete_now(interaction))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _delete_now(interaction: discord.Interaction) -> None:
        with contextlib.suppress(discord.HTTPException):  # dismissed, already gone, token expired
            await interaction.delete_original_response()

    def __len__(self) -> int:
        return len(self._live)


async def show(interaction: discord.Interaction, reply: Reply) -> None:
    """Answer with the user's new live panel (or a notice, which leaves the panel alone)."""
    if isinstance(reply, str):
        await interaction.response.send_message(reply, ephemeral=True)
        return
    panels = app(interaction).panels
    ticket = panels.ticket()
    response = await interaction.response.send_message(view=reply, ephemeral=True)
    message_id = getattr(response, "message_id", None)
    panels.opened(
        interaction.user.id, interaction, message_id if isinstance(message_id, int) else None, ticket
    )


async def update(interaction: discord.Interaction, reply: Reply) -> None:
    """Answer a button click by editing the panel the button sits on."""
    message = interaction.message
    if isinstance(reply, str) or message is None or not message.flags.components_v2:
        await show(interaction, reply)  # notices; v0.0.0 replies can't become a panel
        return
    panels = app(interaction).panels
    ticket = panels.ticket()
    await interaction.response.edit_message(view=reply)
    if message.flags.ephemeral:  # DM replies are ordinary messages and are never deleted
        panels.clicked(interaction.user.id, interaction, message.id, ticket)


class UserCooldown:
    """Per-user rate limit (bounded). Used for buttons, whose DynamicItem callbacks don't run
    app_commands checks while every click can make a link (security review v0 #3), and for DM
    bursts."""

    def __init__(self, rate: int = 10, per: float = 60, *, cap: int = 10_000) -> None:
        self.rate, self.per, self.cap = rate, per, cap
        self._buckets: OrderedDict[int, app_commands.Cooldown] = OrderedDict()  # least recent first

    def retry_after(self, user_id: int) -> float | None:
        bucket = self._buckets.pop(user_id, None) or app_commands.Cooldown(self.rate, self.per)
        self._buckets[user_id] = bucket
        if len(self._buckets) > self.cap:
            self._buckets.popitem(last=False)
        return bucket.update_rate_limit()


class RateLimited:
    """Mixin for the DynamicItems: discord.py runs interaction_check before the callback."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        retry = app(interaction).button_cooldown.retry_after(interaction.user.id)
        if retry is None:
            return True
        await interaction.response.send_message(COOLDOWN_TEXT.format(retry), ephemeral=True)
        return False


# --- text ------------------------------------------------------------------------------


def format_size(n: int) -> str:
    if n < 1024**2:
        return f"{max(1, round(n / 1024))} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def link_summary(record: LinkRecord, *, truncated_from: int | None = None, head: str | None = None) -> str:
    exp = int(record.expires_at)
    parts = [head or f"📦 **{plural(len(record.items), 'file')}**"]
    if record.total_size:
        parts.append(format_size(record.total_size))
    text = " · ".join(parts) + f" · link expires <t:{exp}:R>"
    if truncated_from:
        text += f"\n-# Only the first {len(record.items)} of {truncated_from} files fit in one link."
    return text


def basket_line(basket: Basket) -> str:
    return f"🧺 Basket: {plural(basket.file_count, 'file')} from {plural(len(basket.entries), 'message')}"


def safe_name(name: str) -> str:
    """A file name (user-controlled) for panel text: one short line, no markdown or mentions."""
    name = " ".join(name.rsplit("/", 1)[-1].split())  # drop the ZIP folder; newlines → spaces
    name = "".join(c for c in name if c.isprintable())  # also bidi overrides, zero-width chars
    if len(name) > NAME_CHARS:
        name = name[: NAME_CHARS - 1] + "…"
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


# --- panel parts -----------------------------------------------------------------------


def page_url(base_url: str, token: str, action: str | None = None) -> str:
    return f"{base_url}/d#{token}" + (f".{action}" if action else "")


@dataclass(frozen=True, slots=True)
class PanelLink:
    """The link a panel is about, with its button URLs (the token itself is never kept)."""

    record: LinkRecord
    files_url: str
    zip_url: str

    @classmethod
    def new(cls, base_url: str, token: str, record: LinkRecord) -> PanelLink:
        return cls(record, page_url(base_url, token, "files"), page_url(base_url, token, "zip"))


def row(*items: discord.ui.Item[Any]) -> discord.ui.ActionRow[Any]:
    action_row: discord.ui.ActionRow[Any] = discord.ui.ActionRow()
    for item in items:
        action_row.add_item(item)
    return action_row


def link_row(
    link: PanelLink, *extra: discord.ui.Item[Any], zip_label: str = "Download ZIP"
) -> discord.ui.ActionRow[Any]:
    return row(
        discord.ui.Button(label="Download all", emoji="⬇️", url=link.files_url, id=ID_FILES),
        discord.ui.Button(label=zip_label, emoji="🗜️", url=link.zip_url, id=ID_ZIP),
        *extra,
    )


def in_gallery(item: MediaItem) -> bool:
    """Whether the item's format renders as a gallery tile (GALLERY_TYPES / GALLERY_EXTS).

    A known extension outside the list wins over the content type (an `.svg` sent as
    `image/png` is still SVG). Without either, only an embed counts: Discord's proxy made it.
    """
    ctype = (item.content_type or "").partition(";")[0].strip().lower()
    ext = PurePosixPath(item.filename).suffix.lower()
    if ext and ext not in GALLERY_EXTS and mimetypes.guess_type(f"x{ext}")[0]:
        return False
    if ctype:
        return ctype in GALLERY_TYPES
    return ext in GALLERY_EXTS or (not ext and item.fallback_kind != FILE_KIND)


def previews(record: LinkRecord) -> list[discord.ui.Item[Any]]:
    """Thumbnails of the first 10 images/videos, then the names of the other files."""
    gallery: list[discord.MediaGalleryItem] = []
    hidden = 0  # media beyond the gallery's 10
    others: list[str] = []
    for item, name in zip(record.items, record.names, strict=True):
        thumb = thumbnail(item)
        if thumb is None or not is_allowed_url(thumb) or not in_gallery(item):
            others.append(name)
        elif len(gallery) < GALLERY_MAX:
            # Keep spoilered attachments blurred, as Discord shows them in the original message.
            gallery.append(discord.MediaGalleryItem(thumb, spoiler=item.filename.startswith("SPOILER_")))
        else:
            hidden += 1
    parts: list[discord.ui.Item[Any]] = []
    if gallery:
        parts.append(discord.ui.MediaGallery(*gallery))
    lines = [f"-# 🖼️ +{plural(hidden, 'more image or video', 'more images and videos')}"] if hidden else []
    if others:
        names = " · ".join(safe_name(n) for n in others[:NAMES_MAX])
        more = f" · +{len(others) - NAMES_MAX} more" if len(others) > NAMES_MAX else ""
        lines.append(f"-# 📎 {names}{more}")
    if lines:
        parts.append(discord.ui.TextDisplay("\n".join(lines)))
    return parts


def layout(*parts: discord.ui.Item[Any] | str | None) -> discord.ui.LayoutView:
    """One container holding `parts`; strings become text, None is skipped."""
    container: discord.ui.Container[Any] = discord.ui.Container()
    for part in parts:
        if part is not None:
            container.add_item(discord.ui.TextDisplay(part) if isinstance(part, str) else part)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return detached(view)


def basket_parts(
    state: AppState, user_id: int, link_id: str | None, basket_token: str | None = None
) -> list[discord.ui.Item[Any]]:
    """Below a link: the basket line and one row: Add to basket, Basket ZIP, Clear, Revoke."""
    basket = state.store.basket(user_id)
    parts: list[discord.ui.Item[Any]] = [discord.ui.Separator()]
    buttons: list[discord.ui.Item[Any]] = [AddLinkToBasketButton(link_id)] if link_id else []
    if basket is not None and basket.entries:
        parts.append(discord.ui.TextDisplay(basket_line(basket)))
        with contextlib.suppress(LinkError):  # e.g. a basket file's CDN URL expires soon
            # An in-place edit keeps the link it shows while the basket is unchanged; a new panel
            # can't recover a token (only hashes are stored), so it gets a new one and older
            # panels' Basket ZIP stops working.
            token, _ = state.store.basket_link(user_id, basket_token)
            url = page_url(state.settings.public_base_url, token, "zip")
            buttons.append(discord.ui.Button(label="Basket ZIP", emoji="🧺", url=url, id=ID_BASKET))
        buttons.append(BasketClearButton())
    if link_id:
        buttons.append(RevokeButton(link_id))
    if buttons:
        parts.append(row(*buttons))
    return parts if len(parts) > 1 else []


def link_panel(
    state: AppState,
    user_id: int,
    link: PanelLink | None,
    *,
    note: str | None = None,
    truncated_from: int | None = None,
    basket_token: str | None = None,
) -> discord.ui.LayoutView:
    """A link's panel. Without a link (revoked), only the note and the basket part remain."""
    if link is None:
        return layout(note, *basket_parts(state, user_id, None, basket_token))
    record = link.record
    return layout(
        note,
        link_summary(record, truncated_from=truncated_from),
        *previews(record),
        link_row(link),
        *basket_parts(state, user_id, record.link_id, basket_token),
    )


def basket_panel(state: AppState, user_id: int, note: str | None = None) -> Reply:
    """The basket's own panel, with a fresh basket link (replacing the previous one)."""
    basket = state.store.basket(user_id)
    if basket is None or not basket.entries:
        return "\n".join(t for t in (note, BASKET_EMPTY) if t)
    head = f"🧺 **Basket: {plural(basket.file_count, 'file')} from {plural(len(basket.entries), 'message')}**"
    try:
        token, record = state.store.basket_link(user_id)
    except LinkError as exc:
        clear = row(BasketClearButton())
        return layout(note, f"{head}\nCouldn't create a link: {exc}.", clear)
    link = PanelLink.new(state.settings.public_base_url, token, record)
    return layout(
        note,
        link_summary(record, head=head),
        *previews(record),
        link_row(link, BasketClearButton(), zip_label="Basket ZIP"),
    )


def walk(components: Iterable[Any]) -> Iterator[Any]:
    for c in components:
        yield c
        yield from walk(getattr(c, "children", ()))
        if (accessory := getattr(c, "accessory", None)) is not None:
            yield accessory


def message_urls(interaction: discord.Interaction) -> dict[int, str]:
    """Our link buttons on the clicked message, by component id."""
    components = interaction.message.components if interaction.message else []
    return {c.id: c.url for c in walk(components) if getattr(c, "id", None) in (ID_FILES, ID_ZIP, ID_BASKET)}


def shown_basket_token(interaction: discord.Interaction) -> str | None:
    url = message_urls(interaction).get(ID_BASKET)
    return url.split("#", 1)[1].removesuffix(".zip") if url and "#" in url else None


def panel_link(interaction: discord.Interaction) -> PanelLink | None:
    """The live link of the panel a button sits on: the record via the Revoke button's link id,
    the URLs from the link buttons."""
    if interaction.message is None:
        return None
    link_id = None
    for c in walk(interaction.message.components):
        if m := RevokeButton.__discord_ui_compiled_template__.fullmatch(getattr(c, "custom_id", None) or ""):
            link_id = m["link_id"]
    urls = message_urls(interaction)
    record = app(interaction).store.link_by_id(link_id, interaction.user.id) if link_id else None
    if record is None or ID_FILES not in urls or ID_ZIP not in urls:
        return None
    return PanelLink(record, urls[ID_FILES], urls[ID_ZIP])


# --- buttons ---------------------------------------------------------------------------

BASKET_RESULT_TEXT = {
    AddResult.DUPLICATE: "That message is already in your basket.",
    AddResult.FULL_MESSAGES: "Your basket is full (message limit). Download or clear it first.",
    AddResult.FULL_FILES: "That would exceed the file limit for one link. Download or clear it first.",
}
MEDIA_WORDS = {"image": "images", "video": "videos", "audio": "audio files"}


def nothing_to_download(settings: Settings, this: str = "That") -> str:
    """The empty-message notice, worded after MEDIA_TYPES ("no images or videos" by default)."""
    if settings.all_media:
        return f"{this} message has nothing to download."
    words = [w for kind, w in MEDIA_WORDS.items() if kind in settings.media_types]
    listed = " or ".join(filter(None, [", ".join(words[:-1]), words[-1]]))
    return f"{this} message has no {listed}."


def basket_note(settings: Settings, result: AddResult, added: int) -> str:
    if result is AddResult.ADDED:
        return f"✓ Added {plural(added, 'file')} to your basket."
    if result is AddResult.EMPTY:
        return nothing_to_download(settings)
    return BASKET_RESULT_TEXT[result]


class RevokeButton(
    RateLimited, discord.ui.DynamicItem[discord.ui.Button[Any]], template=rf"zb:revoke:{LINK_ID}"
):
    def __init__(self, link_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Revoke",
                emoji="🗑️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"zb:revoke:{link_id}",
            )
        )
        self.link_id = link_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> RevokeButton:
        return cls(match["link_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        state = app(interaction)
        revoked = state.store.revoke(self.link_id, interaction.user.id)
        note = REVOKED if revoked else LINK_GONE
        reply = link_panel(
            state, interaction.user.id, None, note=note, basket_token=shown_basket_token(interaction)
        )
        await update(interaction, reply)


class AddLinkToBasketButton(
    RateLimited, discord.ui.DynamicItem[discord.ui.Button[Any]], template=rf"zb:basket:{LINK_ID}"
):
    def __init__(self, link_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Add to basket",
                emoji="➕",
                style=discord.ButtonStyle.secondary,
                custom_id=f"zb:basket:{link_id}",
            )
        )
        self.link_id = link_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> AddLinkToBasketButton:
        return cls(match["link_id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        state = app(interaction)
        user_id = interaction.user.id
        record = state.store.link_by_id(self.link_id, user_id)
        if record is None:
            await update(interaction, LINK_EXPIRED)
            return
        result, _ = state.store.add_to_basket(user_id, record.message_key or record.link_id, record.items)
        note = basket_note(state.settings, result, len(record.items))
        link = panel_link(interaction)
        reply = link_panel(state, user_id, link, note=note) if link else basket_panel(state, user_id, note)
        await update(interaction, reply)


class BasketLinkButton(
    RateLimited, discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"zb:basketlink"
):
    """v0.0.0 replies carry this button; new panels link to the basket directly."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Get download link",
                emoji="⬇️",
                style=discord.ButtonStyle.primary,
                custom_id="zb:basketlink",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> BasketLinkButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await update(interaction, basket_panel(app(interaction), interaction.user.id))


class BasketClearButton(
    RateLimited, discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"zb:basketclear"
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Clear basket",
                emoji="🧹",
                style=discord.ButtonStyle.secondary,
                custom_id="zb:basketclear",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> BasketClearButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        state = app(interaction)
        state.store.clear_basket(interaction.user.id)
        link = panel_link(interaction)
        if link:
            reply = link_panel(state, interaction.user.id, link, note=BASKET_CLEARED)
        else:
            reply = layout(BASKET_CLEARED)
        await update(interaction, reply)


DYNAMIC_ITEMS = (RevokeButton, AddLinkToBasketButton, BasketLinkButton, BasketClearButton)
