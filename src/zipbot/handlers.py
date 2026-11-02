"""Command logic shared by the cogs: payload in → (content, view) out. No Discord I/O here,
so it is tested directly (PLAN.md §11 "bot handlers")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import discord

from zipbot.media import MediaItem, extract, payload_from_message
from zipbot.store import LinkError
from zipbot.views import AppState, basket_link_reply, basket_reply, download_view, link_summary

NO_ATTACHMENTS = "This message has no attachments to download."

Reply = tuple[str, discord.ui.View | None]


def message_key(message: discord.Message) -> str:
    return f"{message.channel.id}/{message.id}"


def interaction_payload(interaction: discord.Interaction, message: discord.Message) -> Mapping[str, Any]:
    """The target message as sent in the interaction (complete, incl. snapshots), falling
    back to the parsed object."""
    data: Any = interaction.data or {}
    raw = data.get("resolved", {}).get("messages", {}).get(str(message.id))
    return raw if isinstance(raw, Mapping) else payload_from_message(message)


def _kinds(state: AppState) -> frozenset[str] | None:
    return None if state.settings.all_media else state.settings.media_types


def link_reply(state: AppState, user_id: int, items: Sequence[MediaItem], key: str | None) -> Reply:
    limit = state.settings.max_files_per_link
    total = len(items)
    try:
        token, record = state.store.create_link(user_id, items[:limit], message_key=key)
    except LinkError as exc:
        return f"Couldn't create a link: {exc}.", None
    summary = link_summary(record, truncated_from=total if total > limit else None)
    return summary, download_view(state.settings.public_base_url, token, record)


def download_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Reply:
    items = extract(payload, kinds=_kinds(state))
    if not items:
        return NO_ATTACHMENTS, None
    return link_reply(state, user_id, items, key)


def burst_reply(state: AppState, user_id: int, items: Sequence[MediaItem], messages: int) -> Reply:
    """One DM burst → one link; "Add to basket" then adds the whole burst as one entry."""
    content, view = link_reply(state, user_id, items, None)
    if messages > 1 and view is not None:
        content = f"📨 {messages} messages\n{content}"
    return content, view


def add_to_basket_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Reply:
    items = extract(payload, kinds=_kinds(state))
    result, basket = state.store.add_to_basket(user_id, key, items)
    return basket_reply(result, basket if basket.entries else None)


def basket_show_reply(state: AppState, user_id: int) -> Reply:
    return basket_reply(None, state.store.basket(user_id))


def basket_link(state: AppState, user_id: int) -> Reply:
    return basket_link_reply(state.store, state.settings.public_base_url, user_id)


def basket_clear_reply(state: AppState, user_id: int) -> Reply:
    cleared = state.store.clear_basket(user_id)
    return ("🧺 Basket cleared." if cleared else "🧺 Your basket is already empty."), None
