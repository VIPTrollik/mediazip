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
MAX_LINKS_TOTAL = 50_000  # memory bound; oldest links are evicted first


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LinkError(Exception):
    """The items cannot be turned into a link (none, too many, about to expire)."""


@dataclass(frozen=True, slots=True)
class LinkRecord:
    link_id: str  # public handle for buttons (revoke); not the token
    owner_id: int
    items: tuple[MediaItem, ...]
    names: tuple[str, ...]
    created_at: float
    expires_at: float
    message_key: str | None = None  # source message, so "Add to basket" can dedupe

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
        self._baskets: dict[int, Basket] = {}

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
        mine = [h for h, r in self._links.items() if r.owner_id == owner_id]
        for h in mine[: max(0, len(mine) - self.max_active_links_per_user + 1)]:
            del self._links[h]
        while len(self._links) >= MAX_LINKS_TOTAL:
            self._links.popitem(last=False)

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
        return token, record

    def resolve(self, token: str) -> LinkRecord | None:
        h = token_hash(token)
        record = self._links.get(h)
        if record is None:
            return None
        if record.expires_at <= self.clock():
            del self._links[h]
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
                del self._links[h]
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
            del self._baskets[user_id]
            return None
        return b

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
            result = AddResult.ADDED
        if not b.entries:
            self._baskets.pop(user_id, None)
        return result, b

    def clear_basket(self, user_id: int) -> bool:
        return self._baskets.pop(user_id, None) is not None

    # --- housekeeping ------------------------------------------------------------------

    def sweep(self) -> int:
        now = self.clock()
        dead = [h for h, r in self._links.items() if r.expires_at <= now]
        for h in dead:
            del self._links[h]
        stale = [u for u, b in self._baskets.items() if b.touched_at + self.basket_ttl_sec <= now]
        for u in stale:
            del self._baskets[u]
        return len(dead) + len(stale)

    def __len__(self) -> int:
        return len(self._links)
