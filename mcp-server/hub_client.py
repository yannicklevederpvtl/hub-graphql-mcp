from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from auth import AuthError, TokenProvider, build_token_provider
from config import Settings
from graphql import GraphQLError, OperationDefinitionNode, OperationType, parse

logger = logging.getLogger("hub_graphql_mcp.client")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CONNECTION_PATH = ("entityQuery", "queryEntities")


class HubError(RuntimeError):
    pass


class ReadOnlyViolation(PermissionError):
    pass


def assert_query_only(document: str) -> None:
    """Reject anything except `query` operations before it leaves the process."""
    try:
        ast = parse(document)
    except GraphQLError as exc:
        raise HubError(f"invalid GraphQL document: {exc.message}") from exc
    operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
    if not operations:
        raise HubError("GraphQL document has no operation")
    for operation in operations:
        if operation.operation is not OperationType.QUERY:
            raise ReadOnlyViolation(f"{operation.operation.value} operations are not allowed")


def dig(data: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            raise HubError(f"unexpected Hub response shape: missing {'.'.join(path)}")
        data = data[key]
    return data


class HubClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: TokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider or build_token_provider(settings)
        self._http = httpx.AsyncClient(
            verify=settings.tls_verify,
            timeout=settings.request_timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "hub-graphql-mcp/0.1"},
        )
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.max_concurrency)

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, force_refresh: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if self._token and not force_refresh and now < self._expires_at:
                return self._token
            try:
                token, ttl = await self._tokens.fetch(self._http)
            except httpx.HTTPError as exc:
                raise HubError(f"Hub authentication request failed: {type(exc).__name__}") from exc
            except AuthError as exc:
                raise HubError(str(exc)) from exc
            # Refresh a minute early; never cache for less than 15 seconds.
            self._token = token
            self._expires_at = now + max(ttl - 60, 15)
            logger.info("Obtained Hub bearer token (valid ~%ss)", ttl)
            return token

    async def query(self, document: str, variables: dict[str, Any] | None = None) -> dict:
        assert_query_only(document)
        payload = {"query": document, "variables": variables or {}}
        refreshed = False
        attempt = 0
        async with self._slots:
            while True:
                token = await self._bearer(force_refresh=refreshed and attempt == 0)
                delay: float | None = None
                try:
                    response = await self._http.post(
                        self._settings.graphql_url,
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except httpx.TransportError as exc:
                    if attempt >= self._settings.max_retries:
                        raise HubError(f"Hub unreachable: {type(exc).__name__}") from exc
                else:
                    if response.status_code == 401 and not refreshed and self._tokens.refreshable:
                        refreshed = True
                        attempt = 0
                        continue
                    if response.status_code in RETRYABLE_STATUS:
                        if attempt >= self._settings.max_retries:
                            raise HubError(f"Hub returned HTTP {response.status_code}")
                        delay = _retry_after(response)
                    else:
                        return _graphql_data(response)
                attempt += 1
                await asyncio.sleep(delay if delay is not None else _backoff(attempt))

    async def paginate(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        limit: int | None = None,
        connection_path: tuple[str, ...] = CONNECTION_PATH,
        items_key: str = "entities",
    ) -> tuple[list[dict], bool, int | None]:
        """Follow `pageInfo.endCursor`; return (items, truncated, totalCount)."""
        limit = min(limit or self._settings.max_items, self._settings.max_items)
        entities: list[dict] = []
        after: str | None = None
        total: int | None = None
        while True:
            first = min(self._settings.page_size, limit - len(entities))
            data = await self.query(document, {**(variables or {}), "first": first, "after": after})
            connection = dig(data, connection_path)
            total = connection.get("totalCount", total)
            entities.extend(connection.get(items_key) or [])
            page = connection.get("pageInfo") or {}
            has_next = bool(page.get("hasNextPage"))
            if not has_next or not page.get("endCursor"):
                return entities, False, total
            if len(entities) >= limit:
                return entities, True, total
            after = page["endCursor"]


def _graphql_data(response: httpx.Response) -> dict:
    if response.status_code in (401, 403):
        raise HubError(f"Hub rejected the request (HTTP {response.status_code}); check the "
                       "service account's role and scope")
    try:
        body = response.json()
    except ValueError as exc:
        raise HubError(f"Hub returned non-JSON response (HTTP {response.status_code})") from exc
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        messages = "; ".join(str(e.get("message", "unknown error")) for e in errors[:5])
        raise HubError(f"Hub GraphQL error: {messages}")
    if response.status_code >= 400:
        raise HubError(f"Hub returned HTTP {response.status_code}")
    data = body.get("data") if isinstance(body, dict) else None
    if data is None:
        raise HubError("Hub response has no data")
    return data


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value and value.isdigit():
        return min(float(value), 10.0)
    return None


def _backoff(attempt: int) -> float:
    return min(2 ** (attempt - 1), 8) * 0.5 + random.random() * 0.25  # noqa: S311
