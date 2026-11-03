"""The basket as one long-lived message in the bot DM (PLAN.md §3, "DM basket").

On the first add the bot posts the basket to the user's DM (silently) and from then on edits
that one message: every add, a clear (deletes it), the basket expiring (it turns into "Basket
expired", with no links, thumbnails or names). While it is live, only this message mints the
basket link (which the store never evicts); panels and toasts jump to it.

Per user, in memory:

    NONE ──add──▶ posting ──sent──▶ LIVE ──change──▶ (one edit in flight, the latest state wins)
                    │                 ├─ link ran out ──────▶ redrawn with a fresh link
                    │                 ├─ clear ─────────────▶ deleted ─▶ NONE
                    │                 ├─ basket expiry ─────▶ "Your ZIP expired" ─▶ NONE
                    │                 ├─ edit 404 (deleted) ─▶ posted anew, in the same run
                    │                 └─ edit 403 ──────────▶ unavailable
                    └─ send fails ─▶ unavailable (6 h for 403 50007/50278, else 5 min): panels

A delete or expiry edit that fails on the network is retried by the tick (bounded), so names
and thumbnails don't stay behind. After a restart nothing is known: on the user's first basket
action the DM's recent history is scanned once for the bot's old basket messages (by their
Refresh button), which are turned into "expired". Their buttons still work: Refresh revives a
copy if the basket still exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import discord

from zipbot import views
from zipbot.views import NET_ERRORS

log = logging.getLogger(__name__)

UNAVAILABLE_SEC = 6 * 3600  # after a 403 50007/50278 (blocked / can't DM them): panels only
BACKOFF_SEC = 5 * 60  # after any other failed send (5xx, network, timeout, our own 400)
NO_DM_CODES = frozenset({50007, 50278})  # 50007: blocked the app; 50278: no way to DM them
MAX_USERS = 10_000  # each of: live messages, unavailable marks, users scanned since start
SCAN_LIMIT = 25  # recent DM messages looked at for old basket messages
POST_WAIT_SEC = 10  # a toast waits this long for the DM message to be posted
EDIT_GAP_SEC = 1.0  # between two edits of one user's message
TICK_SEC = 30  # how often expiry is checked
MAX_RETRIES = 5  # a failed delete/expiry edit is retried this often, one tick apart

Want = Literal["live", "delete", "expire"]


class DmClient(views.AppState, Protocol):
    """What the DM basket needs from the bot (a ZipBot)."""

    @property
    def user(self) -> discord.ClientUser | None: ...

    async def create_dm(self, user: discord.abc.Snowflake) -> discord.DMChannel: ...

    def get_partial_messageable(
        self, id: int, *, guild_id: int | None = None, type: discord.ChannelType | None = None
    ) -> discord.PartialMessageable: ...


@dataclass(slots=True)
class _Ref:
    channel_id: int | None = None
    message_id: int | None = None  # None: not posted yet
    link_id: str | None = None  # the basket link the message shows
    due_at: float | None = None  # when that link or the basket expires (store clock)
    want: Want = "live"  # what the next edit makes of the message
    dirty: bool = False  # an edit is due
    posted: asyncio.Event = field(default_factory=asyncio.Event)  # the first send is done (or failed)
    retry: Want | None = None  # a delete/expiry edit that failed, redone by the tick
    failures: int = 0
    stale: list[tuple[int, int]] = field(default_factory=list)  # (channel, message) to expire first


def error_code(exc: BaseException) -> int | None:
    return exc.code if isinstance(exc, discord.HTTPException) else None


class DmBaskets:
    def __init__(self, client: DmClient, *, gap: float = EDIT_GAP_SEC, cap: int = MAX_USERS) -> None:
        self.client = client
        self.gap = gap
        self.cap = cap
        self._refs: OrderedDict[int, _Ref] = OrderedDict()  # user → their message, least recent first
        self._unavailable: OrderedDict[int, float] = OrderedDict()  # user → no DM until then
        self._scanned: OrderedDict[int, None] = OrderedDict()  # users whose DM history was scanned
        self._tasks: dict[int, asyncio.Task[None]] = {}  # one editor per user with pending changes
        self._background: set[asyncio.Task[None]] = set()
        self._ticker: asyncio.Task[None] | None = None

    def _now(self) -> float:
        return self.client.store.clock()

    # --- what the panels ask -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.client.settings.basket_dm

    def usable(self, user_id: int) -> bool:
        """Whether a DM basket message may be posted for this user."""
        if not self.enabled:
            return False
        until = self._unavailable.get(user_id)
        if until is not None and until <= self._now():
            del self._unavailable[user_id]
            until = None
        return until is None

    def live(self, user_id: int) -> bool:
        """The user has a DM basket message (or one is being posted)."""
        return self.enabled and user_id in self._refs

    def pending(self, user_id: int) -> bool:
        ref = self._refs.get(user_id)
        return self.enabled and ref is not None and ref.message_id is None

    def busy(self, user_id: int) -> bool:
        """The user's message is being posted or edited right now."""
        return self.enabled and user_id in self._tasks

    def jump_url(self, user_id: int) -> str | None:
        ref = self._refs.get(user_id)
        if not self.enabled or ref is None or ref.channel_id is None or ref.message_id is None:
            return None
        return views.jump_url(ref.channel_id, ref.message_id)

    def is_live_message(self, user_id: int, message_id: int) -> bool:
        ref = self._refs.get(user_id)
        return ref is not None and ref.message_id == message_id

    async def wait_settled(self, user_id: int) -> str | None:
        """The jump URL once the pending post or edit is done (at most POST_WAIT_SEC): the edit
        may have found the message deleted and posted a new one. None if posting failed (then
        `usable()` is False) or the basket went."""
        if (task := self._tasks.get(user_id)) is not None:
            await asyncio.wait({task}, timeout=POST_WAIT_SEC)  # never cancels the editor
        return self.jump_url(user_id)

    # --- what changes it ---------------------------------------------------------------

    def changed(self, user_id: int) -> None:
        """The basket changed (an add): post the message, or edit it."""
        if self.live(user_id) or self.usable(user_id):
            self._schedule(user_id, "live")

    def link_gone(self, user_id: int) -> None:
        """The store dropped the basket's link while the basket lives on: redraw with a new one."""
        if self.live(user_id):
            self._schedule(user_id, "live")

    def cleared(self, user_id: int) -> None:
        if user_id in self._refs:
            self._schedule(user_id, "delete")

    def mark_unavailable(self, user_id: int, exc: BaseException | None = None) -> None:
        """No DM for a while: 6 h if Discord says the bot can't DM this user, else a short
        backoff (5xx, network, timeout)."""
        no_dm = exc is None or (isinstance(exc, discord.Forbidden) and exc.code in NO_DM_CODES)
        self._unavailable.pop(user_id, None)
        self._unavailable[user_id] = self._now() + (UNAVAILABLE_SEC if no_dm else BACKOFF_SEC)
        if len(self._unavailable) > self.cap:
            self._unavailable.popitem(last=False)

    def adopt(self, user_id: int, channel_id: int, message_id: int, record: Any) -> None:
        """A command in the bot DM answered with the basket message itself: it is the live one.
        If another one appeared meanwhile (an add while this answer was being sent), this one
        is turned into "expired" instead, so the user never has two."""
        if not self.enabled:
            return
        if user_id in self._refs:
            self._spawn(self._expire_message(channel_id, message_id))
            return
        self._unavailable.pop(user_id, None)  # the bot can post there after all
        ref = self._new(user_id)
        ref.channel_id, ref.message_id = channel_id, message_id
        self._shown(user_id, ref, record)
        ref.posted.set()
        if user_id not in self._scanned:
            self._spawn(self._scan(user_id, channel_id, keep=message_id))

    def take_over(self, user_id: int, channel_id: int, message_id: int) -> bool:
        """Refresh on a basket message: it is redrawn (and becomes the live one, if it was an old
        copy). False: another one is being posted right now."""
        if not self.enabled:
            return False
        ref = self._refs.get(user_id)
        if ref is not None and ref.message_id is None:
            return False
        if ref is None or ref.message_id != message_id:
            stale = list(ref.stale) if ref is not None else []
            if ref is not None and ref.channel_id is not None and ref.message_id is not None:
                # Expired by this user's editor, after any edit of it still in flight.
                stale.append((ref.channel_id, ref.message_id))
            self._refs.pop(user_id, None)
            self._unavailable.pop(user_id, None)
            ref = self._new(user_id)
            ref.channel_id, ref.message_id, ref.stale = channel_id, message_id, stale
            ref.posted.set()
        self._schedule(user_id, "live")
        return True

    def forget_message(self, user_id: int, message_id: int) -> None:
        """`message_id` was turned into "expired" by its own button."""
        ref = self._refs.get(user_id)
        if ref is not None and ref.message_id == message_id:
            self._drop(user_id, ref)

    # --- expiry ------------------------------------------------------------------------

    def tick(self) -> None:
        """Expire messages whose basket has run out, redraw those whose link has, and retry
        failed deletes/expiry edits (called every TICK_SEC)."""
        store = self.client.store
        now = self._now()
        for user_id, ref in list(self._refs.items()):
            if ref.message_id is None or ref.due_at is None or ref.due_at > now or user_id in self._tasks:
                continue
            if ref.retry is not None:
                self._schedule(user_id, ref.retry)
                continue
            basket = store.basket(user_id)
            if basket is None or not basket.entries:
                self._schedule(user_id, "expire")
            elif basket.link_id != ref.link_id:
                self._schedule(user_id, "live")  # an edit failed earlier: redraw
            elif ref.link_id is not None and store.link_by_id(ref.link_id, user_id) is None:
                self._schedule(user_id, "live")  # the link ran out before the basket: a fresh one
            else:
                ref.due_at = self._due(user_id, ref.link_id)

    def start(self) -> None:
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick_forever())

    async def _tick_forever(self) -> None:
        while True:
            await asyncio.sleep(TICK_SEC)
            try:
                self.tick()
            except Exception as exc:
                log.warning("DM basket tick failed: %s", type(exc).__name__)

    async def close(self) -> None:
        tasks = [*self._tasks.values(), *self._background, *([self._ticker] if self._ticker else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._ticker = None

    # --- the editor --------------------------------------------------------------------

    def _new(self, user_id: int) -> _Ref:
        ref = self._refs[user_id] = _Ref()
        while len(self._refs) > self.cap:
            _, old = self._refs.popitem(last=False)
            old.posted.set()
            # Don't leave its names and thumbnails behind (one edit per eviction; a message still
            # being posted is expired by _post itself).
            if old.channel_id is not None and old.message_id is not None:
                self._spawn(self._expire_message(old.channel_id, old.message_id))
        return ref

    def _drop(self, user_id: int, ref: _Ref) -> None:
        ref.posted.set()
        if self._refs.get(user_id) is ref:
            del self._refs[user_id]

    def _gone(self, user_id: int, ref: _Ref) -> None:
        """The message is gone (deleted, expired, unknown): forget it, unless an add is already
        waiting, which then posts a new one."""
        ref.message_id = ref.link_id = ref.due_at = None
        ref.retry, ref.failures = None, 0
        ref.posted = asyncio.Event()
        if not (ref.dirty and ref.want == "live"):
            self._drop(user_id, ref)

    def _schedule(self, user_id: int, want: Want) -> None:
        ref = self._refs.get(user_id)
        if ref is None:
            if want != "live":
                return
            ref = self._new(user_id)
        else:
            self._refs.move_to_end(user_id)
        ref.want, ref.dirty, ref.retry = want, True, None
        if user_id not in self._tasks:
            self._tasks[user_id] = asyncio.create_task(self._run(user_id))

    async def _run(self, user_id: int) -> None:
        """The user's one editor: applies the latest wanted state until nothing is due."""
        try:
            while (ref := self._refs.get(user_id)) is not None and ref.dirty:
                ref.dirty = False
                await self._apply(user_id, ref)
                if ref.dirty and self.gap:
                    await asyncio.sleep(self.gap)
        except Exception as exc:
            log.warning("DM basket update failed: %s", type(exc).__name__)
        finally:
            self._tasks.pop(user_id, None)

    def _channel(self, channel_id: int) -> discord.PartialMessageable:
        return self.client.get_partial_messageable(channel_id, type=discord.ChannelType.private)

    def _due(self, user_id: int, link_id: str | None) -> float | None:
        store = self.client.store
        record = store.link_by_id(link_id, user_id) if link_id else None
        times = [t for t in (store.basket_expires_at(user_id), record and record.expires_at) if t]
        return min(times) if times else None

    def _shown(self, user_id: int, ref: _Ref, record: Any) -> None:
        ref.link_id = record.link_id if record is not None else None
        ref.due_at = self._due(user_id, ref.link_id)
        ref.failures = 0

    async def _apply(self, user_id: int, ref: _Ref) -> None:
        while ref.stale:  # old copies replaced by take_over(), after their in-flight edits
            await self._expire_message(*ref.stale.pop())
        # Rendering mints the basket link, so it happens here, in the one editor of this user.
        rendered = views.dm_basket_view(self.client, user_id) if ref.want == "live" else None
        channel_id, message_id = ref.channel_id, ref.message_id
        if rendered is None:  # cleared, expired, or the basket went meanwhile
            if channel_id is None or message_id is None or await self._remove(ref, channel_id, message_id):
                self._gone(user_id, ref)
            else:
                self._retry_later(user_id, ref)
        elif channel_id is None or message_id is None:
            await self._post(user_id, ref, *rendered)
        else:
            await self._edit(user_id, ref, channel_id, message_id, *rendered)

    async def _remove(self, ref: _Ref, channel_id: int, message_id: int) -> bool:
        """Delete (on clear) or expire the message. False: a network error left it as it was."""
        if ref.want == "delete":
            try:
                await self._channel(channel_id).get_partial_message(message_id).delete()
                return True
            except discord.NotFound:
                return True
            except NET_ERRORS as exc:
                self._failed("delete", exc)  # then at least strip it
        return await self._expire_message(channel_id, message_id)

    def _retry_later(self, user_id: int, ref: _Ref) -> None:
        ref.failures += 1
        if ref.failures > MAX_RETRIES:
            log.info("DM basket: giving up on a message after %d attempts", ref.failures)
            self._drop(user_id, ref)
            return
        ref.retry = ref.want
        ref.due_at = self._now() + TICK_SEC

    async def _post(self, user_id: int, ref: _Ref, view: discord.ui.LayoutView, record: Any) -> None:
        try:
            if ref.channel_id is None:
                ref.channel_id = (await self.client.create_dm(discord.Object(user_id))).id
            if user_id not in self._scanned:
                await self._scan(user_id, ref.channel_id, keep=None)
            message = await self._channel(ref.channel_id).send(view=view, silent=True)
        except NET_ERRORS as exc:
            self._failed("send", exc)
            self.mark_unavailable(user_id, exc)
            self._drop(user_id, ref)
            return
        ref.message_id = message.id
        self._shown(user_id, ref, record)
        ref.posted.set()
        if self._refs.get(user_id) is not ref:  # dropped from the cap meanwhile: don't leave it live
            await self._expire_message(ref.channel_id, message.id)

    async def _edit(
        self,
        user_id: int,
        ref: _Ref,
        channel_id: int,
        message_id: int,
        view: discord.ui.LayoutView,
        record: Any,
    ) -> None:
        if user_id not in self._scanned:
            await self._scan(user_id, channel_id, keep=message_id)
        try:
            await self._channel(channel_id).get_partial_message(message_id).edit(view=view)
        except discord.NotFound:
            # The user deleted it: post this very change anew (toasts wait for the new jump).
            ref.message_id = ref.link_id = ref.due_at = None
            ref.posted = asyncio.Event()
            await self._post(user_id, ref, view, record)
        except discord.Forbidden as exc:
            self._failed("edit", exc)
            self.mark_unavailable(user_id, exc)
            self._drop(user_id, ref)
        except NET_ERRORS as exc:
            # The message keeps its old (now revoked) link; the tick redraws it in a minute.
            self._failed("edit", exc)
            ref.due_at = self._now() + 2 * TICK_SEC
        else:
            self._shown(user_id, ref, record)

    async def _expire_message(self, channel_id: int, message_id: int) -> bool:
        """Turn a message into "Your ZIP expired". False: a network error, worth retrying."""
        try:
            await self._channel(channel_id).get_partial_message(message_id).edit(view=views.dm_expired_view())
        except (discord.NotFound, discord.Forbidden):
            return True  # gone, or nothing the bot can do about it any more
        except NET_ERRORS as exc:
            self._failed("expire", exc)
            return False
        return True

    async def _scan(self, user_id: int, channel_id: int, keep: int | None) -> None:
        """Once per user per process: turn the bot's old basket messages in the DM (from before
        a restart) into "expired"."""
        self._scanned.pop(user_id, None)
        self._scanned[user_id] = None
        if len(self._scanned) > self.cap:
            self._scanned.popitem(last=False)
        me = self.client.user
        if me is None:
            return
        try:
            async for message in self._channel(channel_id).history(limit=SCAN_LIMIT):
                if message.author.id == me.id and message.id != keep and views.is_dm_basket(message):
                    await message.edit(view=views.dm_expired_view())
        except NET_ERRORS as exc:
            self._failed("scan", exc)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _failed(what: str, exc: BaseException) -> None:
        # Only the error type and Discord's code (50007: blocked, 50278: no way to DM them). A 400
        # means a payload the bot built is wrong: that's our bug, so it's a warning.
        level = logging.WARNING if getattr(exc, "status", None) == 400 else logging.INFO
        log.log(level, "DM basket %s failed: %s %s", what, type(exc).__name__, error_code(exc) or "")
