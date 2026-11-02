from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from helpers import EX, fixture, resolved_message
from zipbot.media import cdn_expiry, extract, is_allowed_url, payload_from_message

IMAGE_VIDEO = {"image", "video"}


def test_context_menu_fixture_all_attachments_in_order() -> None:
    items = extract(resolved_message(fixture("interaction_context_menu_attachments.json")), kinds=IMAGE_VIDEO)
    assert [i.filename for i in items] == ["test-1.jpg", "test-2.jpg", "test-3.jpg", "anim.gif", "clip.mp4"]
    assert [i.kind for i in items] == ["image"] * 4 + ["video"]
    clip = items[-1]
    assert clip.duration_secs == 3.0
    assert clip.size == 22362
    assert (clip.width, clip.height) == (640, 360)
    assert all(i.url.startswith("https://cdn.discordapp.com/") for i in items)
    assert all(i.proxy_url and i.proxy_url.startswith("https://media.discordapp.net/") for i in items)
    assert all(i.cdn_expires_at is not None and abs(i.cdn_expires_at - EX) <= 2 for i in items)


def test_no_media_fixture() -> None:
    assert extract(resolved_message(fixture("interaction_context_menu_no_media.json"))) == []


def test_forward_fixture_reads_snapshot() -> None:
    msg = fixture("dm_forward.json")
    assert msg["attachments"] == []
    items = extract(msg, kinds=IMAGE_VIDEO)
    assert len(items) == 5
    assert items[0].filename == "test-1.jpg"


def test_image_embed_fixture_uses_external_proxy() -> None:
    (it,) = extract(fixture("message_image_embed.json"), kinds=IMAGE_VIDEO)
    assert it.url.startswith("https://images-ext-1.discordapp.net/external/")
    assert it.filename == "Rotating_earth_(large).gif"
    assert it.content_type == "image/gif"
    assert it.size is None
    assert it.cdn_expires_at is None


def test_kinds_filter_and_all() -> None:
    msg = {
        "attachments": [
            {
                "url": "https://cdn.discordapp.com/a/1/doc.pdf",
                "filename": "doc.pdf",
                "content_type": "application/pdf",
            },
            {"url": "https://cdn.discordapp.com/a/2/song.mp3", "filename": "song.mp3"},  # no content_type
            {
                "url": "https://cdn.discordapp.com/a/3/pic.png",
                "filename": "pic.png",
                "content_type": "image/png",
            },
        ]
    }
    assert [i.filename for i in extract(msg, kinds=IMAGE_VIDEO)] == ["pic.png"]
    assert [i.filename for i in extract(msg, kinds={"audio"})] == ["song.mp3"]
    assert [i.kind for i in extract(msg)] == ["file", "audio", "image"]


def test_any_file_fixture() -> None:
    msg = fixture("message_any_files.json")
    items = extract(msg)
    assert [(i.filename, i.kind) for i in items] == [
        ("report.pdf", "file"),
        ("notes.txt", "file"),  # text/* is not media
        ("photos.zip", "file"),
        ("Makefile", "file"),  # no extension, no content_type
        ("backup.tar.gz", "file"),  # no content_type, guessed as a tarball
        ("photo.png", "image"),
    ]
    assert items[3].content_type is None
    assert [i.filename for i in extract(msg, kinds=IMAGE_VIDEO)] == ["photo.png"]
    assert extract(msg, kinds={"audio"}) == []


def test_untyped_embed_is_still_media() -> None:
    proxy = "https://images-ext-1.discordapp.net/external/x/https/example.com/pic"  # no extension
    image = {"type": "image", "thumbnail": {"proxy_url": proxy}}
    video = {"type": "video", "video": {"proxy_url": proxy + "2"}}
    items = extract({"embeds": [image, video]}, kinds=IMAGE_VIDEO)
    assert [(i.filename, i.kind, i.content_type) for i in items] == [
        ("pic", "image", None),
        ("pic2", "video", None),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/x.png",
        "https://evil.example/x.png",
        "https://cdn.discordapp.com.evil.example/x.png",
        "https://user@cdn.discordapp.com/x.png",
        None,
        42,
    ],
)
def test_disallowed_urls_dropped(url: object) -> None:
    assert not is_allowed_url(url)
    assert extract({"attachments": [{"url": url, "filename": "x.png", "content_type": "image/png"}]}) == []


def test_bad_proxy_url_is_ignored_but_item_kept() -> None:
    (it,) = extract(
        {
            "attachments": [
                {"url": "https://cdn.discordapp.com/a/x.png", "proxy_url": "https://evil.example/x"}
            ]
        }
    )
    assert it.proxy_url is None
    assert it.filename == "x.png"


def test_malformed_fields_are_tolerated() -> None:
    msg: dict[str, Any] = {
        "attachments": [
            "junk",
            {
                "url": "https://cdn.discordapp.com/a/x.png?ex=zz",
                "size": -5,
                "width": True,
                "duration_secs": "1",
            },
        ],
        "embeds": ["junk", {"type": "image", "thumbnail": "junk"}],
        "message_snapshots": ["junk", {"message": "junk"}],
    }
    (it,) = extract(msg)
    assert (it.size, it.width, it.duration_secs, it.cdn_expires_at) == (None, None, None, None)


def test_dedupe_by_path_across_layers() -> None:
    a = {"url": "https://cdn.discordapp.com/a/1/x.png?ex=1&hm=a", "filename": "x.png"}
    b = {"url": "https://cdn.discordapp.com/a/1/x.png?ex=2&hm=b", "filename": "x.png"}
    assert len(extract({"attachments": [a], "message_snapshots": [{"message": {"attachments": [b]}}]})) == 1


def test_embed_types() -> None:
    tenor = {
        "type": "gifv",
        "url": "https://tenor.com/view/cat-123",
        "video": {
            "url": "https://media.tenor.com/x/cat.mp4",
            "proxy_url": "https://images-ext-1.discordapp.net/external/s/https/media.tenor.com/x/cat.mp4",
            "width": 220,
            "height": 200,
        },
        "thumbnail": {
            "url": "https://media.tenor.com/x/cat.png",
            "proxy_url": "https://images-ext-1.discordapp.net/external/t/https/media.tenor.com/x/cat.png",
        },
    }
    youtube = {
        "type": "video",
        "url": "https://www.youtube.com/watch?v=x",
        "video": {"url": "https://www.youtube.com/embed/x"},
        "thumbnail": {
            "url": "https://i.ytimg.com/vi/x/hq.jpg",
            "proxy_url": "https://images-ext-2.discordapp.net/external/y/https/i.ytimg.com/vi/x/hq.jpg",
        },
    }
    article = {
        "type": "article",
        "thumbnail": {
            "url": "https://news/x.jpg",
            "proxy_url": "https://images-ext-1.discordapp.net/external/n/https/news/x.jpg",
        },
    }
    rich = {
        "type": "rich",
        "image": {
            "url": "https://cdn.discordapp.com/a/9/chart.png",
            "proxy_url": "https://media.discordapp.net/a/9/chart.png",
        },
    }
    items = extract({"embeds": [tenor, youtube, article, rich]}, kinds=IMAGE_VIDEO)
    assert [(i.filename, i.kind) for i in items] == [("cat.mp4", "video"), ("chart.png", "image")]
    assert items[0].url.endswith("/cat.mp4")


def test_embed_without_name_gets_fallback() -> None:
    (it,) = extract(
        {"embeds": [{"type": "image", "thumbnail": {"proxy_url": "https://images-ext-1.discordapp.net/"}}]}
    )
    assert it.filename == "embed-1"


def test_cdn_expiry() -> None:
    assert cdn_expiry("https://cdn.discordapp.com/x?ex=6aea244b&is=1") == EX
    assert cdn_expiry("https://cdn.discordapp.com/x") is None
    assert cdn_expiry("https://cdn.discordapp.com/x?ex=nothex") is None


def test_payload_from_discord_objects() -> None:
    data = resolved_message(fixture("interaction_context_menu_attachments.json"))
    state = cast(Any, SimpleNamespace(http=None))
    attachments = [discord.Attachment(data=a, state=state) for a in data["attachments"]]
    embed = discord.Embed.from_dict(fixture("message_image_embed.json")["embeds"][0])
    snapshot = SimpleNamespace(attachments=attachments[:1], embeds=[])
    message = cast(
        discord.Message,
        SimpleNamespace(attachments=attachments, embeds=[embed], message_snapshots=[snapshot]),
    )
    items = extract(payload_from_message(message), kinds=IMAGE_VIDEO)
    assert [i.filename for i in items] == [
        "test-1.jpg",
        "test-2.jpg",
        "test-3.jpg",
        "anim.gif",
        "clip.mp4",
        "Rotating_earth_(large).gif",
    ]
    assert items[4].duration_secs == 3.0
