"""Reply texts and components (PLAN.md §3).

Every reply that carries a link gets the same row of link buttons; each one opens the page
and starts that action right away (`#<token>.files`, `#<token>.zip`, or just the page).
"""

from __future__ import annotations

import re
from typing import Any, Protocol, cast

import discord

from zipbot.config import Settings
from zipbot.store import AddResult, Basket, LinkError, LinkRecord, Store

LINK_ID = r"(?P<link_id>[A-Za-z0-9_-]{6,32})"


class AppState(Protocol):
    store: Store
    settings: Settings


def app(interaction: discord.Interaction) -> AppState:
    return cast(AppState, interaction.client)


def detached(view: discord.ui.View) -> discord.ui.View:
    """Every button is handled globally via DynamicItem (or is a link), so the view need not
    be kept: a stopped view is never added to discord.py's view store (bounded memory)."""
    view.stop()
    return view


async def send_ephemeral(
    interaction: discord.Interaction, content: str, view: discord.ui.View | None
) -> None:
    if view is None:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, view=view, ephemeral=True)


def format_size(n: int) -> str:
    if n < 1024**2:
        return f"{max(1, round(n / 1024))} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def link_summary(record: LinkRecord, *, truncated_from: int | None = None) -> str:
    exp = int(record.expires_at)
    parts = [f"📦 **{plural(len(record.items), 'file')}**"]
    if record.total_size:
        parts.append(format_size(record.total_size))
    text = " · ".join(parts) + f" · link expires <t:{exp}:R>"
    if truncated_from:
        text += f"\n-# Only the first {len(record.items)} of {truncated_from} files fit in one link."
    return text


def page_url(base_url: str, token: str, action: str | None = None) -> str:
    return f"{base_url}/d#{token}" + (f".{action}" if action else "")


def link_buttons(base_url: str, token: str) -> list[discord.ui.Button[Any]]:
    return [
        discord.ui.Button(label="Save to phone", emoji="📱", url=page_url(base_url, token, "files")),
        discord.ui.Button(label="Download ZIP", emoji="⬇️", url=page_url(base_url, token, "zip")),
        discord.ui.Button(label="Open page", emoji="👁️", url=page_url(base_url, token)),
    ]


class RevokeButton(discord.ui.DynamicItem[discord.ui.Button[Any]], template=rf"zb:revoke:{LINK_ID}"):
    def __init__(self, link_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Revoke link",
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
        revoked = app(interaction).store.revoke(self.link_id, interaction.user.id)
        text = (
            "🗑️ Link revoked — it no longer works."
            if revoked
            else "This link has already expired or was revoked."
        )
        await interaction.response.edit_message(content=text, view=None)


class AddLinkToBasketButton(discord.ui.DynamicItem[discord.ui.Button[Any]], template=rf"zb:basket:{LINK_ID}"):
    def __init__(self, link_id: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Add to basket",
                emoji="🧺",
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
        store = app(interaction).store
        record = store.link_by_id(self.link_id, interaction.user.id)
        if record is None:
            await interaction.response.send_message(
                "This link has expired — run the command again.", ephemeral=True
            )
            return
        result, basket = store.add_to_basket(
            interaction.user.id, record.message_key or record.link_id, record.items
        )
        content, view = basket_reply(result, basket)
        await send_ephemeral(interaction, content, view)


class BasketLinkButton(discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"zb:basketlink"):
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
        state = app(interaction)
        content, view = basket_link_reply(state.store, state.settings.public_base_url, interaction.user.id)
        await interaction.response.edit_message(content=content, view=view)


class BasketClearButton(discord.ui.DynamicItem[discord.ui.Button[Any]], template=r"zb:basketclear"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Clear", emoji="🧹", style=discord.ButtonStyle.secondary, custom_id="zb:basketclear"
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> BasketClearButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        app(interaction).store.clear_basket(interaction.user.id)
        await interaction.response.edit_message(content="🧺 Basket cleared.", view=None)


DYNAMIC_ITEMS = (RevokeButton, AddLinkToBasketButton, BasketLinkButton, BasketClearButton)


def download_view(
    base_url: str, token: str, record: LinkRecord, *, basket_button: bool = True
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for button in link_buttons(base_url, token):
        view.add_item(button)
    if basket_button:
        view.add_item(AddLinkToBasketButton(record.link_id))
    view.add_item(RevokeButton(record.link_id))
    return detached(view)


# --- basket ----------------------------------------------------------------------------

BASKET_RESULT_TEXT = {
    AddResult.ADDED: "Added to your basket.",
    AddResult.DUPLICATE: "That message is already in your basket.",
    AddResult.FULL_MESSAGES: "Your basket is full (message limit). Get the link or clear it first.",
    AddResult.FULL_FILES: "That would exceed the file limit for one link. Get the link or clear the basket.",
    AddResult.EMPTY: "That message has no images or videos.",
}


def basket_status(basket: Basket | None) -> str:
    if basket is None or not basket.entries:
        return "🧺 Your basket is empty."
    return f"🧺 Basket: **{plural(len(basket.entries), 'message')} · {plural(basket.file_count, 'file')}**"


def basket_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BasketLinkButton())
    view.add_item(BasketClearButton())
    return detached(view)


def basket_reply(result: AddResult | None, basket: Basket | None) -> tuple[str, discord.ui.View | None]:
    lines = [BASKET_RESULT_TEXT[result]] if result else []
    lines.append(basket_status(basket))
    has_items = basket is not None and bool(basket.entries)
    return "\n".join(lines), basket_view() if has_items else None


def basket_link_reply(store: Store, base_url: str, user_id: int) -> tuple[str, discord.ui.View | None]:
    basket = store.basket(user_id)
    if basket is None or not basket.entries:
        return "🧺 Your basket is empty — use **Apps → Add to ZIP basket** on a message first.", None
    try:
        token, record = store.create_link(user_id, basket.items())
    except LinkError as exc:
        return f"Couldn't create a link: {exc}.", basket_view()
    return f"{basket_status(basket)}\n{link_summary(record)}", download_view(
        base_url, token, record, basket_button=False
    )
