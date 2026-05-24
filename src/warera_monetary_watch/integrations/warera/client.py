from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx
from aiolimiter import AsyncLimiter

logger = logging.getLogger(__name__)


class WareraApiError(RuntimeError):
    """Raised when the Warera API returns an invalid or unexpected response."""


class WareraClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        rate_limit_per_minute: int = 450,
        lookup_batch_size: int = 100,
        lookup_concurrency: int = 10,
        timeout_seconds: float = 20.0,
    ) -> None:
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "x-api-key": token,
        }
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_seconds,
        )
        self._limiter = AsyncLimiter(max(rate_limit_per_minute, 1), 60)
        self._lookup_batch_size = max(1, lookup_batch_size)
        self._lookup_concurrency = max(1, lookup_concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> WareraClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def get_all_countries(self) -> list[dict[str, Any]]:
        data = await self._get("country.getAllCountries")
        if not isinstance(data, list):
            raise WareraApiError("country.getAllCountries did not return a list.")
        return [self._as_dict(item, "country.getAllCountries item") for item in data]

    async def get_regions_object(self) -> dict[str, dict[str, Any]]:
        data = await self._get("region.getRegionsObject")
        if not isinstance(data, Mapping):
            raise WareraApiError("region.getRegionsObject did not return an object.")
        return {str(key): self._as_dict(value, "region.getRegionsObject entry") for key, value in data.items()}

    async def get_wage_transactions(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"transactionType": "wage", "limit": limit}
        if cursor:
            payload["cursor"] = cursor
        data = await self._get("transaction.getPaginatedTransactions", payload)
        return self._as_dict(data, "transaction.getPaginatedTransactions response")

    async def get_users_by_id(
        self,
        user_ids: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        return await self._get_many_by_id(
            "user.getUserById",
            "userId",
            user_ids,
            batch_size=batch_size or self._lookup_batch_size,
        )

    async def get_companies_by_id(
        self,
        company_ids: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        return await self._get_many_by_id(
            "company.getById",
            "companyId",
            company_ids,
            batch_size=batch_size or self._lookup_batch_size,
        )

    async def get_workers_by_employer_user_ids(
        self,
        user_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = self._normalize_ids(user_ids)
        if not normalized_ids:
            return {}

        concurrency = min(self._lookup_concurrency, max(1, len(normalized_ids)))
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_user(user_id: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                item = await self._get("worker.getWorkers", {"userId": user_id})
                return user_id, self._as_dict(item, "worker.getWorkers response")

        results: dict[str, dict[str, Any]] = {}
        for user_id, payload in await asyncio.gather(*(fetch_user(user_id) for user_id in normalized_ids)):
            results[user_id] = payload
        return results

    async def _get_many_by_id(
        self,
        method: str,
        param_name: str,
        ids: Sequence[str],
        *,
        batch_size: int,
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = self._normalize_ids(ids)
        if not normalized_ids:
            return {}

        batch_size = max(1, batch_size)
        chunks = [
            normalized_ids[start : start + batch_size]
            for start in range(0, len(normalized_ids), batch_size)
        ]
        concurrency = min(self._lookup_concurrency, max(1, len(chunks)))
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_chunk(chunk: list[str]) -> dict[str, dict[str, Any]]:
            async with semaphore:
                chunk_results: dict[str, dict[str, Any]] = {}
                if len(chunk) == 1:
                    value = chunk[0]
                    item = await self._get(method, {param_name: value})
                    chunk_results[value] = self._as_dict(item, f"{method} response")
                    return chunk_results

                methods = ",".join(method for _ in chunk)
                input_payload = {
                    str(index): {param_name: value}
                    for index, value in enumerate(chunk)
                }
                items = await self._get_batch(methods, input_payload)
                for index, item in enumerate(items):
                    chunk_results[chunk[index]] = self._as_dict(item, f"{method} batch item")
                return chunk_results

        results: dict[str, dict[str, Any]] = {}
        for chunk_results in await asyncio.gather(*(fetch_chunk(chunk) for chunk in chunks)):
            results.update(chunk_results)
        return results

    async def _get(self, endpoint: str, input_payload: Mapping[str, Any] | None = None) -> Any:
        response_payload = await self._request_json(
            endpoint,
            json_payload=dict(input_payload or {}) if input_payload is not None else None,
        )
        if not isinstance(response_payload, Mapping):
            raise WareraApiError(f"Expected {endpoint} to return a JSON object.")
        result = response_payload.get("result")
        if not isinstance(result, Mapping) or "data" not in result:
            raise WareraApiError(f"Expected {endpoint} to contain result.data.")
        return result["data"]

    async def _get_batch(self, endpoint: str, input_payload: Mapping[str, Any]) -> list[Any]:
        response_payload = await self._request_json(
            endpoint,
            params={"batch": "1"},
            json_payload=dict(input_payload),
        )
        if not isinstance(response_payload, list):
            raise WareraApiError(f"Expected batched {endpoint} to return a JSON array.")

        results: list[Any] = []
        for item in response_payload:
            if not isinstance(item, Mapping):
                raise WareraApiError(f"Expected batched {endpoint} entry to be an object.")
            if "error" in item:
                error_message = self._extract_error_message(item)
                raise WareraApiError(f"{endpoint} batch error: {error_message}")
            result = item.get("result")
            if not isinstance(result, Mapping) or "data" not in result:
                raise WareraApiError(f"Expected batched {endpoint} entry to contain result.data.")
            results.append(result["data"])
        return results

    async def _request_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        json_payload: Mapping[str, Any] | None = None,
    ) -> Any:
        retries = 3
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._limiter:
                    request_params = dict(params or {})
                    if json_payload is None:
                        response = await self._client.get(endpoint, params=request_params)
                    else:
                        response = await self._client.post(
                            endpoint,
                            params=request_params,
                            json=dict(json_payload),
                        )
                self._log_rate_limit_headers(endpoint, response)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, Mapping) and "error" in payload:
                    raise WareraApiError(self._extract_error_message(payload))
                return payload
            except (httpx.HTTPError, ValueError, WareraApiError) as exc:
                last_error = exc
                should_retry = False
                if isinstance(exc, httpx.HTTPStatusError):
                    should_retry = exc.response.status_code >= 500 or exc.response.status_code == 429
                elif isinstance(exc, httpx.HTTPError):
                    should_retry = True
                if attempt < retries - 1 and should_retry:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
        raise WareraApiError(f"{endpoint} request failed: {last_error}") from last_error

    @staticmethod
    def _extract_error_message(payload: Mapping[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
        return "Unknown Warera API error"

    @staticmethod
    def _as_dict(value: Any, context: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise WareraApiError(f"Expected {context} to be an object.")
        return dict(cast(Mapping[str, Any], value))

    @staticmethod
    def _log_rate_limit_headers(endpoint: str, response: httpx.Response) -> None:
        limit = response.headers.get("ratelimit-limit")
        remaining = response.headers.get("ratelimit-remaining")
        reset = response.headers.get("ratelimit-reset")
        if limit or remaining or reset:
            logger.debug(
                "Warera rate limit %s: limit=%s remaining=%s reset=%s",
                endpoint,
                limit or "?",
                remaining or "?",
                reset or "?",
            )

    @staticmethod
    def _normalize_ids(ids: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in ids:
            cleaned = value.strip()
            if not cleaned:
                continue
            if cleaned.lower() in {"none", "null", "undefined"}:
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
        return normalized
