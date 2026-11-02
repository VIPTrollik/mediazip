from __future__ import annotations

import pytest

from helpers import item
from zipbot.naming import MAX_NAME, assign_names, sanitize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.JPG", "photo.jpg"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ("..\\..\\win.ini", "_.._win.ini"),
        ('a/b\\c:d*e?f"g<h>i|j.png', "a_b_c_d_e_f_g_h_i_j.png"),
        ("tab\there\x00.gif", "tab_here_.gif"),
        ("  .hidden. ", "hidden"),
        ("trailing.dots...", "trailing.dots"),
        ("CON.txt", "_CON.txt"),
        ("lpt1", "_lpt1"),
        ("com1.tar.gz", "_com1.tar.gz"),
        ("console.log", "console.log"),
        ("", "file"),
        ("...", "file"),
        (".jpg", "jpg"),
        ("evil‮gpj.exe", "evilgpj.exe"),
        ("é.png", "é.png"),
        ("weird.ext!", "weird.ext!"),
        ("name.verylongextension123", "name.verylongextension123"),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_sanitize_caps_length_keeping_extension() -> None:
    out = sanitize("x" * 500 + ".jpeg")
    assert len(out) == MAX_NAME
    assert out.endswith(".jpeg")


def test_dedupe_case_insensitive() -> None:
    items = [item("a.jpg"), item("A.JPG"), item("a.jpg"), item("b.png")]
    assert assign_names(items) == ["a.jpg", "A (1).jpg", "a (2).jpg", "b.png"]


def test_dedupe_does_not_collide_with_existing_counter_name() -> None:
    items = [item("a (1).jpg"), item("a.jpg"), item("a.jpg")]
    assert assign_names(items) == ["a (1).jpg", "a.jpg", "a (2).jpg"]


def test_counter_respects_length_cap() -> None:
    long = "y" * 200 + ".png"
    names = assign_names([item(long), item(long)])
    assert names[0] != names[1]
    assert all(len(n) <= MAX_NAME for n in names)
    assert names[1].endswith(" (1).png")


def test_folders_only_with_multiple_sources() -> None:
    assert assign_names([item("a.jpg", source=3), item("a.jpg", source=3)]) == ["a.jpg", "a (1).jpg"]
    names = assign_names([item("a.jpg", source=0), item("a.jpg", source=5), item("a.jpg", source=0)])
    assert names == ["01/a.jpg", "02/a.jpg", "01/a (1).jpg"]
