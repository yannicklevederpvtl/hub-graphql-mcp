from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from config import Settings


def make_settings(**overrides: Any) -> Settings:
    base = Settings(
        hub_url="https://hub.test/hub",
        auth_mode="static",
        static_token="static-token",  # noqa: S106
        mcp_api_key="test-key",
        max_retries=2,
        page_size=2,
        max_items=10,
    )
    return replace(base, **overrides)


def entity(
    name: str,
    entity_type: str = "Tanzu.TAS.ServiceInstance",
    props: dict[str, Any] | None = None,
    **relations: list[dict],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entityId": f"vrn/provider:TAS/{entity_type}:{name}",
        "entityName": name,
        "entityType": entity_type,
        "lastUpdateTime": "2026-10-27T09:00:00Z",
        "properties": [{"name": k, "value": v} for k, v in (props or {}).items()],
    }
    for alias, items in relations.items():
        result[alias] = {"entities": items}
    return result


def connection(entities: list[dict], has_next: bool = False, cursor: str | None = None,
               total: int | None = None) -> dict:
    return {
        "entityQuery": {
            "queryEntities": {
                "entities": entities,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "totalCount": total if total is not None else len(entities),
            }
        }
    }


class FakeHub:
    """Stands in for HubClient: returns canned entities and records documents."""

    def __init__(self, entities: list[dict] | None = None, error: Exception | None = None):
        self.entities = entities or []
        self.error = error
        self.documents: list[str] = []
        self.http = None

    async def query(self, document: str, variables: dict | None = None) -> dict:
        self.documents.append(document)
        if self.error:
            raise self.error
        first = (variables or {}).get("first") or len(self.entities)
        return connection(self.entities[:first])

    async def paginate(self, document: str, variables: dict | None = None,
                       limit: int | None = None, **_: Any):
        self.documents.append(document)
        if self.error:
            raise self.error
        return list(self.entities), False, len(self.entities)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return make_settings()
