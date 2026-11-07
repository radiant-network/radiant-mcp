"""Server configuration, read from the environment once and validated.

Every variable the server reads is a field of ``Settings``. ``__main__`` calls
``get_settings()`` at startup, so a missing or malformed value stops the server
before it accepts a request instead of failing the first tool that reads it.
Modules call ``get_settings()``, never ``os.getenv``. Tests install their own
instance with ``use_settings()``.
"""

import re
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DB_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _split(value: object) -> object:
    """``"openid, profile"`` → ``["openid", "profile"]``: comma-separated, blanks dropped."""
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    # -- OAuth (Keycloak) ---------------------------------------------------------
    # Bare realm URL, e.g. https://kc.example.com/realms/radiant. Required: there is
    # no unauthenticated mode.
    keycloak_realm_url: str = Field(min_length=1)
    # Expected `aud` claim. Unset: DCR-registered clients have no audience mapper.
    keycloak_audience: str | None = None
    oauth_required_scopes: Annotated[list[str], NoDecode] = ["openid"]
    # Advertised in the protected-resource metadata; Claude requests offline_access.
    oauth_supported_scopes: Annotated[list[str], NoDecode] = ["openid", "offline_access"]
    # Server root URL (not /mcp).
    mcp_base_url: str = "http://localhost:8000"
    # Proxies whose X-Forwarded-* headers uvicorn trusts.
    forwarded_allow_ips: str = "*"

    # -- StarRocks (connection target only: each call authenticates with the caller's JWT)
    starrocks_host: str = "localhost"
    starrocks_port: int = Field(9030, ge=1, le=65535)
    starrocks_db: str | None = None
    starrocks_connection_timeout: int = Field(10, gt=0)
    starrocks_query_timeout: int = Field(60, gt=0)
    starrocks_max_rows: int = Field(500, ge=1)

    # -- Radiant portal ------------------------------------------------------------
    # Unset: the tools backed by the portal API are not registered.
    radiant_api_url: str | None = None
    radiant_api_timeout: float = Field(60.0, gt=0)
    # Unset: the tools return no portal_url.
    radiant_portal_url: str | None = None
    # StarRocks database of the shared tables (staging_sequencing_experiment, clinvar, ...).
    radiant_shared_db: str = "radiant"

    @field_validator("oauth_required_scopes", "oauth_supported_scopes", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        return _split(value)

    @field_validator(
        "keycloak_audience", "starrocks_db", "radiant_api_url", "radiant_portal_url", mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("keycloak_realm_url", "mcp_base_url", "radiant_api_url", "radiant_portal_url")
    @classmethod
    def _no_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @field_validator("radiant_shared_db")
    @classmethod
    def _database_name(cls, value: str) -> str:
        if not _DB_NAME.match(value):
            raise ValueError(f"not a database name: {value!r}")
        return value


_current: Settings | None = None


def get_settings() -> Settings:
    """The settings in effect, read from the environment on the first call."""
    global _current
    if _current is None:
        _current = Settings()
    return _current


def use_settings(settings: Settings | None) -> None:
    """Install ``settings`` (tests); ``None`` reads the environment again on the next call."""
    global _current
    _current = settings


def load_or_exit() -> Settings:
    """``get_settings()`` for startup: an invalid environment exits with one line per error."""
    try:
        return get_settings()
    except ValidationError as exc:
        lines = [
            f"  {str(err['loc'][0]).upper() if err['loc'] else '?'}: {err['msg']}" for err in exc.errors()
        ]
        raise SystemExit("Invalid configuration:\n" + "\n".join(lines)) from None
