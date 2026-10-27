"""Bearer-token providers for the Tanzu Hub GraphQL API.

Hub service accounts are OAuth clients: the default `client_credentials` mode exchanges the
client ID and secret at the Hub UAA token endpoint (`https://<hub-host>/auth/oauth/token`).
The `graphql` mode instead sends the Altair "Starter > 01 - Authentication" mutation pasted
into graphql/auth.graphql; `static` uses a pre-issued token for bootstrapping.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from config import Settings
from graphql import GraphQLError, OperationDefinitionNode, OperationType, parse

TOKEN_KEYS = ("accessToken", "access_token", "bearerToken", "token", "idToken", "id_token")
TTL_KEYS = ("expiresIn", "expires_in", "ttl", "ttlSeconds")
EXPIRY_KEYS = ("expiresAt", "expires_at", "expiry", "expirationTime")
ALTAIR_ID_PLACEHOLDERS = ("{{TANZU_CLIENT_ID}}", "{{HUB_CLIENT_ID}}")
ALTAIR_SECRET_PLACEHOLDERS = ("{{TANZU_CLIENT_SECRET}}", "{{HUB_CLIENT_SECRET}}")


class AuthError(RuntimeError):
    pass


class TokenProvider(Protocol):
    refreshable: bool

    async def fetch(self, http: httpx.AsyncClient) -> tuple[str, int]:
        """Return a bearer token and its lifetime in seconds."""
        ...


def dig(data: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if isinstance(data, list):
            data = data[int(key)]
        else:
            data = data[key]
    return data


def _scalar(value: Any) -> bool:
    return isinstance(value, str | int | float) and not isinstance(value, bool) and value != ""


def find_key(data: Any, keys: tuple[str, ...]) -> Any:
    """Depth-first search for the first scalar value stored under one of `keys`."""
    if isinstance(data, dict):
        for key in keys:
            if _scalar(data.get(key)):
                return data[key]
        for value in data.values():
            found = find_key(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_key(value, keys)
            if found is not None:
                return found
    return None


def jwt_ttl(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(padded)).get("exp")
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(exp, int | float):
        return None
    return int(exp - time.time())


def _ttl_from(data: Any, token: str, ttl_path: str | None, default: int) -> int:
    if ttl_path:
        return int(dig(data, ttl_path))
    ttl = find_key(data, TTL_KEYS)
    if ttl is not None:
        return int(ttl)
    expiry = find_key(data, EXPIRY_KEYS)
    if isinstance(expiry, str):
        try:
            expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            return int(expires.timestamp() - time.time())
        except ValueError:
            pass
    elif isinstance(expiry, int | float):
        epoch = expiry / 1000 if expiry > 1e12 else expiry
        return int(epoch - time.time())
    return jwt_ttl(token) or default


def _fill_template(template: str, client_id: str, client_secret: str) -> dict[str, Any]:
    try:
        parsed = json.loads(template)
    except json.JSONDecodeError as exc:
        raise AuthError("HUB_AUTH_VARIABLES must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise AuthError("HUB_AUTH_VARIABLES must be a JSON object")

    def fill(value: Any) -> Any:
        if value == "$client_id":
            return client_id
        if value == "$client_secret":
            return client_secret
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    return fill(parsed)


def _strip_bearer(token: str) -> str:
    return token[7:] if token.lower().startswith("bearer ") else token


class GraphQLMutationAuth:
    refreshable = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._document, self._variables = self._prepare(settings.auth_document_path)

    def _prepare(self, path: Path) -> tuple[str, dict[str, Any]]:
        if not path.exists():
            raise AuthError(f"{path} not found; paste the Altair authentication mutation there")
        client_id = self._settings.client_id or ""
        client_secret = self._settings.client_secret or ""
        document = path.read_text(encoding="utf-8")
        # Altair placeholders sit inside "..." literals; insert GraphQL-escaped content only.
        for placeholder in ALTAIR_ID_PLACEHOLDERS:
            document = document.replace(placeholder, json.dumps(client_id)[1:-1])
        for placeholder in ALTAIR_SECRET_PLACEHOLDERS:
            document = document.replace(placeholder, json.dumps(client_secret)[1:-1])
        try:
            ast = parse(document)
        except GraphQLError as exc:
            raise AuthError(
                f"{path.name} is not a GraphQL document yet; paste the "
                "'Starter > 01 - Authentication' mutation from Hub's Altair client"
            ) from exc
        operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
        if len(operations) != 1 or operations[0].operation is not OperationType.MUTATION:
            raise AuthError(f"{path.name} must contain exactly one mutation")
        if self._settings.auth_variables:
            return document, _fill_template(
                self._settings.auth_variables, client_id, client_secret
            )
        variables: dict[str, Any] = {}
        for definition in operations[0].variable_definitions or ():
            name = definition.variable.name.value
            lowered = name.lower()
            if "secret" in lowered or "password" in lowered:
                variables[name] = client_secret
            elif "id" in lowered or "client" in lowered:
                variables[name] = client_id
            else:
                raise AuthError(
                    f"cannot map authentication variable ${name}; set HUB_AUTH_VARIABLES, e.g. "
                    '{"input": {"clientId": "$client_id", "clientSecret": "$client_secret"}}'
                )
        return document, variables

    async def fetch(self, http: httpx.AsyncClient) -> tuple[str, int]:
        response = await http.post(
            self._settings.graphql_url,
            json={"query": self._document, "variables": self._variables},
        )
        if response.status_code >= 400:
            raise AuthError(f"Hub authentication failed with HTTP {response.status_code}")
        body = response.json()
        if body.get("errors") or not body.get("data"):
            raise AuthError("Hub authentication mutation returned errors")
        data = body["data"]
        token = dig(data, self._settings.token_path) if self._settings.token_path else find_key(
            data, TOKEN_KEYS
        )
        if not isinstance(token, str) or not token:
            raise AuthError(
                "no token found in the authentication response; set HUB_AUTH_TOKEN_PATH"
            )
        token = _strip_bearer(token)
        ttl = _ttl_from(data, token, self._settings.ttl_path, self._settings.default_token_ttl)
        return token, ttl


class ClientCredentialsAuth:
    """Hub service accounts are OAuth clients of the Hub UAA (`/auth/oauth/token`)."""

    refreshable = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self, http: httpx.AsyncClient) -> tuple[str, int]:
        response = await http.post(
            self._settings.token_url or "",
            data={
                "grant_type": "client_credentials",
                "client_id": self._settings.client_id or "",
                "client_secret": self._settings.client_secret or "",
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise AuthError(f"token endpoint returned HTTP {response.status_code}")
        body = response.json()
        token = body.get("access_token")
        if not token:
            raise AuthError("token endpoint response has no access_token")
        ttl = body.get("expires_in") or jwt_ttl(token) or self._settings.default_token_ttl
        return token, int(ttl)


class StaticTokenAuth:
    refreshable = False

    def __init__(self, settings: Settings) -> None:
        self._token = _strip_bearer(settings.static_token or "")

    async def fetch(self, http: httpx.AsyncClient) -> tuple[str, int]:
        return self._token, jwt_ttl(self._token) or 3600


def build_token_provider(settings: Settings) -> TokenProvider:
    if settings.auth_mode == "client_credentials":
        return ClientCredentialsAuth(settings)
    if settings.auth_mode == "static":
        return StaticTokenAuth(settings)
    return GraphQLMutationAuth(settings)
