"""The server settings: read from the environment, validated before the server starts."""

import pytest
from pydantic import ValidationError

from radiant_mcp import settings as settings_module
from radiant_mcp.schema_catalog import render
from radiant_mcp.settings import Settings, load_or_exit


@pytest.fixture
def env(monkeypatch):
    """A clean environment holding only the variables the test sets."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("KEYCLOAK_REALM_URL", "https://kc.example/realms/radiant/")
    return monkeypatch


def test_defaults_and_values_come_from_the_environment(env):
    env.setenv("STARROCKS_PORT", "9031")
    env.setenv("OAUTH_REQUIRED_SCOPES", "openid, profile,")
    env.setenv("RADIANT_PORTAL_URL", "https://portal.example/")
    env.setenv("STARROCKS_DB", "  ")
    settings = Settings()
    assert settings.keycloak_realm_url == "https://kc.example/realms/radiant"
    assert settings.starrocks_port == 9031 and settings.starrocks_query_timeout == 60
    # "openid, profile" must not require the scope " profile".
    assert settings.oauth_required_scopes == ["openid", "profile"]
    assert settings.oauth_supported_scopes == ["openid", "offline_access"]
    assert settings.radiant_portal_url == "https://portal.example"
    assert settings.starrocks_db is None and settings.radiant_api_url is None
    assert settings.radiant_shared_db == "radiant"


@pytest.mark.parametrize("name, value", [
    ("STARROCKS_PORT", "abc"),
    ("STARROCKS_PORT", "70000"),
    ("STARROCKS_MAX_ROWS", "0"),
    ("RADIANT_API_TIMEOUT", "-1"),
    ("RADIANT_SHARED_DB", "radiant; DROP"),
])
def test_a_malformed_value_is_rejected(env, name, value):
    env.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_startup_stops_on_a_bad_environment_with_one_line_per_variable(env):
    env.delenv("KEYCLOAK_REALM_URL")
    env.setenv("STARROCKS_PORT", "abc")
    settings_module.use_settings(None)
    with pytest.raises(SystemExit) as exc:
        load_or_exit()
    message = str(exc.value)
    assert message.startswith("Invalid configuration:")
    assert "KEYCLOAK_REALM_URL: Field required" in message and "STARROCKS_PORT:" in message


def test_describe_schema_names_the_configured_shared_database(configure):
    assert "### radiant.clinvar" in render("annotations")
    configure(radiant_shared_db="radiant_qa")
    text = render("annotations")
    assert "### radiant_qa.clinvar" in text and "`radiant_qa.`" in text
    assert "radiant.clinvar" not in text
