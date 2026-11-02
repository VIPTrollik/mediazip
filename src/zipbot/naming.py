"""Safe, unique file names for the ZIP / individual downloads (PLAN.md §4).

Names are computed once on the server and sent in the resolve response, so the page
never invents paths itself.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from zipbot.media import MediaItem

MAX_NAME = 120  # characters, extension included
MAX_EXT = 16
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)


def _split_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > MAX_EXT or not ext.isalnum():
        return name, ""
    return stem, "." + ext


def sanitize(name: str, fallback: str = "file") -> str:
    """One path segment that is valid on Windows, macOS, Linux and inside a ZIP."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(
        ch for ch in name if unicodedata.category(ch) not in {"Cf", "Co", "Cs"}
    )  # bidi tricks etc.
    name = _UNSAFE.sub("_", name).strip(" .")
    stem, ext = _split_ext(name)
    stem = stem.strip(" .")
    stem = stem or fallback
    if _RESERVED.match(stem.split(".", 1)[0]):
        stem = "_" + stem
    stem = stem[: MAX_NAME - len(ext)].rstrip(" .") or fallback
    return stem + ext.lower()


def _with_counter(name: str, n: int) -> str:
    stem, ext = _split_ext(name)
    suffix = f" ({n})"
    return stem[: MAX_NAME - len(ext) - len(suffix)] + suffix + ext


def assign_names(items: Sequence[MediaItem]) -> list[str]:
    """Final relative path per item: sanitized, unique (case-insensitive), and placed in a
    `01/`, `02/`... folder per source message when items come from more than one message."""
    sources = sorted({item.source_index for item in items})
    folders = {src: f"{pos:02d}/" for pos, src in enumerate(sources, 1)} if len(sources) > 1 else {}
    taken: set[str] = set()
    result: list[str] = []
    for item in items:
        folder = folders.get(item.source_index, "")
        base = sanitize(item.filename)
        candidate, n = base, 0
        while (folder + candidate).casefold() in taken:
            n += 1
            candidate = _with_counter(base, n)
        taken.add((folder + candidate).casefold())
        result.append(folder + candidate)
    return result
