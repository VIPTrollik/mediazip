from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from helpers import EX, fixture, resolved_message
from zipbot.__main__ import configure_logging
from zipbot.bot import ZipBot
from zipbot.cogs import dm as dm_mod
from zipbot.cogs.dm import BUSY, HELP, HELP_COOLDOWN_SEC, DmCog
from zipbot.config import Settings
from zipbot.store import Store
from zipbot.views import ID_FILES, UserCooldown

STATE = cast(Any, SimpleNamespace(http=None))
TOKEN = re.compile(r"/d#([A-Za-z0-9_-]{43})")


class Channel:
    def __init__(self) -> None:
        self.id = 55
        self.sent: list[tuple[str, Any]] = []  # (text, view): a panel's text, or the plain message
        self.fail = False
        self.gate: asyncio.Event | None = None  # if set, send() waits for it

    async def send(self, content: str | None = None, *, view: Any = None) -> Any:
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise discord.HTTPException(cast(Any, SimpleNamespace(status=500, reason="x")), "boom")
        assert (content is None) != (view is None)  # a V2 layout never comes with `content`
        self.sent.append((panel_text(view) if view is not None else content or "", view))
        return SimpleNamespace(delete=AsyncMock())

    def typing(self) -> Any:
        class _Typing:
            async def __aenter__(self) -> None: ...
            async def __aexit__(self, *exc: object) -> None: ...

        return _Typing()


def panel_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def attachments(n: int | None = None) -> list[discord.Attachment]:
    data = resolved_message(fixture("interaction_context_menu_attachments.json"))["attachments"]
    return [discord.Attachment(data=a, state=STATE) for a in data[:n]]


def dm(channel: Channel, msg_id: int, *, files: list[Any] | None = None, forward: list[Any] | None = None,
       user: int = 7, bot: bool = False, guild: Any = None) -> Any:  # fmt: skip
    snapshots = [SimpleNamespace(attachments=forward, embeds=[])] if forward else []
    return SimpleNamespace(
        id=msg_id,
        guild=guild,
        author=SimpleNamespace(id=user, bot=bot),
        channel=channel,
        attachments=files or [],
        embeds=[],
        message_snapshots=snapshots,
    )


@pytest.fixture
async def cog(settings: Settings) -> Any:
    s = settings.model_copy(update={"dm_debounce_sec": 0.05})
    store = Store.from_settings(s)
    store.clock = lambda: EX - 24 * 3600
    bot = ZipBot(s, store)
    c = DmCog(bot)
    yield c
    await c.cog_unload()
    await bot.close()


async def settle(cog: DmCog) -> None:
    for _ in range(100):
        tasks = [b.task for b in cog.pending.values() if b.task]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0.01)


def link_record(cog: DmCog, content_view: tuple[str, Any]) -> Any:
    _, view = content_view
    url = next(c.url for c in view.walk_children() if getattr(c, "id", None) == ID_FILES)
    m = TOKEN.search(url)
    assert m
    return cog.bot.store.resolve(m.group(1))


async def test_burst_of_forwards_gives_one_link(cog: DmCog) -> None:
    ch = Channel()
    for n in range(3):
        await cog.on_message(dm(ch, 100 + n, forward=attachments(2)))
    await settle(cog)
    assert len(ch.sent) == 1
    content, view = ch.sent[0]
    assert content.startswith("📨 3 messages\n📦 **6 files**")
    assert view.is_finished()  # detached: buttons are handled by DynamicItems
    record = link_record(cog, ch.sent[0])
    assert [n.split("/")[0] for n in record.names] == ["01", "01", "02", "02", "03", "03"]


async def test_each_burst_gets_its_own_link(cog: DmCog) -> None:
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(1)))
    await settle(cog)
    await cog.on_message(dm(ch, 2, files=attachments(2)))
    await settle(cog)
    assert len(ch.sent) == 2
    assert ch.sent[0][0].startswith("📦 **1 file**")
    assert ch.sent[1][0].startswith("📦 **2 files**")
    first, second = link_record(cog, ch.sent[0]), link_record(cog, ch.sent[1])
    assert first and second and first.link_id != second.link_id  # the earlier link keeps working
    assert cog.bot.store.basket(7) is None  # DMs don't touch the basket on their own


async def test_message_during_send_starts_new_burst(cog: DmCog) -> None:
    ch = Channel()
    ch.gate = asyncio.Event()
    await cog.on_message(dm(ch, 1, files=attachments(1)))
    await asyncio.sleep(0.1)  # first burst finished debouncing, now blocked in send()
    assert 7 not in cog.pending
    await cog.on_message(dm(ch, 2, files=attachments(2)))
    ch.gate.set()
    await settle(cog)
    firsts = sorted(content.split(" · ")[0] for content, _ in ch.sent)
    assert firsts == ["📦 **1 file**", "📦 **2 files**"]  # neither burst swallowed the other
    assert len(ch.sent) == 2


async def test_text_only_gets_help(cog: DmCog) -> None:
    ch = Channel()
    await cog.on_message(dm(ch, 1))
    assert ch.sent == [(HELP, None)]


async def test_text_during_burst_is_silent(cog: DmCog) -> None:
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(1)))
    await cog.on_message(dm(ch, 2))
    await settle(cog)
    assert len(ch.sent) == 1 and ch.sent[0][1] is not None


@pytest.mark.parametrize("kw", [{"bot": True}, {"guild": object()}])
async def test_ignores_bots_and_guilds(cog: DmCog, kw: dict[str, Any]) -> None:
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(1), **kw))
    await settle(cog)
    assert ch.sent == []


async def test_burst_is_truncated_to_link_limit(cog: DmCog) -> None:
    cog.bot.settings = cog.bot.settings.model_copy(update={"max_files_per_link": 3})
    cog.bot.store.max_files_per_link = 3
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(5)))
    await settle(cog)
    assert "Only the first 3 of 5 files" in ch.sent[0][0]


async def test_send_failure_is_contained(cog: DmCog) -> None:
    ch = Channel()
    ch.fail = True
    await cog.on_message(dm(ch, 1, files=attachments(1)))
    await settle(cog)
    assert cog.pending == {}


async def test_users_are_independent(cog: DmCog) -> None:
    a, b = Channel(), Channel()
    await cog.on_message(dm(a, 1, files=attachments(1), user=1))
    await cog.on_message(dm(b, 2, files=attachments(2), user=2))
    await settle(cog)
    assert "1 file" in a.sent[0][0] and "2 files" in b.sent[0][0]


def test_library_loggers_never_debug() -> None:
    root = logging.getLogger()
    old = root.level
    try:
        configure_logging("DEBUG")
        assert root.level == logging.DEBUG
        assert logging.getLogger("discord").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("discord.http").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("aiohttp.access").getEffectiveLevel() == logging.INFO
    finally:
        root.setLevel(old)


async def test_full_burst_is_sent_at_once(cog: DmCog) -> None:
    cog.bot.settings = cog.bot.settings.model_copy(update={"max_files_per_link": 3, "dm_debounce_sec": 60})
    cog.bot.store.max_files_per_link = 3
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(2)))
    await cog.on_message(dm(ch, 2, files=attachments(2)))  # fills the burst: no 60 s wait
    assert 7 not in cog.pending
    await asyncio.wait_for(asyncio.gather(*cog.sending), 2)
    assert len(ch.sent) == 1
    assert "📦 **3 files**" in ch.sent[0][0] and "Only the first 3 of 4 files" in ch.sent[0][0]
    await cog.on_message(dm(ch, 3, files=attachments(1)))
    assert cog.pending[7].messages == 1  # the next message starts a new burst


def test_burst_keeps_at_most_the_cap() -> None:
    from zipbot.cogs.dm import Burst
    from zipbot.media import extract, payload_from_message

    items = extract(payload_from_message(dm(Channel(), 1, files=attachments(5))))
    burst = Burst(cast(Any, None), started=0)
    burst.add(items, 3)
    burst.add(items, 3)
    assert len(burst.items) == 3 and burst.offered == 10 and burst.messages == 2


async def test_burst_lifetime_is_capped(cog: DmCog) -> None:
    cog.bot.settings = cog.bot.settings.model_copy(update={"dm_debounce_sec": 8})
    clock = SimpleNamespace(t=0.0)
    cog.clock = lambda: clock.t
    delays: list[float] = []

    async def flush(user_id: int, burst: Any, delay: float) -> None:
        delays.append(delay)

    cog.flush = flush  # type: ignore[method-assign]
    ch = Channel()
    for n, t in enumerate([0, 7, 14, 21, 23, 30]):
        clock.t = t
        await cog.on_message(dm(ch, n, files=attachments(1)))
        await asyncio.sleep(0)
    # 3 x 8 s debounce = 24 s after the first message at most, however often more arrive
    assert delays == [8, 8, 8, 3, 1, 0]


async def test_help_has_a_cooldown(cog: DmCog) -> None:
    clock = SimpleNamespace(t=0.0)
    cog.clock = lambda: clock.t
    ch = Channel()
    await cog.on_message(dm(ch, 1))
    await cog.on_message(dm(ch, 2))
    await cog.on_message(dm(ch, 3, user=8))
    assert [c for c, _ in ch.sent] == [HELP, HELP]
    clock.t += HELP_COOLDOWN_SEC
    await cog.on_message(dm(ch, 4))
    assert len(ch.sent) == 3
    assert list(cog.helped) == [7]  # user 8's entry expired and was dropped


async def test_help_registry_is_bounded(cog: DmCog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm_mod, "MAX_NOTICE_USERS", 2)
    ch = Channel()
    for user in range(4):
        await cog.on_message(dm(ch, user, user=user))
    assert len(cog.helped) == 2 and len(ch.sent) == 4


async def test_bursts_are_rate_limited_with_one_notice(cog: DmCog) -> None:
    cog.burst_cooldown = UserCooldown(rate=1, per=60)
    clock = SimpleNamespace(t=0.0)
    cog.clock = lambda: clock.t
    ch = Channel()
    await cog.on_message(dm(ch, 1, files=attachments(1)))
    await settle(cog)
    for n in range(2, 6):  # a second burst within the minute, message after message
        await cog.on_message(dm(ch, n, files=attachments(1)))
    await settle(cog)
    assert [c for c, _ in ch.sent][1:] == [BUSY]  # one notice, not one per message
    assert 7 not in cog.pending
    other = Channel()
    await cog.on_message(dm(other, 9, files=attachments(1), user=8))  # per user
    await settle(cog)
    assert len(other.sent) == 1 and other.sent[0][1] is not None
