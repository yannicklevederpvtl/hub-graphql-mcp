import json

import pytest

from config import ConfigError, load_settings

BASE = {"HUB_URL": "https://hub.test/hub/", "HUB_CLIENT_ID": "id", "HUB_CLIENT_SECRET": "s",
        "MCP_API_KEY": "k"}


def test_defaults_and_trailing_slash():
    settings = load_settings(BASE)
    assert settings.graphql_url == "https://hub.test/hub/graphql"
    assert settings.auth_mode == "client_credentials"
    assert settings.token_url == "https://hub.test/auth/oauth/token"  # noqa: S105
    assert settings.tls_verify is True


def test_credentials_from_user_provided_service():
    vcap = {"user-provided": [{"name": "hub-graphql-mcp-creds", "credentials": {
        "client_id": "vcap-id", "client_secret": "vcap-secret", "mcp_api_key": "vcap-key"}}]}
    settings = load_settings({"HUB_URL": "https://hub.test/hub",
                              "VCAP_SERVICES": json.dumps(vcap)})
    assert (settings.client_id, settings.client_secret, settings.mcp_api_key) == (
        "vcap-id", "vcap-secret", "vcap-key")


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"HUB_URL": "http://hub.test/hub"}, "https"),
        ({"HUB_URL": ""}, "HUB_URL"),
        ({"MCP_API_KEY": ""}, "MCP_API_KEY"),
        ({"HUB_CLIENT_SECRET": ""}, "HUB_CLIENT_SECRET"),
        ({"HUB_AUTH_MODE": "basic"}, "HUB_AUTH_MODE"),
        ({"HUB_TOKEN_URL": "http://hub.test/auth/oauth/token"}, "HUB_TOKEN_URL"),
        ({"HUB_FILTER_TEMPLATE": "{ field: x }"}, "HUB_FILTER_TEMPLATE"),
        ({"ENABLE_MYSQL_DEEP_STATUS": "true"}, "MYSQL_DEEP_STATUS_URL_TEMPLATE"),
        ({"MYSQL_DEEP_STATUS_URL_TEMPLATE": "http://db.example.com/{guid}"}, "https"),
    ],
)
def test_validation_errors(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**BASE, **overrides})


def test_internal_http_allowed_for_deep_status():
    settings = load_settings({**BASE, "ENABLE_MYSQL_DEEP_STATUS": "true",
                              "MYSQL_DEEP_STATUS_URL_TEMPLATE":
                                  "http://dashboard-app-hub.apps.internal:8080/api/mysql/{guid}"})
    assert settings.mysql_deep_status is True


def test_unauthenticated_requires_explicit_opt_in():
    settings = load_settings({**BASE, "MCP_API_KEY": "", "ALLOW_UNAUTHENTICATED_MCP": "true"})
    assert settings.mcp_api_key is None
