from __future__ import annotations

import pytest

from zipbot.config import Settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    # Isolate from the developer's real .env and environment.
    monkeypatch.chdir(tmp_path)
    for key in ("DISCORD_TOKEN", "PUBLIC_BASE_URL", "MEDIA_TYPES", "TRUSTED_PROXIES"):
        monkeypatch.delenv(key, raising=False)
    return Settings(discord_token="test-token", public_base_url="https://dl.example.com")  # pyright: ignore[reportCallIssue]
