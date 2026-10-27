from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from config import Settings, load_settings
from hub_client import HubClient, HubError
from mcp_tools import build_mcp
from mysql_deep import MySQLDeepStatus
from services import HubService

MCP_PATH = "/mcp"
PROTECTED_PREFIXES = (MCP_PATH, "/api")
logger = logging.getLogger("hub_graphql_mcp")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_service(settings: Settings) -> HubService:
    hub = HubClient(settings)
    deep = MySQLDeepStatus(settings, hub.http) if settings.mysql_deep_status else None
    return HubService(hub, settings, deep)


def supplied_api_key(request: Request) -> str:
    """`x-api-key`, or `Authorization: Bearer` (how agent_buildpack sends a binding's api_key)."""
    if key := request.headers.get("x-api-key"):
        return key
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _hub_error(exc: HubError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"source": "tanzu-hub", "error": str(exc)})


def create_app(settings: Settings | None = None, service: HubService | None = None) -> FastAPI:
    if settings is None:
        configure_logging()
        settings = load_settings()
    service = service or build_service(settings)

    api_app = FastAPI(
        title="Tanzu Hub GraphQL MCP", version="0.1.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @api_app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mcp": MCP_PATH,
            "auth_mode": settings.auth_mode,
            "alerts": "hub-insights" if settings.insights_selection else "not-configured",
            "mysql_deep_status": settings.mysql_deep_status,
        }

    @api_app.get("/api/diagnostics/hub")
    async def diagnostics() -> Any:
        try:
            return await service.check_connectivity()
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/organizations")
    async def organizations(foundation: str | None = None) -> Any:
        try:
            return await service.list_organizations(foundation)
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/spaces")
    async def spaces(org: str | None = None, foundation: str | None = None) -> Any:
        try:
            return await service.list_spaces(org, foundation)
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/users")
    async def users(org: str | None = None, space: str | None = None) -> Any:
        return await service.list_users(org, space)

    @api_app.get("/api/service-instances")
    async def service_instances(
        org: str | None = None,
        space: str | None = None,
        offering: str | None = None,
        foundation: str | None = None,
    ) -> Any:
        try:
            return await service.list_service_instances(org, space, offering, foundation)
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/service-instances/{identifier:path}/mysql-status")
    async def mysql_status(identifier: str) -> Any:
        try:
            return await service.get_mysql_service_instance_status(identifier)
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/service-instances/{identifier:path}")
    async def service_instance(identifier: str) -> Any:
        try:
            return await service.get_service_instance(identifier)
        except HubError as exc:
            return _hub_error(exc)

    @api_app.get("/api/services-status")
    async def services_status(
        org: str | None = None,
        space: str | None = None,
        offering: str | None = None,
        only_unhealthy: bool = False,
    ) -> Any:
        try:
            return await service.list_services_status(org, space, offering, only_unhealthy)
        except HubError as exc:
            return _hub_error(exc)

    mcp = build_mcp(service)
    mcp_app = mcp.http_app(path=MCP_PATH, transport="streamable-http", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):
            logger.info(
                "hub-graphql-mcp started; hub=%s auth=%s mcp=%s",
                settings.hub_url, settings.auth_mode, MCP_PATH,
            )
            yield
        await service.hub.aclose()

    app = FastAPI(
        title="Tanzu Hub GraphQL MCP",
        routes=[*mcp_app.routes, *api_app.routes],
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def require_api_key(request: Request, call_next: Any) -> Any:
        expected = settings.mcp_api_key
        if expected and request.url.path.startswith(PROTECTED_PREFIXES):
            supplied = supplied_api_key(request)
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse(
                    status_code=401,
                    content={"error": "API key required"},
                    headers={"WWW-Authenticate": 'Bearer realm="hub-graphql-mcp"'},
                )
        return await call_next(request)

    return app
