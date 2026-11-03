"""The basket as one long-lived message in the bot DM (PLAN.md §3, "DM basket").

On the first add the bot posts the basket to the user's DM (silently) and from then on edits
that one message: every add, a clear (deletes it), the basket or its link expiring (it turns
into "Basket expired", with no links, thumbnails or names). While it is live, only this message
mints the basket link; panels and toasts jump to it.

Per user, in memory:

    NONE ──add──▶ posting ──sent──▶ LIVE ──change──▶ (one edit in flight, the latest state wins)
                    │                 ├─ clear ─────────────▶ deleted ─▶ NONE
                    │                 ├─ basket/link expiry ─▶ "Basket expired" ─▶ NONE
                    │                 ├─ edit 404 (deleted) ─▶ NONE: the next add posts a new one
                    │                 └─ edit 403 ──────────▶ unavailable
                    └─ send fails ─▶ unavailable for 6 h: today's ephemeral panels instead

After a restart nothing is known: on the user's first basket action the DM's recent history is
scanned once for the bot's old basket messages (by their Refresh button), which are turned
into "expired". Their buttons still work: Refresh revives a copy if the basket still exists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import discord

from zipbot import views
from zipbot.views import NET_ERRORS

log = logging.getLogger(__name__)

UNAVAILABLE_SEC = 6 * 3600  # after a failed send, panels only for this long
MAX_USERS = 10_000  # each of: live messages, unavailable marks, users scanned since start
SCAN_LIMIT = 25  # recent DM messages looked at for old basket messages
POST_WAIT_SEC = 10  # a toast waits this long for the DM message to be posted
EDIT_GAP_SEC = 1.0  # between two edits of one user's message
TICK_SEC = 30  # how often expiry is checked

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
        now = self._now()
        while self._unavailable and next(iter(self._unavailable.values())) <= now:
            self._unavailable.popitem(last=False)
        return user_id not in self._unavailable

    def live(self, user_id: int) -> bool:
        """The user has a DM basket message (or one is being posted)."""
        return self.enabled and user_id in self._refs

    def pending(self, user_id: int) -> bool:
        ref = self._refs.get(user_id)
        return self.enabled and ref is not None and ref.message_id is None

    def jump_url(self, user_id: int) -> str | None:
        ref = self._refs.get(user_id)
        if not self.enabled or ref is None or ref.channel_id is None or ref.message_id is None:
            return None
        return views.jump_url(ref.channel_id, ref.message_id)

    def is_live_message(self, user_id: int, message_id: int) -> bool:
        ref = self._refs.get(user_id)
        return ref is not None and ref.message_id == message_id

    async def wait_posted(self, user_id: int) -> str | None:
        """The jump URL once the message being posted is there; None if it failed (then
        `usable()` is False), the basket went, or it takes longer than POST_WAIT_SEC."""
        ref = self._refs.get(user_id)
        if ref is not None and ref.message_id is None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(POST_WAIT_SEC):
                    await ref.posted.wait()
        return self.jump_url(user_id)

    # --- what changes it ---------------------------------------------------------------

    def changed(self, user_id: int) -> None:
        """The basket changed (an add): post the message, or edit it."""
        if self.live(user_id) or self.usable(user_id):
            self._schedule(user_id, "live")

    def cleared(self, user_id: int) -> None:
        if user_id in self._refs:
            self._schedule(user_id, "delete")

    def mark_unavailable(self, user_id: int) -> None:
        self._unavailable.pop(user_id, None)
        self._unavailable[user_id] = self._now() + UNAVAILABLE_SEC
        if len(self._unavailable) > self.cap:
            self._unavailable.popitem(last=False)

    def adopt(self, user_id: int, channel_id: int, message_id: int, record: Any) -> None:
        """A command in the bot DM answered with the basket message itself: it is the live one."""
        if not self.enabled or user_id in self._refs:
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
            if ref is not None and ref.channel_id is not None and ref.message_id is not None:
                self._spawn(self._expire_message(ref.channel_id, ref.message_id))
                self._refs.pop(user_id, None)
            self._unavailable.pop(user_id, None)
            ref = self._new(user_id)
            ref.channel_id, ref.message_id = channel_id, message_id
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
        """Expire messages whose basket or link has run out (called every TICK_SEC)."""
        store = self.client.store
        now = self._now()
        for user_id, ref in list(self._refs.items()):
            if ref.message_id is None or ref.due_at is None or ref.due_at > now or user_id in self._tasks:
                continue
            basket = store.basket(user_id)
            if basket is None or not basket.entries:
                self._schedule(user_id, "expire")
            elif basket.link_id != ref.link_id:
                self._schedule(user_id, "live")  # an edit failed earlier: redraw
            elif ref.link_id is not None and store.link_by_id(ref.link_id, user_id) is None:
                self._schedule(user_id, "expire")  # the link ran out before the basket
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
            _, old = self._refs.popitem(last=False)  # its message just goes stale
            old.posted.set()
        return ref

    def _drop(self, user_id: int, ref: _Ref) -> None:
        ref.posted.set()
        if self._refs.get(user_id) is ref:
            del self._refs[user_id]

    def _gone(self, user_id: int, ref: _Ref) -> None:
        """The message is gone (deleted, expired, unknown): forget it, unless an add is already
        waiting, which then posts a new one."""
        ref.message_id = ref.link_id = ref.due_at = None
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
        ref.want, ref.dirty = want, True
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

    async def _apply(self, user_id: int, ref: _Ref) -> None:
        # Rendering mints the basket link, so it happens here, in the one editor of this user.
        rendered = views.dm_basket_view(self.client, user_id) if ref.want == "live" else None
        channel_id, message_id = ref.channel_id, ref.message_id
        if rendered is None:  # cleared, expired, or the basket went meanwhile
            if channel_id is not None and message_id is not None:
                if ref.want == "delete":
                    try:
                        await self._channel(channel_id).get_partial_message(message_id).delete()
                    except NET_ERRORS as exc:
                        self._failed("delete", exc)
                else:
                    await self._expire_message(channel_id, message_id)
            self._gone(user_id, ref)
        elif channel_id is None or message_id is None:
            await self._post(user_id, ref, *rendered)
        else:
            await self._edit(user_id, ref, channel_id, message_id, *rendered)

    async def _post(self, user_id: int, ref: _Ref, view: discord.ui.LayoutView, record: Any) -> None:
        try:
            if ref.channel_id is None:
                ref.channel_id = (await self.client.create_dm(discord.Object(user_id))).id
            if user_id not in self._scanned:
                await self._scan(user_id, ref.channel_id, keep=None)
            message = await self._channel(ref.channel_id).send(view=view, silent=True)
        except NET_ERRORS as exc:
            self._failed("send", exc)
            self.mark_unavailable(user_id)
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
            self._gone(user_id, ref)  # the user deleted it: the next add posts a new one
        except discord.Forbidden as exc:
            self._failed("edit", exc)
            self.mark_unavailable(user_id)
            self._drop(user_id, ref)
        except NET_ERRORS as exc:
            # The message keeps its old (now revoked) link; the tick redraws it in a minute.
            self._failed("edit", exc)
            ref.due_at = self._now() + 2 * TICK_SEC
        else:
            self._shown(user_id, ref, record)

    async def _expire_message(self, channel_id: int, message_id: int) -> None:
        try:
            await self._channel(channel_id).get_partial_message(message_id).edit(view=views.dm_expired_view())
        except NET_ERRORS as exc:
            self._failed("expire", exc)

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

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _failed(what: str, exc: BaseException) -> None:
        # Only the error type and Discord's code (50007: blocked, 50278: no way to DM them).
        log.info("DM basket %s failed: %s %s", what, type(exc).__name__, error_code(exc) or "")
