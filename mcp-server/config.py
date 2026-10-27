from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent
AUTH_MODES = ("graphql", "client_credentials", "static")
DEFAULT_FILTER_TEMPLATE = "{ field: %(field)s, operator: EQ, values: [%(value)s] }"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    hub_url: str
    auth_mode: str = "client_credentials"
    client_id: str | None = None
    client_secret: str | None = None
    static_token: str | None = None
    token_url: str | None = None
    auth_document_path: Path = BASE_DIR / "graphql" / "auth.graphql"
    auth_variables: str | None = None
    token_path: str | None = None
    ttl_path: str | None = None
    default_token_ttl: int = 300
    mcp_api_key: str | None = None
    allow_unauthenticated: bool = False
    ca_bundle: str | None = None
    request_timeout: float = 20.0
    max_retries: int = 3
    max_concurrency: int = 8
    page_size: int = 100
    max_items: int = 1000
    filter_template: str = DEFAULT_FILTER_TEMPLATE
    guid_field: str = "properties.GUID"
    insights_selection: str = ""
    mysql_deep_status: bool = False
    mysql_deep_url_template: str | None = None
    mysql_deep_username: str | None = None
    mysql_deep_password: str | None = None

    @property
    def graphql_url(self) -> str:
        return f"{self.hub_url}/graphql"

    @property
    def tls_verify(self) -> str | bool:
        if self.ca_bundle and Path(self.ca_bundle).exists():
            return self.ca_bundle
        return True


def _user_provided_credentials(env: Mapping[str, str], name: str) -> dict[str, str]:
    try:
        vcap = json.loads(env.get("VCAP_SERVICES", "{}") or "{}")
    except json.JSONDecodeError as exc:
        raise ConfigError("VCAP_SERVICES is not valid JSON") from exc
    for service in vcap.get("user-provided", []):
        if service.get("name") == name:
            return {k: str(v) for k, v in (service.get("credentials") or {}).items()}
    return {}


def _graphql_selection_file(path: Path) -> str:
    """Return a selection-set snippet from a file, ignoring comment-only content."""
    if not path.exists():
        return ""
    body = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    return body.strip()


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    creds = _user_provided_credentials(env, env.get("HUB_CREDS_SERVICE", "hub-graphql-mcp-creds"))

    def secret(env_key: str, cred_key: str) -> str | None:
        return creds.get(cred_key) or env.get(env_key) or None

    hub_url = (env.get("HUB_URL") or "").rstrip("/")
    if not hub_url:
        raise ConfigError("HUB_URL is required, for example https://hub.example.com/hub")
    if urlparse(hub_url).scheme != "https":
        raise ConfigError("HUB_URL must use https")

    auth_mode = env.get("HUB_AUTH_MODE", "client_credentials").strip().lower()
    if auth_mode not in AUTH_MODES:
        raise ConfigError(f"HUB_AUTH_MODE must be one of {', '.join(AUTH_MODES)}")
    parsed_hub = urlparse(hub_url)
    default_token_url = f"https://{parsed_hub.netloc}/auth/oauth/token"

    settings = Settings(
        hub_url=hub_url,
        auth_mode=auth_mode,
        client_id=secret("HUB_CLIENT_ID", "client_id"),
        client_secret=secret("HUB_CLIENT_SECRET", "client_secret"),
        static_token=secret("HUB_STATIC_TOKEN", "static_token"),
        token_url=env.get("HUB_TOKEN_URL") or default_token_url,
        auth_document_path=Path(
            env.get("HUB_AUTH_DOCUMENT", str(BASE_DIR / "graphql" / "auth.graphql"))
        ),
        auth_variables=env.get("HUB_AUTH_VARIABLES") or None,
        token_path=env.get("HUB_AUTH_TOKEN_PATH") or None,
        ttl_path=env.get("HUB_AUTH_TTL_PATH") or None,
        default_token_ttl=_int(env, "HUB_DEFAULT_TOKEN_TTL_SECONDS", 300, minimum=30),
        mcp_api_key=secret("MCP_API_KEY", "mcp_api_key"),
        allow_unauthenticated=_bool(env.get("ALLOW_UNAUTHENTICATED_MCP")),
        ca_bundle=env.get("CA_BUNDLE") or None,
        request_timeout=float(env.get("REQUEST_TIMEOUT_SECONDS", "20")),
        max_retries=_int(env, "MAX_RETRIES", 3, minimum=0),
        max_concurrency=_int(env, "MAX_CONCURRENCY", 8),
        page_size=min(_int(env, "PAGE_SIZE", 100), 500),
        max_items=_int(env, "MAX_ITEMS", 1000),
        filter_template=env.get("HUB_FILTER_TEMPLATE") or DEFAULT_FILTER_TEMPLATE,
        guid_field=env.get("HUB_GUID_FIELD", "properties.GUID"),
        insights_selection=_graphql_selection_file(Path(
            env.get("HUB_INSIGHTS_SELECTION_FILE", str(BASE_DIR / "graphql" / "insights.graphql"))
        )),
        mysql_deep_status=_bool(env.get("ENABLE_MYSQL_DEEP_STATUS")),
        mysql_deep_url_template=env.get("MYSQL_DEEP_STATUS_URL_TEMPLATE") or None,
        mysql_deep_username=secret("MYSQL_DEEP_USERNAME", "mysql_deep_username"),
        mysql_deep_password=secret("MYSQL_DEEP_PASSWORD", "mysql_deep_password"),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if not settings.mcp_api_key and not settings.allow_unauthenticated:
        raise ConfigError(
            "MCP_API_KEY is required (user-provided service credential mcp_api_key); set "
            "ALLOW_UNAUTHENTICATED_MCP=true only for an internal route behind MCP Gateway"
        )
    if settings.auth_mode in ("graphql", "client_credentials") and not (
        settings.client_id and settings.client_secret
    ):
        raise ConfigError(
            "HUB_CLIENT_ID and HUB_CLIENT_SECRET are required (user-provided service "
            "credentials client_id / client_secret, or environment variables)"
        )
    if settings.auth_mode == "client_credentials" and urlparse(
        settings.token_url or ""
    ).scheme != "https":
        raise ConfigError("HUB_TOKEN_URL must use https")
    if settings.auth_mode == "static" and not settings.static_token:
        raise ConfigError("HUB_STATIC_TOKEN is required when HUB_AUTH_MODE=static")
    if "%(field)s" not in settings.filter_template or "%(value)s" not in settings.filter_template:
        raise ConfigError("HUB_FILTER_TEMPLATE must contain %(field)s and %(value)s")
    if settings.mysql_deep_status and not settings.mysql_deep_url_template:
        raise ConfigError(
            "MYSQL_DEEP_STATUS_URL_TEMPLATE is required when ENABLE_MYSQL_DEEP_STATUS=true"
        )
    if settings.mysql_deep_url_template:
        if "{guid}" not in settings.mysql_deep_url_template:
            raise ConfigError("MYSQL_DEEP_STATUS_URL_TEMPLATE must contain {guid}")
        parsed = urlparse(settings.mysql_deep_url_template.replace("{guid}", "x"))
        internal = (parsed.hostname or "").endswith(".apps.internal")
        if parsed.scheme != "https" and not internal:
            raise ConfigError(
                "MYSQL_DEEP_STATUS_URL_TEMPLATE must use https (http is only allowed for "
                "container-to-container *.apps.internal routes)"
            )
