from __future__ import annotations

import re

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Dotfiles lose the leading dot: nothing hidden lands in an extracted folder.
        (".env", "env"),
        (".env.local", "env.local"),
        (".gitignore", "gitignore"),
        ("..env", "env"),
        # No extension: kept as is.
        ("Makefile", "Makefile"),
        ("README", "README"),
        ("nul", "_nul"),
        # Every extension is kept (only lowercased), not just media ones.
        ("report.PDF", "report.pdf"),
        ("notes.txt", "notes.txt"),
        ("setup.exe", "setup.exe"),
        ("photos.zip", "photos.zip"),
        ("Backup.TAR.GZ", "Backup.tar.gz"),
        ("data.tar.xz", "data.tar.xz"),
        ("v1.2.3.tar.bz2", "v1.2.3.tar.bz2"),
        ("aux.tar.gz", "_aux.tar.gz"),
        (".tar.gz", "tar.gz"),
        ("tar.gz", "tar.gz"),
        ("photo.2024.jpg", "photo.2024.jpg"),
    ],
)
def test_sanitize_any_file_type(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


@pytest.mark.parametrize("ext", [".pdf", ".tar.gz", ".docx", ".7z"])
def test_long_names_keep_their_extension(ext: str) -> None:
    out = sanitize("ž" * 300 + ext.upper())
    assert len(out) == MAX_NAME
    assert out.endswith(ext)
    assert out[: -len(ext)] == "ž" * (MAX_NAME - len(ext))


def test_names_match_the_zip_entry_pattern() -> None:
    hostile = [".env", ".", "..", "....tar.gz", "a" * 300, " .bashrc ", "x\x00.txt", "dir/.ssh/id_rsa"]
    names = assign_names([item(n, ctype=None, source=i % 2) for i, n in enumerate(hostile)])
    entry = re.compile(r'^(\d{2}/)?[^/\\:*?"<>|\x00-\x1f]+$')
    assert all(entry.match(n) and len(n) <= 3 + MAX_NAME for n in names)
    assert not any(n.split("/")[-1].startswith((".", " ")) for n in names)


def test_dedupe_keeps_different_extensions_apart() -> None:
    items = [item("Report.pdf"), item("report.PDF"), item("report.txt"), item("REPORT"), item("report")]
    assert assign_names(items) == ["Report.pdf", "report (1).pdf", "report.txt", "REPORT", "report (1)"]


def test_dedupe_counter_goes_before_a_double_extension() -> None:
    items = [item("backup.tar.gz"), item("BACKUP.tar.GZ"), item("backup.gz")]
    assert assign_names(items) == ["backup.tar.gz", "BACKUP (1).tar.gz", "backup.gz"]


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
