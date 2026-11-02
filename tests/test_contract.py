from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from helpers import EX, fixture, item, resolved_message
from zipbot.contract import resolve_body, thumbnail
from zipbot.media import extract
from zipbot.store import Store

SCHEMA = json.loads((Path(__file__).parent / "contract" / "resolve.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def store() -> Store:
    return Store(
        link_ttl_sec=3600,
        basket_ttl_sec=3600,
        max_files_per_link=100,
        basket_max_messages=10,
        max_active_links_per_user=20,
        clock=lambda: EX - 24 * 3600,
    )


def test_schema_itself_is_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_fixture_messages_produce_valid_bodies(store: Store) -> None:
    items = [
        *extract(resolved_message(fixture("interaction_context_menu_attachments.json")), source_index=0),
        *extract(fixture("message_image_embed.json"), source_index=1),
        *extract(fixture("dm_forward.json"), source_index=2),
    ]
    _, record = store.create_link(1, items)
    body = resolve_body(record)
    jsonschema.validate(body, SCHEMA)
    assert body["expires_at"] == EX - 24 * 3600 + 3600
    names = [i["name"] for i in body["items"]]
    assert names[0] == "01/test-1.jpg"
    assert names[5] == "02/Rotating_earth_(large).gif"
    video = body["items"][4]
    assert video["kind"] == "video"
    assert video["duration"] == 3.0
    assert "format=webp" in video["thumb"]
    assert "width=320&height=180" in video["thumb"]


def test_hostile_filename_still_matches_schema(store: Store) -> None:
    _, record = store.create_link(1, [item('<img src=x onerror=alert(1)>/../"x".png'), item("CON")])
    body = resolve_body(record)
    jsonschema.validate(body, SCHEMA)
    assert body["items"][0]["name"] == "_img src=x onerror=alert(1)__..__x_.png"


def test_any_file_type_body(store: Store) -> None:
    items = [
        *extract(fixture("message_any_files.json"), source_index=0, kinds=None),  # MEDIA_TYPES=all
        item(".env", ctype=None, width=None, height=None, source=1),  # dotfile, no content_type
    ]
    _, record = store.create_link(1, items)
    body = resolve_body(record)
    jsonschema.validate(body, SCHEMA)
    rows = [(i["name"], i["kind"], i["mime"], i["thumb"]) for i in body["items"]]
    assert rows[:5] == [
        ("01/report.pdf", "file", "application/pdf", None),
        ("01/notes.txt", "file", "text/plain; charset=utf-8", None),
        ("01/photos.zip", "file", "application/zip", None),
        ("01/Makefile", "file", None, None),
        ("01/backup.tar.gz", "file", None, None),
    ]
    assert rows[5][:2] == ("01/photo.png", "image") and rows[5][3]
    assert rows[6] == ("02/env", "file", None, None)


def test_schema_v2_kinds() -> None:
    assert SCHEMA["$id"] == "zipbot/resolve-response/v2"
    body = resolve_body_stub()
    for kind in ("image", "video", "audio", "file"):
        body["items"][0]["kind"] = kind
        jsonschema.validate(body, SCHEMA)
    for kind in (None, "document"):  # v2: every item has a kind
        body["items"][0]["kind"] = kind
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(body, SCHEMA)


def resolve_body_stub() -> dict[str, Any]:
    entry = dict.fromkeys(SCHEMA["properties"]["items"]["items"]["required"])
    entry.update(url="https://cdn.discordapp.com/a/1/x.bin", name="x.bin", kind="file")
    return {"expires_at": 1, "items": [entry]}


def test_schema_rejects_foreign_urls() -> None:
    body = {"expires_at": 1, "items": [dict.fromkeys(SCHEMA["properties"]["items"]["items"]["required"])]}
    body["items"][0].update(url="https://evil.example/x", name="x")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(body, SCHEMA)


def test_thumbnail_rules() -> None:
    assert thumbnail(item(proxy_url=None)) is None
    assert thumbnail(item("a.pdf", ctype="application/pdf")) is None
    small = thumbnail(item(width=100, height=50))
    assert small is not None
    assert small.endswith("&format=webp&width=100&height=50")
    ext = item(
        proxy_url="https://images-ext-1.discordapp.net/external/x/https/a/b.gif", width=None, height=None
    )
    assert thumbnail(ext) == ext.proxy_url
    sized = item(
        proxy_url="https://images-ext-1.discordapp.net/external/x/https/a/b.gif", width=640, height=640
    )
    assert (
        thumbnail(sized)
        == "https://images-ext-1.discordapp.net/external/x/https/a/b.gif?width=320&height=320"
    )
    no_amp = item(proxy_url="https://media.discordapp.net/a/b.png?ex=1", width=None, height=None)
    assert thumbnail(no_amp) == "https://media.discordapp.net/a/b.png?ex=1&format=webp"
