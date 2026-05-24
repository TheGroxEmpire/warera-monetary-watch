from __future__ import annotations

import json

import httpx
import pytest

from warera_monetary_watch.integrations.warera.client import WareraClient


@pytest.mark.asyncio
async def test_get_users_by_id_single_id_uses_non_batch_request() -> None:
    client = WareraClient(base_url="https://example.com/trpc", token="test-token")
    calls: list[tuple[str, str, object]] = []

    async def fake_get(endpoint: str, input_payload: object = None) -> object:
        calls.append(("get", endpoint, input_payload))
        return {"_id": "user-1", "username": "Alice"}

    async def fake_get_batch(endpoint: str, input_payload: object) -> list[object]:
        calls.append(("batch", endpoint, input_payload))
        return []

    client._get = fake_get  # type: ignore[method-assign]
    client._get_batch = fake_get_batch  # type: ignore[method-assign]

    try:
        result = await client.get_users_by_id([" user-1 "])
    finally:
        await client.close()

    assert result == {"user-1": {"_id": "user-1", "username": "Alice"}}
    assert calls == [("get", "user.getUserById", {"userId": "user-1"})]


def test_get_users_by_id_normalizes_empty_and_sentinel_values() -> None:
    assert WareraClient._normalize_ids(["", "  ", "None", "undefined", " user-1 ", "user-1"]) == ["user-1"]


@pytest.mark.asyncio
async def test_get_posts_trpc_input_as_json_body() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {"data": {"ok": True}}})

    client = WareraClient(base_url="https://example.com/trpc", token="test-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://example.com/trpc/",
        transport=httpx.MockTransport(handler),
    )

    try:
        result = await client._get("example.method", {"ownerId": "country-1"})
    finally:
        await client.close()

    assert result == {"ok": True}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/trpc/example.method"
    assert requests[0].url.query == b""
    assert json.loads(requests[0].content) == {"ownerId": "country-1"}


@pytest.mark.asyncio
async def test_get_batch_posts_trpc_input_body_with_batch_param() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {"result": {"data": {"_id": "country-1"}}},
                {"result": {"data": {"_id": "country-2"}}},
            ],
        )

    client = WareraClient(base_url="https://example.com/trpc", token="test-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://example.com/trpc/",
        transport=httpx.MockTransport(handler),
    )

    try:
        result = await client._get_batch(
            "country.getCountryById,country.getCountryById",
            {
                "0": {"countryId": "country-1"},
                "1": {"countryId": "country-2"},
            },
        )
    finally:
        await client.close()

    assert result == [{"_id": "country-1"}, {"_id": "country-2"}]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/trpc/country.getCountryById,country.getCountryById"
    assert requests[0].url.params["batch"] == "1"
    assert json.loads(requests[0].content) == {
        "0": {"countryId": "country-1"},
        "1": {"countryId": "country-2"},
    }
