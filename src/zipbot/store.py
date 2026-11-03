"""In-memory TTL store: download links and baskets (PLAN.md §4, §6).

Tokens are bearer secrets: only `sha256(token)` is kept. Everything is lost on restart,
which is fine for links that live ≤ 60 minutes.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from zipbot.config import Settings
from zipbot.media import MediaItem
from zipbot.naming import assign_names

CDN_SAFETY_SEC = 5 * 60  # links die this long before the first CDN URL does
MIN_LINK_SEC = 60
# Memory bound for all links together. An item costs about 0.9 KB (two signed URLs, the name and
# its ZIP name; measured with tracemalloc), up to ~2 KB with very long names: 40k items stay
# under ~80 MB of the container's 256 MB. When full, new links are refused, so nobody can evict
# other users' links (security review v0 #3).
MAX_ITEMS_TOTAL = 40_000
# Per user, across their live links (a link plus the basket link at the default 100 files
# each): a user's own oldest links go first, so one account holds at most ~1.25% of the total.
MAX_ITEMS_PER_USER = 500


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LinkError(Exception):
    """The items cannot be turned into a link (none, too many, about to expire, store full)."""


@dataclass(frozen=True, slots=True)
class LinkRecord:
    link_id: str  # public handle for buttons (revoke); not the token
    owner_id: int
    items: tuple[MediaItem, ...]
    names: tuple[str, ...]
    created_at: float
    expires_at: float
    message_key: str | None = None  # source message, so "Add to ZIP" can dedupe

    @property
    def total_size(self) -> int:
        return sum(item.size or 0 for item in self.items)


@dataclass(slots=True)
class BasketEntry:
    message_key: str
    items: tuple[MediaItem, ...]


@dataclass(slots=True)
class Basket:
    entries: list[BasketEntry] = field(default_factory=list)
    touched_at: float = 0.0
    link_id: str | None = None  # the one live basket link; replaced when a new one is made
    version: int = 0  # bumped whenever entries change
    link_version: int = -1  # `version` when link_id was made

    @property
    def file_count(self) -> int:
        return sum(len(e.items) for e in self.entries)

    def items(self) -> list[MediaItem]:
        """All items, re-indexed so each basket message gets its own ZIP folder."""
        out: list[MediaItem] = []
        for pos, entry in enumerate(self.entries):
            out.extend(replace(item, source_index=pos) for item in entry.items)
        return out


class AddResult(Enum):
    ADDED = "added"
    DUPLICATE = "duplicate"
    FULL_MESSAGES = "full_messages"
    FULL_FILES = "full_files"
    EMPTY = "empty"


class Store:
    def __init__(
        self,
        *,
        link_ttl_sec: float,
        basket_ttl_sec: float,
        max_files_per_link: int,
        basket_max_messages: int,
        max_active_links_per_user: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.link_ttl_sec = link_ttl_sec
        self.basket_ttl_sec = basket_ttl_sec
        self.max_files_per_link = max_files_per_link
        self.basket_max_messages = basket_max_messages
        self.max_active_links_per_user = max_active_links_per_user
        self.clock = clock
        self._links: OrderedDict[str, LinkRecord] = OrderedDict()  # token hash → record, oldest first
        self._items = 0  # sum of len(record.items) over _links
        self._baskets: dict[int, Basket] = {}
        # Called with the user id when a live basket's link goes (it ran out, or anything but a
        # re-mint, clear or basket expiry dropped it): the DM basket message then redraws.
        self.on_basket_link_gone: Callable[[int], None] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        return cls(
            link_ttl_sec=settings.link_ttl_min * 60,
            basket_ttl_sec=settings.basket_ttl_min * 60,
            max_files_per_link=settings.max_files_per_link,
            basket_max_messages=settings.basket_max_messages,
            max_active_links_per_user=settings.max_active_links_per_user,
        )

    # --- links -------------------------------------------------------------------------

    def create_link(
        self, owner_id: int, items: Sequence[MediaItem], *, message_key: str | None = None
    ) -> tuple[str, LinkRecord]:
        if not items:
            raise LinkError("no media")
        if len(items) > self.max_files_per_link:
            raise LinkError(f"more than {self.max_files_per_link} files")
        now = self.clock()
        expires = now + self.link_ttl_sec
        cdn = [i.cdn_expires_at for i in items if i.cdn_expires_at is not None]
        if cdn:
            expires = min(expires, min(cdn) - CDN_SAFETY_SEC)
        if expires - now < MIN_LINK_SEC:
            raise LinkError("Discord file links expire too soon")

        self.sweep()
        mine = [h for h, r in self._links.items() if r.owner_id == owner_id]  # oldest first
        held = sum(len(self._links[h].items) for h in mine)
        budget = max(MAX_ITEMS_PER_USER, 2 * self.max_files_per_link)  # always fits link + basket
        # The basket link is never evicted: the DM basket message shows it for the basket's whole
        # life. The rest always makes room (at least 2 links, budget for two full ones).
        b = self._baskets.get(owner_id)
        pinned = b.link_id if b is not None else None
        evict: list[str] = []
        freed = 0
        for h in (h for h in mine if self._links[h].link_id != pinned):
            if len(mine) - len(evict) < self.max_active_links_per_user and (
                held - freed + len(items) <= budget
            ):
                break
            evict.append(h)
            freed += len(self._links[h].items)
        if self._items - freed + len(items) > MAX_ITEMS_TOTAL:
            raise LinkError("the bot is busy right now, please try again in a few minutes")
        for h in evict:
            self._drop(h)

        token = secrets.token_urlsafe(32)
        record = LinkRecord(
            link_id=secrets.token_urlsafe(9),
            owner_id=owner_id,
            items=tuple(items),
            names=tuple(assign_names(items)),
            created_at=now,
            expires_at=expires,
            message_key=message_key,
        )
        self._links[token_hash(token)] = record
        self._items += len(record.items)
        return token, record

    def _drop(self, h: str) -> None:
        record = self._links.pop(h)
        self._items -= len(record.items)
        b = self._baskets.get(record.owner_id)
        if b is not None and b.link_id == record.link_id:
            b.link_id = None
            if self.on_basket_link_gone is not None:
                self.on_basket_link_gone(record.owner_id)

    def resolve(self, token: str) -> LinkRecord | None:
        h = token_hash(token)
        record = self._links.get(h)
        if record is None:
            return None
        if record.expires_at <= self.clock():
            self._drop(h)
            return None
        return record

    def link_by_id(self, link_id: str, owner_id: int) -> LinkRecord | None:
        now = self.clock()
        for record in self._links.values():
            if record.link_id == link_id and record.owner_id == owner_id and record.expires_at > now:
                return record
        return None

    def revoke(self, link_id: str, owner_id: int) -> bool:
        for h, record in self._links.items():
            if record.link_id == link_id and record.owner_id == owner_id:
                self._drop(h)
                return True
        return False

    def active_links(self, owner_id: int) -> int:
        now = self.clock()
        return sum(1 for r in self._links.values() if r.owner_id == owner_id and r.expires_at > now)

    # --- baskets -----------------------------------------------------------------------

    def basket(self, user_id: int) -> Basket | None:
        b = self._baskets.get(user_id)
        if b is None:
            return None
        if b.touched_at + self.basket_ttl_sec <= self.clock():
            self._expire_basket(user_id)
            return None
        return b

    def _expire_basket(self, user_id: int) -> None:
        """An idle basket goes, and its link with it: a link never outlives its basket."""
        b = self._baskets.pop(user_id)
        if b.link_id:
            self.revoke(b.link_id, user_id)

    def basket_expires_at(self, user_id: int) -> float | None:
        b = self.basket(user_id)
        return None if b is None else b.touched_at + self.basket_ttl_sec

    def add_to_basket(
        self, user_id: int, message_key: str, items: Sequence[MediaItem]
    ) -> tuple[AddResult, Basket]:
        b = self.basket(user_id) or self._baskets.setdefault(user_id, Basket())
        b.touched_at = self.clock()
        if not items:
            result = AddResult.EMPTY
        elif any(e.message_key == message_key for e in b.entries):
            result = AddResult.DUPLICATE
        elif len(b.entries) >= self.basket_max_messages:
            result = AddResult.FULL_MESSAGES
        elif b.file_count + len(items) > self.max_files_per_link:
            result = AddResult.FULL_FILES
        else:
            b.entries.append(BasketEntry(message_key, tuple(items)))
            b.version += 1
            result = AddResult.ADDED
        if not b.entries:
            self._baskets.pop(user_id, None)
        return result, b

    def basket_link(
        self, user_id: int, token: str | None = None, *, mint: bool = True
    ) -> tuple[str, LinkRecord]:
        """A link for the whole basket. `token` (a basket link a panel already shows) is kept if
        it is still the basket's live link and the basket hasn't changed since; otherwise the
        previous basket link is revoked and a new one made, so each user has at most one and
        re-rendering a panel doesn't use up their link cap. With `mint=False` only `token` can
        come back: a panel that doesn't own the basket link never revokes it."""
        b = self.basket(user_id)
        if b is None or not b.entries:
            raise LinkError("your ZIP is empty")
        if token and b.link_version == b.version:
            current = self.resolve(token)
            if current and current.link_id == b.link_id and current.owner_id == user_id:
                return token, current
        if not mint:
            raise LinkError("your ZIP's link is shown elsewhere")
        if b.link_id:
            old, b.link_id = b.link_id, None  # a re-mint: nobody needs telling
            self.revoke(old, user_id)
        token, record = self.create_link(user_id, b.items())
        b.link_id, b.link_version = record.link_id, b.version
        return token, record

    def clear_basket(self, user_id: int) -> bool:
        b = self._baskets.pop(user_id, None)
        if b is not None and b.link_id:
            self.revoke(b.link_id, user_id)
        return b is not None

    # --- housekeeping ------------------------------------------------------------------

    def sweep(self) -> int:
        now = self.clock()
        # Baskets first: a basket expiring with its link is not "the link went, redraw".
        stale = [u for u, b in self._baskets.items() if b.touched_at + self.basket_ttl_sec <= now]
        for u in stale:
            self._expire_basket(u)
        dead = [h for h, r in self._links.items() if r.expires_at <= now]
        for h in dead:
            self._drop(h)
        return len(dead) + len(stale)

    def __len__(self) -> int:
        return len(self._links)

    @property
    def item_count(self) -> int:
        return self._items
