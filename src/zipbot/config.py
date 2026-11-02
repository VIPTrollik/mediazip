"""Settings loaded from the environment (`.env` in dev, `env_file` in compose)."""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: SecretStr
    public_base_url: str
    # For the /install link before the bot has logged in; after login the bot's own id is used.
    discord_application_id: int | None = Field(None, ge=1)
    web_host: str = "0.0.0.0"  # noqa: S104 - inside the container; compose publishes on 127.0.0.1 only
    web_port: int = Field(8080, ge=1, le=65535)

    link_ttl_min: int = Field(60, ge=1, le=24 * 60)
    basket_ttl_min: int = Field(60, ge=1, le=24 * 60)
    basket_max_messages: int = Field(10, ge=1, le=50)
    max_files_per_link: int = Field(100, ge=1, le=500)
    # Every attachment by default; e.g. `image,video` opts out of other files.
    media_types: Annotated[frozenset[str], NoDecode] = frozenset({"all"})
    dm_debounce_sec: float = Field(8, ge=0, le=60)
    max_active_links_per_user: int = Field(20, ge=1, le=1000)
    resolve_rate_per_min: int = Field(30, ge=1, le=10_000)
    # Peers whose X-Forwarded-For is believed: loopback only; docker-compose.yml adds its gateway.
    trusted_proxies: Annotated[tuple[IPv4Network | IPv6Network, ...], NoDecode] = (
        ip_network("127.0.0.1/32"),
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("public_base_url")
    @classmethod
    def _https_url(cls, v: str) -> str:
        parts = urlsplit(v)
        local = parts.hostname in LOCAL_HOSTS
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("must be an https:// URL (http:// only allowed for localhost)")
        if not parts.hostname or parts.query or parts.fragment:
            raise ValueError("must be a bare origin/base path, without query or fragment")
        return v.rstrip("/")

    @field_validator("discord_application_id", mode="before")
    @classmethod
    def _optional_id(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v  # `DISCORD_APPLICATION_ID=` means unset

    @field_validator("media_types", mode="before")
    @classmethod
    def _media_types(cls, v: object) -> object:
        if isinstance(v, str):
            items = {s.strip().lower() for s in v.split(",") if s.strip()}
            allowed = {"image", "video", "audio", "all"}
            if not items or not items <= allowed:
                raise ValueError(f"comma list of {sorted(allowed)}")
            return frozenset(items)
        return v

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _proxies(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(ip_network(s.strip(), strict=False) for s in v.split(",") if s.strip())
        return v

    @property
    def all_media(self) -> bool:
        return "all" in self.media_types
