from __future__ import annotations

import pytest

from helpers import EX, item
from zipbot import store as store_mod
from zipbot.store import CDN_SAFETY_SEC, AddResult, LinkError, Store, token_hash


class Clock:
    def __init__(self, t: float = EX - 24 * 3600) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Store:
    return Store(
        link_ttl_sec=3600,
        basket_ttl_sec=3600,
        max_files_per_link=5,
        basket_max_messages=3,
        max_active_links_per_user=2,
        clock=clock,
    )


def test_create_and_resolve(store: Store, clock: Clock) -> None:
    token, record = store.create_link(7, [item("a.jpg"), item("a.jpg")])
    assert len(token) >= 43
    assert store.resolve(token) is record
    assert record.names == ("a.jpg", "a (1).jpg")
    assert record.expires_at == clock.t + 3600
    assert record.total_size == 200
    assert record.link_id and record.link_id not in token


def test_only_hash_is_stored(store: Store) -> None:
    token, _ = store.create_link(7, [item()])
    assert token not in store._links
    assert token_hash(token) in store._links


def test_unknown_token(store: Store) -> None:
    assert store.resolve("nope") is None


def test_expiry(store: Store, clock: Clock) -> None:
    token, _ = store.create_link(7, [item()])
    clock.t += 3600
    assert store.resolve(token) is None
    assert len(store) == 0


def test_expiry_clamped_to_cdn(store: Store, clock: Clock) -> None:
    clock.t = EX - 1800
    _, record = store.create_link(7, [item(), item("b.jpg", cdn_expires_at=None)])
    assert record.expires_at == EX - CDN_SAFETY_SEC


def test_cdn_about_to_expire(store: Store, clock: Clock) -> None:
    clock.t = EX - CDN_SAFETY_SEC - 30
    with pytest.raises(LinkError):
        store.create_link(7, [item()])


@pytest.mark.parametrize("n", [0, 6])
def test_item_count_limits(store: Store, n: int) -> None:
    with pytest.raises(LinkError):
        store.create_link(7, [item(f"{i}.jpg") for i in range(n)])


def test_per_user_cap_evicts_oldest(store: Store) -> None:
    t1, _ = store.create_link(7, [item()])
    t2, _ = store.create_link(7, [item()])
    t_other, _ = store.create_link(8, [item()])
    t3, _ = store.create_link(7, [item()])
    assert store.resolve(t1) is None
    assert store.resolve(t2) and store.resolve(t3) and store.resolve(t_other)
    assert store.active_links(7) == 2


def test_global_item_budget_refuses_instead_of_evicting(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "MAX_ITEMS_TOTAL", 4)
    first, _ = store.create_link(1, [item(), item("b.jpg")])
    store.create_link(2, [item()])
    with pytest.raises(LinkError, match="busy"):
        store.create_link(3, [item(), item("b.jpg")])
    assert store.resolve(first)  # nobody else's link was evicted
    assert len(store) == 2 and store.item_count == 3
    store.create_link(3, [item()])  # still fits exactly
    assert store.item_count == 4


def test_budget_counts_own_evictions(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "MAX_ITEMS_TOTAL", 4)
    store.create_link(7, [item(), item("b.jpg")])
    store.create_link(7, [item(), item("b.jpg")])  # full; the per-user cap is 2
    _, record = store.create_link(7, [item(), item("b.jpg")])  # replaces the user's oldest
    assert store.item_count == 4 and store.active_links(7) == 2
    with pytest.raises(LinkError):
        store.create_link(8, [item()])
    assert store.link_by_id(record.link_id, 7)


def test_item_count_tracks_every_removal(store: Store, clock: Clock) -> None:
    token, record = store.create_link(7, [item(), item("b.jpg")])
    store.create_link(8, [item()])
    store.revoke(record.link_id, 7)
    assert store.item_count == 1
    clock.t += 3600
    assert store.sweep() == 1
    assert store.item_count == 0
    t2, _ = store.create_link(9, [item()])
    clock.t += 3600
    assert store.resolve(t2) is None
    assert store.item_count == 0 and store.resolve(token) is None


def test_revoke_only_by_owner(store: Store) -> None:
    token, record = store.create_link(7, [item()])
    assert not store.revoke(record.link_id, owner_id=8)
    assert store.resolve(token)
    assert store.revoke(record.link_id, owner_id=7)
    assert store.resolve(token) is None
    assert not store.revoke(record.link_id, owner_id=7)


def test_basket_flow(store: Store) -> None:
    res, basket = store.add_to_basket(7, "m1", [item("a.jpg"), item("b.jpg")])
    assert res is AddResult.ADDED
    assert store.add_to_basket(7, "m1", [item("a.jpg")])[0] is AddResult.DUPLICATE
    store.add_to_basket(7, "m2", [item("a.jpg", source=0)])
    assert basket.file_count == 3
    items = basket.items()
    assert [i.source_index for i in items] == [0, 0, 1]
    _, record = store.create_link(7, items)
    assert record.names == ("01/a.jpg", "01/b.jpg", "02/a.jpg")


def test_basket_caps(store: Store) -> None:
    assert store.add_to_basket(7, "m1", [item(f"{i}.jpg") for i in range(4)])[0] is AddResult.ADDED
    assert store.add_to_basket(7, "m2", [item(), item()])[0] is AddResult.FULL_FILES
    store.add_to_basket(7, "m2", [item()])
    assert store.add_to_basket(7, "m3", [])[0] is AddResult.EMPTY
    s2 = Store(
        link_ttl_sec=1,
        basket_ttl_sec=60,
        max_files_per_link=99,
        basket_max_messages=2,
        max_active_links_per_user=1,
    )
    s2.add_to_basket(1, "a", [item()])
    s2.add_to_basket(1, "b", [item()])
    assert s2.add_to_basket(1, "c", [item()])[0] is AddResult.FULL_MESSAGES


def test_empty_add_does_not_create_basket(store: Store) -> None:
    assert store.add_to_basket(7, "m1", [])[0] is AddResult.EMPTY
    assert store.basket(7) is None


def test_basket_sliding_ttl_and_clear(store: Store, clock: Clock) -> None:
    store.add_to_basket(7, "m1", [item()])
    clock.t += 3000
    store.add_to_basket(7, "m2", [item()])
    clock.t += 3000
    basket = store.basket(7)
    assert basket is not None
    assert len(basket.entries) == 2
    clock.t += 600
    assert store.basket(7) is None
    store.add_to_basket(7, "m1", [item()])
    assert store.clear_basket(7)
    assert not store.clear_basket(7)


def test_sweep(store: Store, clock: Clock) -> None:
    store.create_link(7, [item()])
    store.create_link(8, [item()])
    store.add_to_basket(7, "m1", [item()])
    clock.t += 1800
    store.add_to_basket(9, "m1", [item()])
    clock.t += 1800
    assert store.sweep() == 3  # both links and basket 7 hit their TTL
    assert len(store) == 0
    assert store.basket(9) is not None


def test_one_live_basket_link(store: Store) -> None:
    with pytest.raises(LinkError):
        store.basket_link(7)
    store.add_to_basket(7, "m1", [item("a.jpg")])
    t1, r1 = store.basket_link(7)
    store.create_link(7, [item()])  # an ordinary link next to it
    store.add_to_basket(7, "m2", [item("b.jpg")])
    t2, r2 = store.basket_link(7)
    assert store.resolve(t1) is None  # replaced, not piled up
    assert store.resolve(t2) is r2 and r2.names == ("01/a.jpg", "02/b.jpg")
    t3, _ = store.basket_link(7)  # the cap of 2 is never eaten by basket links
    assert store.active_links(7) == 2 and store.resolve(t3)
    basket = store.basket(7)
    assert basket and basket.link_id != r1.link_id


def test_clear_basket_revokes_its_link(store: Store) -> None:
    store.add_to_basket(7, "m1", [item()])
    token, _ = store.basket_link(7)
    assert store.clear_basket(7)
    assert store.resolve(token) is None
    store.add_to_basket(7, "m1", [item()])
    assert store.clear_basket(7)  # no link was made: nothing to revoke
