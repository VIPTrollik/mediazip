"""Command logic shared by the cogs: payload in → reply (a panel, a notice or a toast) out. No
Discord I/O here, so it is tested directly (PLAN.md §11 "bot handlers"); a basket change only
schedules the DM basket message's edit, which runs in the background."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import discord

from zipbot.media import MediaItem, extract, payload_from_message
from zipbot.store import AddResult, LinkError
from zipbot.views import (
    BASKET_ALREADY_EMPTY,
    BASKET_CLEARED,
    AppState,
    DmPost,
    Panel,
    PanelLink,
    Reply,
    basket_note,
    basket_panel,
    basket_reply,
    basket_toast,
    clear_basket,
    dm_basket_view,
    layout,
    link_panel,
    nothing_to_download,
)


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
) -> Panel:
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


def download_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Panel:
    items = extract(payload, kinds=_kinds(state))
    if not items:
        state.stats.record("no_media")
        return nothing_to_download(state.settings, "This")
    return link_reply(state, user_id, items, key)


def burst_reply(
    state: AppState, user_id: int, items: Sequence[MediaItem], messages: int, total: int | None = None
) -> Panel:
    """One DM burst → one link; "Add to ZIP" then adds the whole burst as one entry."""
    note = f"📨 {messages} messages" if messages > 1 else None
    return link_reply(state, user_id, items, None, total=total, note=note)


def add_to_basket_reply(state: AppState, user_id: int, payload: Mapping[str, Any], key: str) -> Reply:
    items = extract(payload, kinds=_kinds(state))
    result, _ = state.store.add_to_basket(user_id, key, items)
    if result is AddResult.EMPTY:
        state.stats.record("no_media")
    if result is AddResult.ADDED:
        state.dm_baskets.changed(user_id)  # posts or edits the DM basket message
    note = basket_note(state.settings, result, len(items))
    return basket_reply(state, user_id, note, len(items) if result is AddResult.ADDED else None)


def basket_show_reply(state: AppState, user_id: int, *, in_bot_dm: bool = False) -> Reply:
    """A toast pointing at the live DM basket message; in the bot DM, the answer itself can
    become that message; otherwise the basket panel."""
    if toast := basket_toast(state, user_id):
        return toast
    if in_bot_dm and state.dm_baskets.enabled and (rendered := dm_basket_view(state, user_id)):
        return DmPost(*rendered)
    return basket_panel(state, user_id)


def basket_clear_reply(state: AppState, user_id: int) -> Reply:
    if clear_basket(state, user_id):
        return layout(BASKET_CLEARED)  # replaces the panel whose basket buttons are now stale
    return BASKET_ALREADY_EMPTY
