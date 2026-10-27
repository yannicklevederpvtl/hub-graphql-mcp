"""Optional live MySQL topology, which Hub's inventory graph does not carry.

Calls a read-only HTTP endpoint built from MYSQL_DEEP_STATUS_URL_TEMPLATE (for example the
existing dashboard app's MySQL status route, or a proxy `/v0/backends` endpoint) with the
service instance GUID substituted for `{guid}`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from config import Settings
from health import summarize_backends

logger = logging.getLogger("hub_graphql_mcp.mysql_deep")


class MySQLDeepStatus:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._template = settings.mysql_deep_url_template or ""
        self._http = http
        self._auth = (
            (settings.mysql_deep_username, settings.mysql_deep_password)
            if settings.mysql_deep_username and settings.mysql_deep_password
            else None
        )

    async def fetch(self, guid: str) -> dict[str, Any]:
        url = self._template.replace("{guid}", quote(guid, safe=""))
        try:
            response = await self._http.get(url, auth=self._auth)
        except httpx.HTTPError as exc:
            logger.warning("Deep MySQL status unavailable for %s: %s", guid, type(exc).__name__)
            return {"available": False, "reason": f"request failed: {type(exc).__name__}"}
        if response.status_code >= 400:
            return {"available": False, "reason": f"HTTP {response.status_code}"}
        try:
            payload = response.json()
        except ValueError:
            return {"available": False, "reason": "non-JSON response"}
        backends = payload.get("backends") if isinstance(payload, dict) else payload
        summary = summarize_backends(backends)
        if summary is not None:
            return {"available": True, **summary}
        return {"available": True, "status": _status_of(payload), "raw": payload}


def _status_of(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("status", "health", "overall_status"):
            value = payload.get(key)
            if isinstance(value, str) and value.lower() in {
                "healthy", "degraded", "failed", "unknown", "in_progress"
            }:
                return value.lower()
    return "unknown"
