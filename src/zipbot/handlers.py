"""Command logic shared by the cogs: payload in → reply (a panel or a notice) out. No Discord
I/O here, so it is tested directly (PLAN.md §11 "bot handlers")."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import discord

from zipbot.media import MediaItem, extract, payload_from_message
from zipbot.store import LinkError
from zipbot.views import (
    BASKET_CLEARED,
    AppState,
    PanelLink,
    Reply,
    basket_note,
    basket_panel,
    layout,
    link_panel,
)

NO_ATTACHMENTS = "This message has no attachments to download."


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


def link_reply(
    state: AppState,
    user_id: int,
    items: Sequence[MediaItem],
    key: str | None,
    *,
    total: int | None = None,
    note: str | None = None,
) -> Reply:
    """`total`: how many items there were before the caller capped them (DM bursts)."""
    limit = state.settings.max_files_per_link
    total = len(items) if total is None else total
    try:
        token, record = state.store.create_link(user_id, items[:limit], message_key=key)
    except LinkError as exc:
        return f"Couldn't create a link: {exc}."
    link = PanelLink.new(state.settings.public_base_url, token, record)
    truncated_from = total if total > len(record.items) else None
    return link_panel(state, user_id, link, note=note, truncated_from=truncated_from)


def download_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Reply:
    items = extract(payload, kinds=_kinds(state))
    if not items:
        return NO_ATTACHMENTS
    return link_reply(state, user_id, items, key)


def burst_reply(
    state: AppState, user_id: int, items: Sequence[MediaItem], messages: int, total: int | None = None
) -> Reply:
    """One DM burst → one link; "Add to basket" then adds the whole burst as one entry."""
    note = f"📨 {messages} messages" if messages > 1 else None
    return link_reply(state, user_id, items, None, total=total, note=note)


def add_to_basket_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Reply:
    items = extract(payload, kinds=_kinds(state))
    result, _ = state.store.add_to_basket(user_id, key, items)
    return basket_panel(state, user_id, basket_note(result, len(items)))


def basket_show_reply(state: AppState, user_id: int) -> Reply:
    return basket_panel(state, user_id)


def basket_clear_reply(state: AppState, user_id: int) -> Reply:
    if state.store.clear_basket(user_id):
        return layout(BASKET_CLEARED)  # replaces the panel whose basket buttons are now stale
    return "🧺 Your basket is already empty."
