import base64
import json
import time

import httpx
import pytest
from conftest import connection, make_settings

import hub_client
from auth import GraphQLMutationAuth, jwt_ttl
from hub_client import HubClient, HubError, ReadOnlyViolation, assert_query_only

QUERY = "query Q { entityQuery { queryEntities(first: 1) { totalCount } } }"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(hub_client.asyncio, "sleep", instant)


def jwt(exp_in: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + exp_in}).encode()
    ).decode().rstrip("=")
    return f"e30.{payload}.sig"


@pytest.mark.parametrize(
    "document",
    [
        "mutation { deleteService(id: 1) { id } }",
        "subscription { events { id } }",
        "query A { a } mutation B { b }",
    ],
)
def test_guard_rejects_non_queries(document):
    with pytest.raises(ReadOnlyViolation):
        assert_query_only(document)


def test_guard_accepts_queries_and_rejects_garbage():
    assert_query_only(QUERY)
    assert_query_only("{ __typename }")
    with pytest.raises(HubError):
        assert_query_only("not graphql")


async def test_mutation_never_reaches_the_network():
    calls = []
    client = HubClient(
        make_settings(),
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
    )
    with pytest.raises(ReadOnlyViolation):
        await client.query("mutation { deleteService(id: 1) { id } }")
    assert calls == []


def auth_settings(tmp_path, document: str, **overrides):
    path = tmp_path / "auth.graphql"
    path.write_text(document)
    return make_settings(
        auth_mode="graphql", client_id="sa-id", client_secret='s"cret',  # noqa: S106
        static_token=None, auth_document_path=path, **overrides,
    )


async def test_token_is_fetched_once_cached_and_refreshed_on_401(tmp_path):
    settings = auth_settings(
        tmp_path,
        "mutation Auth($clientId: String!, $clientSecret: String!) "
        "{ auth { token(id: $clientId, secret: $clientSecret) { accessToken expiresIn } } }",
    )
    auth_calls = []
    data_calls = []
    state = {"reject_next": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["query"].startswith("mutation"):
            auth_calls.append(body["variables"])
            n = len(auth_calls)
            return httpx.Response(200, json={
                "data": {"auth": {"token": {"accessToken": f"tok-{n}", "expiresIn": 600}}}
            })
        data_calls.append(request.headers["Authorization"])
        if state["reject_next"]:
            state["reject_next"] = False
            return httpx.Response(401)
        return httpx.Response(200, json={"data": {"ok": True}})

    client = HubClient(settings, transport=httpx.MockTransport(handler))
    assert await client.query(QUERY) == {"ok": True}
    assert await client.query(QUERY) == {"ok": True}
    assert auth_calls == [{"clientId": "sa-id", "clientSecret": 's"cret'}]

    state["reject_next"] = True
    assert await client.query(QUERY) == {"ok": True}
    assert len(auth_calls) == 2
    assert data_calls == ["Bearer tok-1", "Bearer tok-1", "Bearer tok-1", "Bearer tok-2"]


async def test_client_credentials_matches_documented_token_request():
    from urllib.parse import parse_qs

    token_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/oauth/token":
            token_requests.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "uaa-token", "expires_in": 1800})
        assert request.headers["Authorization"] == "Bearer uaa-token"
        return httpx.Response(200, json={"data": {"ok": True}})

    settings = make_settings(
        auth_mode="client_credentials", client_id="sa-id", client_secret="sa-secret",  # noqa: S106
        static_token=None, token_url="https://hub.test/auth/oauth/token",  # noqa: S106
    )
    client = HubClient(settings, transport=httpx.MockTransport(handler))
    assert await client.query(QUERY) == {"ok": True}
    assert await client.query(QUERY) == {"ok": True}
    assert token_requests == [{"grant_type": ["client_credentials"], "client_id": ["sa-id"],
                               "client_secret": ["sa-secret"]}]


def test_altair_placeholders_are_escaped(tmp_path):
    settings = auth_settings(
        tmp_path,
        'mutation { auth(clientId: "{{TANZU_CLIENT_ID}}", '
        'clientSecret: "{{TANZU_CLIENT_SECRET}}") { token } }',
    )
    provider = GraphQLMutationAuth(settings)
    assert 'clientSecret: "s\\"cret"' in provider._document


def test_auth_variables_template(tmp_path):
    settings = auth_settings(
        tmp_path,
        "mutation Auth($input: TokenInput!) { auth(input: $input) { token } }",
        auth_variables='{"input": {"clientId": "$client_id", "clientSecret": "$client_secret"}}',
    )
    provider = GraphQLMutationAuth(settings)
    assert provider._variables == {"input": {"clientId": "sa-id", "clientSecret": 's"cret'}}


def test_unfilled_auth_document_fails_with_guidance(tmp_path):
    from auth import AuthError

    with pytest.raises(AuthError, match="Altair"):
        GraphQLMutationAuth(auth_settings(tmp_path, "# paste here\n"))


def test_jwt_ttl():
    assert 290 <= jwt_ttl(jwt(300)) <= 300
    assert jwt_ttl("opaque") is None


async def test_retries_on_503_then_succeeds():
    responses = [httpx.Response(503), httpx.Response(429, headers={"Retry-After": "1"}),
                 httpx.Response(200, json={"data": {"ok": 1}})]
    client = HubClient(make_settings(), transport=httpx.MockTransport(lambda r: responses.pop(0)))
    assert await client.query(QUERY) == {"ok": 1}


async def test_gives_up_after_max_retries():
    client = HubClient(make_settings(max_retries=1),
                       transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    with pytest.raises(HubError, match="502"):
        await client.query(QUERY)


async def test_graphql_errors_raise_even_with_http_200():
    client = HubClient(make_settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": None, "errors": [
            {"message": "Cannot query field 'insights' on type 'Entity'."}]})
    ))
    with pytest.raises(HubError, match="Cannot query field"):
        await client.query(QUERY)


async def test_403_mentions_service_account_role():
    client = HubClient(make_settings(),
                       transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(HubError, match="role"):
        await client.query(QUERY)


async def test_pagination_follows_cursor_and_caps_items():
    seen_after = []

    def handler(request):
        variables = json.loads(request.content)["variables"]
        seen_after.append(variables["after"])
        n = len(seen_after)
        batch = [{"entityId": f"e{n}-{i}"} for i in range(variables["first"])]
        return httpx.Response(200, json={"data": connection(batch, True, f"c{n}", total=99)})

    client = HubClient(make_settings(page_size=2, max_items=5),
                       transport=httpx.MockTransport(handler))
    entities, truncated, total = await client.paginate(QUERY)
    assert len(entities) == 5
    assert truncated is True
    assert total == 99
    assert seen_after == [None, "c1", "c2"]


async def test_pagination_stops_on_last_page():
    pages = [connection([{"entityId": "a"}, {"entityId": "b"}], True, "c1"),
             connection([{"entityId": "c"}], False, None)]
    client = HubClient(make_settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": pages.pop(0)})))
    entities, truncated, _ = await client.paginate(QUERY)
    assert [e["entityId"] for e in entities] == ["a", "b", "c"]
    assert truncated is False
