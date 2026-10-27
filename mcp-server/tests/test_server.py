import json

import pytest
from conftest import FakeHub, entity, make_settings
from fastapi.testclient import TestClient
from fastmcp import Client

from mcp_tools import build_mcp
from server import create_app
from services import HubService

EXPECTED_TOOLS = {
    "list_organizations", "list_spaces", "list_users", "list_service_instances",
    "get_service_instance", "list_services_status", "get_mysql_service_instance_status",
}
MCP_HEADERS = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"}}}


def make_service(entities=None):
    if entities is None:
        entities = [entity("payments", "Tanzu.TAS.Organization")]
    return HubService(FakeHub(entities), make_settings())


@pytest.fixture
def client():
    app = create_app(make_settings(), make_service())
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_open(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["mcp"] == "/mcp"


@pytest.mark.parametrize("path", ["/api/organizations", "/mcp"])
def test_api_key_required(client, path):
    assert client.post(path, json=INITIALIZE).status_code in (401, 405)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-api-key": "wrong"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic test-key"}).status_code == 401


@pytest.mark.parametrize(
    "headers", [{"x-api-key": "test-key"}, {"Authorization": "Bearer test-key"}]
)
def test_rest_route_with_key(client, headers):
    response = client.get("/api/organizations", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "payments"


def test_openapi_docs_are_disabled(client):
    assert client.get("/docs", headers={"x-api-key": "test-key"}).status_code == 404


def test_mcp_initialize_over_streamable_http(client):
    response = client.post("/mcp", json=INITIALIZE,
                           headers={**MCP_HEADERS, "x-api-key": "test-key"})
    assert response.status_code == 200
    assert "serverInfo" in response.text


async def test_mcp_exposes_seven_read_only_tools():
    async with Client(build_mcp(make_service())) as mcp_client:
        tools = await mcp_client.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_tool_call_returns_json():
    async with Client(build_mcp(make_service())) as mcp_client:
        result = await mcp_client.call_tool("list_organizations", {})
    payload = json.loads(result.content[0].text)
    assert payload["items"][0]["name"] == "payments"


async def test_hub_failures_are_mcp_tool_errors():
    from hub_client import HubError

    service = HubService(FakeHub(error=HubError("Hub returned HTTP 503")), make_settings())
    async with Client(build_mcp(service)) as mcp_client:
        result = await mcp_client.call_tool("list_spaces", {}, raise_on_error=False)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "Hub returned HTTP 503"


async def test_not_found_is_data_not_an_error():
    async with Client(build_mcp(make_service([]))) as mcp_client:
        result = await mcp_client.call_tool(
            "get_service_instance", {"identifier": "4f1d9a52-1c55-4a7e-9a39-2b4b0c6f0c11"}
        )
    assert result.is_error is False
    assert "no service instance found" in json.loads(result.content[0].text)["error"]
