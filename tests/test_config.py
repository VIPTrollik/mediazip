from __future__ import annotations

from ipaddress import ip_address

import pytest
from pydantic import ValidationError

from zipbot.config import Settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("DISCORD_TOKEN", "s3cr3t-value")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://dl.example.com/")
    return monkeypatch


def load() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]


def test_defaults(env: pytest.MonkeyPatch) -> None:
    s = load()
    assert s.public_base_url == "https://dl.example.com"
    assert s.media_types == {"image", "video"}  # other files lack CORS on Discord's CDN
    assert not s.all_media
    assert s.link_ttl_min == 60
    assert [str(n) for n in s.trusted_proxies] == ["127.0.0.1/32"]  # compose adds its gateway
    assert not any(ip_address("172.18.0.1") in net for net in s.trusted_proxies)


def test_application_id_optional(env: pytest.MonkeyPatch) -> None:
    assert load().discord_application_id is None
    env.setenv("DISCORD_APPLICATION_ID", "")  # the .env.example line, left empty
    assert load().discord_application_id is None
    env.setenv("DISCORD_APPLICATION_ID", "1234567890123")
    assert load().discord_application_id == 1234567890123


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_rejects_bad_application_id(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("DISCORD_APPLICATION_ID", value)
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize(("value", "types"), [("all", {"all"}), ("audio, VIDEO", {"audio", "video"})])
def test_media_types_opt_in(env: pytest.MonkeyPatch, value: str, types: set[str]) -> None:
    env.setenv("MEDIA_TYPES", value)
    s = load()
    assert s.media_types == types
    assert s.all_media == (value == "all")


def test_ga_measurement_id_optional(env: pytest.MonkeyPatch) -> None:
    env.delenv("GA_MEASUREMENT_ID", raising=False)
    assert load().ga_measurement_id is None  # unset: no Google Analytics at all
    env.setenv("GA_MEASUREMENT_ID", "")
    assert load().ga_measurement_id is None
    env.setenv("GA_MEASUREMENT_ID", " G-Z8VRMRQ3X4 ")
    assert load().ga_measurement_id == "G-Z8VRMRQ3X4"


@pytest.mark.parametrize(
    "value", ["UA-12345-1", "G-abc123", "G-12", "G-12345678901234567", 'G-1234"><script>', "GT-ABCDEF"]
)
def test_rejects_bad_ga_measurement_id(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("GA_MEASUREMENT_ID", value)
    with pytest.raises(ValidationError):
        load()


def test_token_is_secret(env: pytest.MonkeyPatch) -> None:
    s = load()
    assert "s3cr3t" not in repr(s)
    assert s.discord_token.get_secret_value() == "s3cr3t-value"


def test_lists_from_env(env: pytest.MonkeyPatch) -> None:
    env.setenv("MEDIA_TYPES", "Image, all")
    env.setenv("TRUSTED_PROXIES", "10.0.0.1, 192.168.0.0/16")
    s = load()
    assert s.all_media
    assert [str(n) for n in s.trusted_proxies] == ["10.0.0.1/32", "192.168.0.0/16"]


@pytest.mark.parametrize(
    "url", ["http://dl.example.com", "ftp://x", "https://x/?q=1", "https://x/#f", "nope"]
)
def test_rejects_bad_base_url(env: pytest.MonkeyPatch, url: str) -> None:
    env.setenv("PUBLIC_BASE_URL", url)
    with pytest.raises(ValidationError):
        load()


def test_allows_http_localhost(env: pytest.MonkeyPatch) -> None:
    env.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    assert load().public_base_url == "http://localhost:8080"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MEDIA_TYPES", "pdf"),
        ("MEDIA_TYPES", ","),
        ("LINK_TTL_MIN", "0"),
        ("MAX_ACTIVE_LINKS_PER_USER", "1"),  # a panel needs its own link plus the basket link
    ],
)
def test_rejects_bad_values(env: pytest.MonkeyPatch, key: str, value: str) -> None:
    env.setenv(key, value)
    with pytest.raises(ValidationError):
        load()


def test_missing_token_fails(env: pytest.MonkeyPatch) -> None:
    env.delenv("DISCORD_TOKEN")
    with pytest.raises(ValidationError):
        load()
