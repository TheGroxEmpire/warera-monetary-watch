from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import case, delete, false, func, insert, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warera_monetary_watch.config import Settings, get_settings
from warera_monetary_watch.db.models import (
    CollectorState,
    CompanyCache,
    CountryCache,
    HourlyTaxRollup,
    RegionCache,
    UserCache,
    WageEventRaw,
)
from warera_monetary_watch.integrations.warera.client import WareraApiError, WareraClient

logger = logging.getLogger(__name__)

CURSOR_STATE_KEY = "collector.wage_cursor"
LAST_SUCCESS_KEY = "collector.last_success_at"
LAST_ERROR_KEY = "collector.last_error"
LAST_STATS_KEY = "collector.last_stats"
LAST_PROGRESS_KEY = "collector.last_progress_at"
COUNTRIES_SYNCED_AT_KEY = "collector.countries_synced_at"
REGIONS_SYNCED_AT_KEY = "collector.regions_synced_at"
RETENTION_APPLIED_AT_KEY = "collector.retention_applied_at"

MONEY_QUANTUM = Decimal("0.000001")
INVALID_LOOKUP_ID_SENTINELS = {"none", "null", "undefined"}
DB_LOOKUP_BATCH_SIZE = 1000


@dataclass(slots=True)
class CountryMeta:
    id: str
    code: str
    name: str
    income_tax: Decimal


@dataclass(slots=True)
class RegionMeta:
    id: str
    code: str
    name: str
    country_id: str
    initial_country_id: str
    resistance: Decimal | None
    resistance_max: Decimal | None


@dataclass(slots=True)
class EnrichedWageEvent:
    transaction_id: str
    created_at: datetime
    event_hour: datetime
    money: Decimal
    quantity: Decimal
    seller_user_id: str
    buyer_user_id: str
    seller_company_id: str | None
    operating_region_id: str | None
    operating_country_id: str | None
    region_initial_country_id: str | None
    owner_country_id: str | None
    item_code: str | None
    is_core_region: bool | None
    region_resistance: Decimal | None
    region_resistance_max: Decimal | None
    income_tax_rate: Decimal | None
    income_tax_money: Decimal | None
    resolved: bool
    unresolved_reason: str | None
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class CollectedTransactions:
    transactions: list[dict[str, Any]]
    newest_marker: dict[str, str] | None
    reached_cursor: bool
    exhausted_pages: bool


def parse_api_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def floor_to_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_transaction_marker(transaction: dict[str, Any]) -> dict[str, str]:
    return {
        "transaction_id": str(transaction["_id"]),
        "created_at": str(transaction["createdAt"]),
    }


def transaction_matches_marker(transaction: dict[str, Any], marker: dict[str, str] | None) -> bool:
    if not marker:
        return False
    return str(transaction.get("_id")) == marker.get("transaction_id")


def decimal_from_number(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def compute_income_tax_money(wage_money: Decimal, tax_rate: Decimal) -> Decimal:
    if tax_rate <= 0:
        return Decimal("0.000000")
    return quantize_money((wage_money * tax_rate) / Decimal("100"))


def normalize_lookup_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() in INVALID_LOOKUP_ID_SENTINELS:
        return None
    return normalized


def batched_lookup_values(values: list[Any], batch_size: int = DB_LOOKUP_BATCH_SIZE) -> list[list[Any]]:
    batches: list[list[Any]] = []
    seen: set[Any] = set()
    current: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        current.append(value)
        if len(current) >= batch_size:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


async def load_rows_by_in_batches(
    session: AsyncSession,
    model: Any,
    column: Any,
    values: list[Any],
) -> list[Any]:
    rows: list[Any] = []
    for batch in batched_lookup_values(values):
        result = await session.execute(select(model).where(column.in_(batch)))
        rows.extend(result.scalars())
    return rows


async def fetch_existing_companies_by_id(
    client: WareraClient,
    company_ids: list[str],
    *,
    batch_size: int,
) -> dict[str, dict[str, Any]]:
    effective_batch_size = max(1, batch_size)

    async def fetch_batch(ids: list[str]) -> dict[str, dict[str, Any]]:
        try:
            return await client.get_companies_by_id(ids, batch_size=min(effective_batch_size, len(ids)))
        except WareraApiError as exc:
            if "not found" not in str(exc).lower():
                raise

        if len(ids) == 1:
            logger.warning("Skipping missing company during refresh: %s", ids[0])
            return {}

        midpoint = len(ids) // 2
        companies = await fetch_batch(ids[:midpoint])
        companies.update(await fetch_batch(ids[midpoint:]))
        return companies

    companies: dict[str, dict[str, Any]] = {}
    company_batches = batched_lookup_values(company_ids, effective_batch_size)
    concurrency = min(
        max(1, int(getattr(client, "_lookup_concurrency", 1))),
        max(1, len(company_batches)),
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_top_level_batch(ids: list[str]) -> dict[str, dict[str, Any]]:
        async with semaphore:
            return await fetch_batch(ids)

    for batch_result in await asyncio.gather(
        *(fetch_top_level_batch(company_batch) for company_batch in company_batches)
    ):
        companies.update(batch_result)
    return companies


def find_company_id_for_wage(
    *,
    seller_user_id: str,
    buyer_user_id: str,
    created_at: datetime,
    wage_money: Decimal,
    quantity: Decimal,
    employer_workers: dict[str, Any] | None,
) -> str | None:
    if not employer_workers:
        return None

    candidates: list[dict[str, Any]] = []
    for group in employer_workers.get("workersPerCompany") or []:
        if not isinstance(group, dict):
            continue
        company = group.get("company")
        company_id = normalize_lookup_id(company.get("_id")) if isinstance(company, dict) else None
        if company_id is None:
            continue
        for worker in group.get("workers") or []:
            if not isinstance(worker, dict):
                continue
            if normalize_lookup_id(worker.get("user")) != seller_user_id:
                continue
            if normalize_lookup_id(worker.get("employer")) != buyer_user_id:
                continue
            joined_at_value = worker.get("joinedAt")
            if joined_at_value:
                try:
                    joined_at = parse_api_datetime(str(joined_at_value))
                except ValueError:
                    continue
                if joined_at > created_at:
                    continue
            candidate = dict(worker)
            candidate["company"] = normalize_lookup_id(worker.get("company")) or company_id
            candidates.append(candidate)

    if not candidates:
        return None
    if len(candidates) == 1:
        return normalize_lookup_id(candidates[0].get("company"))

    expected_wage = quantize_money(wage_money / quantity) if quantity else None
    if expected_wage is not None:
        wage_matches = [
            candidate
            for candidate in candidates
            if quantize_money(decimal_from_number(candidate.get("wage"))) == expected_wage
        ]
        if len(wage_matches) == 1:
            return normalize_lookup_id(wage_matches[0].get("company"))

    return None


def enrich_wage_transactions(
    transactions: list[dict[str, Any]],
    seller_users: dict[str, dict[str, Any]],
    buyer_users: dict[str, dict[str, Any]],
    workers_by_employer_user_id: dict[str, dict[str, Any]],
    companies: dict[str, dict[str, Any]],
    countries_by_id: dict[str, CountryMeta],
    regions_by_id: dict[str, RegionMeta],
) -> list[EnrichedWageEvent]:
    enriched: list[EnrichedWageEvent] = []

    for transaction in transactions:
        transaction_id = str(transaction.get("_id", ""))
        created_at = parse_api_datetime(str(transaction["createdAt"]))
        money = quantize_money(decimal_from_number(transaction.get("money")))
        quantity = quantize_money(decimal_from_number(transaction.get("quantity")))
        seller_user_id = normalize_lookup_id(transaction.get("sellerId")) or ""
        buyer_user_id = normalize_lookup_id(transaction.get("buyerId")) or ""
        raw_payload = dict(transaction)

        seller_company_id: str | None = None
        operating_region_id: str | None = None
        operating_country_id: str | None = None
        region_initial_country_id: str | None = None
        owner_country_id: str | None = None
        item_code: str | None = None
        is_core_region: bool | None = None
        region_resistance: Decimal | None = None
        region_resistance_max: Decimal | None = None
        income_tax_rate: Decimal | None = None
        income_tax_money: Decimal | None = None
        resolved = False
        unresolved_reason: str | None = None

        seller_user = seller_users.get(seller_user_id) if seller_user_id else None
        buyer_user = buyer_users.get(buyer_user_id) if buyer_user_id else None

        if not seller_user_id:
            unresolved_reason = "missing_seller_user_id"
        elif not buyer_user_id:
            unresolved_reason = "missing_buyer_user_id"
        elif seller_user is None:
            unresolved_reason = "missing_seller_user"
        elif buyer_user is None:
            unresolved_reason = "missing_buyer_user"
        else:
            company_id = find_company_id_for_wage(
                seller_user_id=seller_user_id,
                buyer_user_id=buyer_user_id,
                created_at=created_at,
                wage_money=money,
                quantity=quantity,
                employer_workers=workers_by_employer_user_id.get(buyer_user_id),
            )
            if company_id is None:
                unresolved_reason = "missing_matching_worker_company"
            else:
                seller_company_id = company_id
                company = companies.get(company_id)
                if company is None:
                    unresolved_reason = "missing_company_detail"
                else:
                    region_id = normalize_lookup_id(company.get("region"))
                    buyer_country_id = normalize_lookup_id(buyer_user.get("country"))
                    if region_id is None:
                        unresolved_reason = "missing_region"
                    elif buyer_country_id is None:
                        unresolved_reason = "missing_owner_country"
                    else:
                        region = regions_by_id.get(region_id)
                        owner_country = countries_by_id.get(buyer_country_id)
                        if region is None:
                            unresolved_reason = "unknown_region"
                        elif owner_country is None:
                            unresolved_reason = "unknown_owner_country"
                        else:
                            country = countries_by_id.get(region.country_id)
                            if country is None:
                                unresolved_reason = "unknown_operating_country"
                            else:
                                operating_region_id = region.id
                                operating_country_id = region.country_id
                                region_initial_country_id = region.initial_country_id
                                owner_country_id = owner_country.id
                                item_code_value = company.get("itemCode")
                                item_code = str(item_code_value) if item_code_value else None
                                is_core_region = region.country_id == region.initial_country_id
                                region_resistance = region.resistance
                                region_resistance_max = region.resistance_max
                                income_tax_rate = decimal_from_number(
                                    transaction.get("incomeTaxRate", country.income_tax)
                                )
                                income_tax_money = compute_income_tax_money(money, income_tax_rate)
                                resolved = True

        enriched.append(
            EnrichedWageEvent(
                transaction_id=transaction_id,
                created_at=created_at,
                event_hour=floor_to_hour(created_at),
                money=money,
                quantity=quantity,
                seller_user_id=seller_user_id,
                buyer_user_id=buyer_user_id,
                seller_company_id=seller_company_id,
                operating_region_id=operating_region_id,
                operating_country_id=operating_country_id,
                region_initial_country_id=region_initial_country_id,
                owner_country_id=owner_country_id,
                item_code=item_code,
                is_core_region=is_core_region,
                region_resistance=region_resistance,
                region_resistance_max=region_resistance_max,
                income_tax_rate=income_tax_rate,
                income_tax_money=income_tax_money,
                resolved=resolved,
                unresolved_reason=unresolved_reason,
                raw_payload=raw_payload,
            )
        )

    return enriched


class WageCollectorService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory

    async def run_forever(self) -> None:
        while True:
            try:
                stats = await self.run_once()
                logger.info("Collector cycle complete: %s", stats)
            except Exception as exc:  # pragma: no cover - defensive runtime logging
                logger.exception("Collector cycle failed")
                async with self._session_factory() as session:
                    await self._set_state_value(session, LAST_ERROR_KEY, str(exc))
                    await session.commit()
            await asyncio.sleep(self._settings.collector_poll_interval_seconds)

    async def run_once(self) -> dict[str, Any]:
        async with self._session_factory() as session:
            async with WareraClient(
                base_url=self._settings.warera_api_base_url,
                token=self._settings.warera_api_token,
                rate_limit_per_minute=self._settings.warera_api_rate_limit_per_minute,
                lookup_batch_size=self._settings.warera_api_lookup_batch_size,
                lookup_concurrency=self._settings.warera_api_lookup_concurrency,
            ) as client:
                await self._sync_metadata_if_needed(session, client)

                cursor_marker = await self._get_state_json(session, CURSOR_STATE_KEY)
                if cursor_marker is None:
                    latest_page = await client.get_wage_transactions(limit=1)
                    items = latest_page.get("items", [])
                    if items:
                        await self._set_state_json(session, CURSOR_STATE_KEY, build_transaction_marker(items[0]))
                    now = datetime.now(tz=UTC).isoformat()
                    await self._set_state_value(session, LAST_SUCCESS_KEY, now)
                    await self._set_state_json(
                        session,
                        LAST_STATS_KEY,
                        {
                            "initialized": True,
                            "new_transactions": 0,
                            "resolved_transactions": 0,
                            "unresolved_transactions": 0,
                        },
                    )
                    await session.commit()
                    return {"initialized": True, "new_transactions": 0}

                countries_by_id = await self._load_country_map(session)
                regions_by_id = await self._load_region_map(session)

                affected_hours: set[datetime] = set()

                ingest_stats = await self._collect_and_ingest_new_transactions(
                    session,
                    client,
                    countries_by_id,
                    regions_by_id,
                    cursor_marker,
                )
                affected_hours.update(ingest_stats["affected_hours"])
                resolved_count = ingest_stats["resolved_transactions"]
                unresolved_count = ingest_stats["unresolved_transactions"]
                new_transaction_count = ingest_stats["new_transactions"]
                if ingest_stats["reached_cursor"] and ingest_stats["newest_marker"] is not None:
                    await self._set_state_json(session, CURSOR_STATE_KEY, ingest_stats["newest_marker"])
                elif ingest_stats["exhausted_pages"]:
                    # Safeguard: handle stale cursors and retrace all missing data
                    cursor_date_str = cursor_marker.get("created_at")
                    cursor_txn_id = cursor_marker.get("transaction_id")
                    
                    # If this is our fallback sentinel cursor from a previous stale reset,
                    # move forward to newest data now that we've retraced
                    if cursor_txn_id == "0" and ingest_stats["newest_marker"] is not None:
                        logger.warning(
                            "Retrace of missing data complete; advancing to %s",
                            ingest_stats["newest_marker"].get("created_at"),
                        )
                        await self._set_state_json(session, CURSOR_STATE_KEY, ingest_stats["newest_marker"])
                    elif cursor_date_str:
                        try:
                            cursor_date = parse_api_datetime(cursor_date_str)
                            cursor_age = datetime.now(tz=UTC) - cursor_date
                            if cursor_age > timedelta(hours=24):
                                # Cursor is too old - retrace from the earliest transaction in database
                                # to ensure complete accuracy from API's beginning
                                result = await session.execute(
                                    select(func.min(WageEventRaw.created_at))
                                )
                                earliest_date = result.scalar()
                                # Use database minimum, or fallback to far past if DB empty
                                if earliest_date:
                                    fallback_time = earliest_date - timedelta(days=1)
                                else:
                                    fallback_time = datetime(2026, 4, 20, tzinfo=UTC)
                                fallback_marker = {
                                    "transaction_id": "0",
                                    "created_at": fallback_time.isoformat(),
                                }
                                logger.warning(
                                    "Cursor is %s old and pages exhausted; resetting to %s (earliest known: %s) to retrace all missing data",
                                    cursor_age,
                                    fallback_time,
                                    earliest_date,
                                )
                                await self._set_state_json(session, CURSOR_STATE_KEY, fallback_marker)
                            else:
                                logger.warning(
                                    "Collector hit max pages before reaching previous cursor; keeping cursor at %s",
                                    cursor_marker,
                                )
                        except (ValueError, TypeError):
                            logger.warning(
                                "Collector hit max pages before reaching previous cursor; keeping cursor at %s",
                                cursor_marker,
                            )
                    else:
                        logger.warning(
                            "Collector hit max pages before reaching previous cursor; keeping cursor at %s",
                            cursor_marker,
                        )

                unresolved_hours = await self._resolve_unresolved_events(
                    session,
                    client,
                    countries_by_id,
                    regions_by_id,
                )
                if unresolved_hours:
                    affected_hours.update(unresolved_hours)
                    await self._rebuild_rollups(session, unresolved_hours)
                    await session.commit()

                await self._apply_retention_if_needed(session)

                now = datetime.now(tz=UTC).isoformat()
                await self._set_state_value(session, LAST_SUCCESS_KEY, now)
                await self._set_state_value(session, LAST_ERROR_KEY, "")
                stats = {
                    "initialized": False,
                    "new_transactions": new_transaction_count,
                    "resolved_transactions": resolved_count,
                    "unresolved_transactions": unresolved_count,
                    "affected_hours": len(affected_hours),
                    "reached_cursor": ingest_stats["reached_cursor"],
                    "exhausted_pages": ingest_stats["exhausted_pages"],
                    "started_at": ingest_stats["started_at"],
                    "pages_scanned": ingest_stats["pages_scanned"],
                    "max_pages": ingest_stats["max_pages"],
                }
                await self._set_state_json(session, LAST_STATS_KEY, stats)
                await session.commit()
                return stats

    async def _collect_new_transactions(
        self,
        client: WareraClient,
        cursor_marker: dict[str, str],
    ) -> CollectedTransactions:
        newest_marker: dict[str, str] | None = None
        cursor: str | None = None
        collected: list[dict[str, Any]] = []
        reached_cursor = False
        exhausted_pages = True

        for _ in range(self._settings.collector_max_pages_per_cycle):
            page = await client.get_wage_transactions(limit=100, cursor=cursor)
            items = page.get("items", [])
            if not items:
                exhausted_pages = False
                break

            if newest_marker is None:
                newest_marker = build_transaction_marker(items[0])

            stop = False
            for item in items:
                if transaction_matches_marker(item, cursor_marker):
                    reached_cursor = True
                    stop = True
                    break
                collected.append(item)

            if stop:
                exhausted_pages = False
                break
            if not page.get("nextCursor"):
                exhausted_pages = False
                break
            cursor = str(page["nextCursor"])

        collected.reverse()
        return CollectedTransactions(
            transactions=collected,
            newest_marker=newest_marker,
            reached_cursor=reached_cursor,
            exhausted_pages=exhausted_pages,
        )

    async def _collect_and_ingest_new_transactions(
        self,
        session: AsyncSession,
        client: WareraClient,
        countries_by_id: dict[str, CountryMeta],
        regions_by_id: dict[str, RegionMeta],
        cursor_marker: dict[str, str],
    ) -> dict[str, Any]:
        newest_marker: dict[str, str] | None = None
        cursor: str | None = None
        batch: list[dict[str, Any]] = []
        affected_hours: set[datetime] = set()
        reached_cursor = False
        exhausted_pages = True
        cycle_started_at = datetime.now(tz=UTC)
        pages_scanned = 0
        new_transactions = 0
        resolved_transactions = 0
        unresolved_transactions = 0

        async def save_progress(*, stage: str, last_batch_transactions: int = 0) -> None:
            now = datetime.now(tz=UTC).isoformat()
            await self._set_state_value(session, LAST_PROGRESS_KEY, now)
            await self._set_state_value(session, LAST_ERROR_KEY, "")
            await self._set_state_json(
                session,
                LAST_STATS_KEY,
                {
                    "initialized": False,
                    "in_progress": True,
                    "stage": stage,
                    "started_at": cycle_started_at.isoformat(),
                    "updated_at": now,
                    "pages_scanned": pages_scanned,
                    "max_pages": self._settings.collector_max_pages_per_cycle,
                    "new_transactions": new_transactions,
                    "resolved_transactions": resolved_transactions,
                    "unresolved_transactions": unresolved_transactions,
                    "affected_hours": len(affected_hours),
                    "last_batch_transactions": last_batch_transactions,
                    "reached_cursor": reached_cursor,
                    "exhausted_pages": False,
                },
            )
            await session.commit()

        async def flush_batch() -> None:
            nonlocal resolved_transactions, unresolved_transactions
            if not batch:
                return
            batch_size = len(batch)
            await save_progress(stage="enriching_batch", last_batch_transactions=batch_size)
            stats = await self._ingest_transactions(
                session,
                client,
                countries_by_id,
                regions_by_id,
                list(batch),
            )
            batch_hours = stats["affected_hours"]
            affected_hours.update(batch_hours)
            resolved_transactions += stats["resolved_transactions"]
            unresolved_transactions += stats["unresolved_transactions"]
            if batch_hours:
                await self._rebuild_rollups(session, batch_hours)
            batch.clear()
            await save_progress(stage="committed_batch", last_batch_transactions=batch_size)
            await session.commit()
            logger.info(
                "Committed collector ingest batch: resolved=%s unresolved=%s affected_hours=%s",
                stats["resolved_transactions"],
                stats["unresolved_transactions"],
                len(batch_hours),
            )

        for _ in range(self._settings.collector_max_pages_per_cycle):
            page = await client.get_wage_transactions(limit=100, cursor=cursor)
            pages_scanned += 1
            items = page.get("items", [])
            if not items:
                exhausted_pages = False
                break

            if newest_marker is None:
                newest_marker = build_transaction_marker(items[0])

            stop = False
            for item in items:
                if transaction_matches_marker(item, cursor_marker):
                    reached_cursor = True
                    stop = True
                    break
                batch.append(item)
                new_transactions += 1
                if len(batch) >= self._settings.collector_ingest_batch_size:
                    await flush_batch()

            if stop:
                exhausted_pages = False
                break
            if not page.get("nextCursor"):
                exhausted_pages = False
                break
            cursor = str(page["nextCursor"])
            await save_progress(stage="scanning_pages")

        await flush_batch()
        return {
            "newest_marker": newest_marker,
            "new_transactions": new_transactions,
            "resolved_transactions": resolved_transactions,
            "unresolved_transactions": unresolved_transactions,
            "affected_hours": affected_hours,
            "reached_cursor": reached_cursor,
            "exhausted_pages": exhausted_pages,
            "started_at": cycle_started_at.isoformat(),
            "pages_scanned": pages_scanned,
            "max_pages": self._settings.collector_max_pages_per_cycle,
        }

    async def _ingest_transactions(
        self,
        session: AsyncSession,
        client: WareraClient,
        countries_by_id: dict[str, CountryMeta],
        regions_by_id: dict[str, RegionMeta],
        transactions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        seller_ids = sorted(
            {
                seller_id
                for item in transactions
                if (seller_id := normalize_lookup_id(item.get("sellerId"))) is not None
            }
        )
        buyer_ids = sorted(
            {
                buyer_id
                for item in transactions
                if (buyer_id := normalize_lookup_id(item.get("buyerId"))) is not None
            }
        )

        users = await self._load_users_with_cache(session, client, sorted({*seller_ids, *buyer_ids}))
        seller_users = {user_id: users[user_id] for user_id in seller_ids if user_id in users}
        buyer_users = {user_id: users[user_id] for user_id in buyer_ids if user_id in users}
        await self._upsert_users(session, users)

        workers_by_employer_user_id = await client.get_workers_by_employer_user_ids(buyer_ids)
        company_ids = {
            company_id
            for item in transactions
            if (seller_id := normalize_lookup_id(item.get("sellerId"))) is not None
            and (buyer_id := normalize_lookup_id(item.get("buyerId"))) is not None
            and (
                company_id := find_company_id_for_wage(
                    seller_user_id=seller_id,
                    buyer_user_id=buyer_id,
                    created_at=parse_api_datetime(str(item["createdAt"])),
                    wage_money=quantize_money(decimal_from_number(item.get("money"))),
                    quantity=quantize_money(decimal_from_number(item.get("quantity"))),
                    employer_workers=workers_by_employer_user_id.get(buyer_id),
                )
            )
            is not None
        }
        companies = await self._load_companies_with_cache(session, client, sorted(company_ids))
        await self._upsert_companies(session, companies)

        enriched = enrich_wage_transactions(
            transactions,
            seller_users=seller_users,
            buyer_users=buyer_users,
            workers_by_employer_user_id=workers_by_employer_user_id,
            companies=companies,
            countries_by_id=countries_by_id,
            regions_by_id=regions_by_id,
        )
        existing = await self._load_existing_events(session, [event.transaction_id for event in enriched])

        resolved_transactions = 0
        unresolved_transactions = 0
        affected_hours: set[datetime] = set()
        for event in enriched:
            model = existing.get(event.transaction_id)
            if not event.resolved:
                unresolved_transactions += 1
                if model is not None and model.resolved:
                    # A temporary lookup miss while replaying pages must not erase
                    # a previously resolved historical event.
                    continue
                if model is None:
                    model = WageEventRaw(transaction_id=event.transaction_id)
                    session.add(model)
                model.created_at = event.created_at
                model.event_hour = event.event_hour
                model.money = event.money
                model.quantity = event.quantity
                model.seller_user_id = event.seller_user_id
                model.buyer_user_id = event.buyer_user_id
                model.seller_company_id = event.seller_company_id
                model.operating_region_id = event.operating_region_id
                model.operating_country_id = event.operating_country_id
                model.region_initial_country_id = event.region_initial_country_id
                model.owner_country_id = event.owner_country_id
                model.item_code = event.item_code
                model.is_core_region = event.is_core_region
                model.region_resistance = event.region_resistance
                model.region_resistance_max = event.region_resistance_max
                model.income_tax_rate = event.income_tax_rate
                model.income_tax_money = event.income_tax_money
                model.resolved = False
                model.unresolved_reason = event.unresolved_reason
                model.raw_payload = event.raw_payload
                model.resolved_at = None
                continue

            if model is None:
                model = WageEventRaw(transaction_id=event.transaction_id)
                session.add(model)
            elif model.resolved:
                # Replayed pages can arrive long after company locations or country tax rates
                # have changed. Keep the event-time enrichment captured on first resolution.
                continue

            model.created_at = event.created_at
            model.event_hour = event.event_hour
            model.money = event.money
            model.quantity = event.quantity
            model.seller_user_id = event.seller_user_id
            model.buyer_user_id = event.buyer_user_id
            model.seller_company_id = event.seller_company_id
            model.operating_region_id = event.operating_region_id
            model.operating_country_id = event.operating_country_id
            model.region_initial_country_id = event.region_initial_country_id
            model.owner_country_id = event.owner_country_id
            model.item_code = event.item_code
            model.is_core_region = event.is_core_region
            model.region_resistance = event.region_resistance
            model.region_resistance_max = event.region_resistance_max
            model.income_tax_rate = event.income_tax_rate
            model.income_tax_money = event.income_tax_money
            model.resolved = event.resolved
            model.unresolved_reason = event.unresolved_reason
            model.raw_payload = {}
            model.resolved_at = datetime.now(tz=UTC) if event.resolved else None

            resolved_transactions += 1
            affected_hours.add(event.event_hour)

        return {
            "resolved_transactions": resolved_transactions,
            "unresolved_transactions": unresolved_transactions,
            "affected_hours": affected_hours,
        }

    async def _resolve_unresolved_events(
        self,
        session: AsyncSession,
        client: WareraClient,
        countries_by_id: dict[str, CountryMeta],
        regions_by_id: dict[str, RegionMeta],
    ) -> set[datetime]:
        result = await session.execute(
            select(WageEventRaw)
            .where(WageEventRaw.resolved.is_(False))
            .order_by(WageEventRaw.created_at.desc())
            .limit(250)
        )
        unresolved_models = list(result.scalars())
        if not unresolved_models:
            return set()

        transactions = [dict(model.raw_payload) for model in unresolved_models]
        seller_ids = sorted(
            {
                seller_id
                for model in unresolved_models
                if (seller_id := normalize_lookup_id(model.seller_user_id)) is not None
            }
        )
        buyer_ids = sorted(
            {
                buyer_id
                for model in unresolved_models
                if (buyer_id := normalize_lookup_id(model.buyer_user_id)) is not None
            }
        )

        users = await self._load_users_with_cache(session, client, sorted({*seller_ids, *buyer_ids}))
        seller_users = {user_id: users[user_id] for user_id in seller_ids if user_id in users}
        buyer_users = {user_id: users[user_id] for user_id in buyer_ids if user_id in users}
        await self._upsert_users(session, users)

        workers_by_employer_user_id = await client.get_workers_by_employer_user_ids(buyer_ids)
        company_ids = {
            company_id
            for item in transactions
            if (seller_id := normalize_lookup_id(item.get("sellerId"))) is not None
            and (buyer_id := normalize_lookup_id(item.get("buyerId"))) is not None
            and (
                company_id := find_company_id_for_wage(
                    seller_user_id=seller_id,
                    buyer_user_id=buyer_id,
                    created_at=parse_api_datetime(str(item["createdAt"])),
                    wage_money=quantize_money(decimal_from_number(item.get("money"))),
                    quantity=quantize_money(decimal_from_number(item.get("quantity"))),
                    employer_workers=workers_by_employer_user_id.get(buyer_id),
                )
            )
            is not None
        }
        companies = await self._load_companies_with_cache(session, client, sorted(company_ids))
        await self._upsert_companies(session, companies)

        enriched = enrich_wage_transactions(
            transactions,
            seller_users=seller_users,
            buyer_users=buyer_users,
            workers_by_employer_user_id=workers_by_employer_user_id,
            companies=companies,
            countries_by_id=countries_by_id,
            regions_by_id=regions_by_id,
        )
        affected_hours: set[datetime] = set()
        models_by_id = {model.transaction_id: model for model in unresolved_models}
        for event in enriched:
            model = models_by_id.get(event.transaction_id)
            if model is None:
                continue
            model.seller_company_id = event.seller_company_id
            model.operating_region_id = event.operating_region_id
            model.operating_country_id = event.operating_country_id
            model.region_initial_country_id = event.region_initial_country_id
            model.owner_country_id = event.owner_country_id
            model.item_code = event.item_code
            model.is_core_region = event.is_core_region
            model.region_resistance = event.region_resistance
            model.region_resistance_max = event.region_resistance_max
            model.income_tax_rate = event.income_tax_rate
            model.income_tax_money = event.income_tax_money
            model.resolved = event.resolved
            model.unresolved_reason = event.unresolved_reason
            model.resolved_at = datetime.now(tz=UTC) if event.resolved else None
            if event.resolved:
                model.raw_payload = {}
                affected_hours.add(event.event_hour)
            else:
                model.raw_payload = event.raw_payload
        return affected_hours

    async def _rebuild_rollups(self, session: AsyncSession, hours: set[datetime]) -> None:
        normalized_hours = sorted({floor_to_hour(hour) for hour in hours})
        if not normalized_hours:
            return

        for hour_batch in batched_lookup_values(normalized_hours):
            base_filters = (
                WageEventRaw.resolved.is_(True),
                WageEventRaw.event_hour.in_(hour_batch),
                WageEventRaw.operating_country_id.is_not(None),
                WageEventRaw.region_initial_country_id.is_not(None),
                WageEventRaw.owner_country_id.is_not(None),
                WageEventRaw.item_code.is_not(None),
                WageEventRaw.income_tax_money.is_not(None),
            )
            is_core_region = func.coalesce(WageEventRaw.is_core_region, false())
            resistance_ratio = case(
                (
                    (WageEventRaw.region_resistance.is_(None))
                    | (WageEventRaw.region_resistance_max.is_(None))
                    | (WageEventRaw.region_resistance_max <= 0),
                    Decimal("0"),
                ),
                (WageEventRaw.region_resistance < 0, Decimal("0")),
                (WageEventRaw.region_resistance > WageEventRaw.region_resistance_max, Decimal("1")),
                else_=WageEventRaw.region_resistance / WageEventRaw.region_resistance_max,
            )
            core_owner_share = resistance_ratio * Decimal("0.4")
            occupier_share = Decimal("1") - core_owner_share

            operating_entries = (
                select(
                    WageEventRaw.event_hour.label("hour_start"),
                    WageEventRaw.operating_country_id.label("recipient_country_id"),
                    WageEventRaw.item_code.label("item_code"),
                    WageEventRaw.owner_country_id.label("owner_country_id"),
                    is_core_region.label("is_core_region"),
                    case(
                        (is_core_region.is_(False), WageEventRaw.income_tax_money * occupier_share),
                        else_=WageEventRaw.income_tax_money,
                    ).label("tax_income"),
                    case(
                        (is_core_region.is_(False), WageEventRaw.money * occupier_share),
                        else_=WageEventRaw.money,
                    ).label("wage_money"),
                    WageEventRaw.transaction_id.label("transaction_id"),
                    WageEventRaw.seller_company_id.label("seller_company_id"),
                    WageEventRaw.seller_user_id.label("seller_user_id"),
                )
                .where(*base_filters)
            )
            original_country_entries = (
                select(
                    WageEventRaw.event_hour.label("hour_start"),
                    WageEventRaw.region_initial_country_id.label("recipient_country_id"),
                    WageEventRaw.item_code.label("item_code"),
                    WageEventRaw.owner_country_id.label("owner_country_id"),
                    literal(True).label("is_core_region"),
                    (WageEventRaw.income_tax_money * core_owner_share).label("tax_income"),
                    (WageEventRaw.money * core_owner_share).label("wage_money"),
                    WageEventRaw.transaction_id.label("transaction_id"),
                    WageEventRaw.seller_company_id.label("seller_company_id"),
                    WageEventRaw.seller_user_id.label("seller_user_id"),
                )
                .where(
                    *base_filters,
                    is_core_region.is_(False),
                    WageEventRaw.region_initial_country_id != WageEventRaw.operating_country_id,
                    WageEventRaw.region_resistance.is_not(None),
                    WageEventRaw.region_resistance > 0,
                )
            )
            attributed_events = (
                union_all(operating_entries, original_country_entries)
                .subquery("attributed_wage_events")
            )
            await session.execute(
                delete(HourlyTaxRollup).where(HourlyTaxRollup.hour_start.in_(hour_batch))
            )
            await session.execute(
                insert(HourlyTaxRollup).from_select(
                    [
                        "hour_start",
                        "operating_country_id",
                        "item_code",
                        "owner_country_id",
                        "is_core_region",
                        "tax_income_sum",
                        "wage_money_sum",
                        "transaction_count",
                        "company_count",
                        "worker_count",
                    ],
                    select(
                        attributed_events.c.hour_start,
                        attributed_events.c.recipient_country_id,
                        attributed_events.c.item_code,
                        attributed_events.c.owner_country_id,
                        attributed_events.c.is_core_region,
                        func.sum(attributed_events.c.tax_income),
                        func.sum(attributed_events.c.wage_money),
                        func.count(attributed_events.c.transaction_id),
                        func.count(func.distinct(attributed_events.c.seller_company_id)),
                        func.count(func.distinct(attributed_events.c.seller_user_id)),
                    )
                    .group_by(
                        attributed_events.c.hour_start,
                        attributed_events.c.recipient_country_id,
                        attributed_events.c.item_code,
                        attributed_events.c.owner_country_id,
                        attributed_events.c.is_core_region,
                    ),
                )
            )

    async def _sync_metadata_if_needed(self, session: AsyncSession, client: WareraClient) -> None:
        now = datetime.now(tz=UTC)

        countries_synced_at = await self._get_state_datetime(session, COUNTRIES_SYNCED_AT_KEY)
        if countries_synced_at is None or (now - countries_synced_at).total_seconds() >= self._settings.country_metadata_refresh_seconds:
            countries = await client.get_all_countries()
            await self._upsert_countries(session, countries)
            await self._set_state_value(session, COUNTRIES_SYNCED_AT_KEY, now.isoformat())

        regions_synced_at = await self._get_state_datetime(session, REGIONS_SYNCED_AT_KEY)
        if regions_synced_at is None or (now - regions_synced_at).total_seconds() >= self._settings.country_metadata_refresh_seconds:
            regions = await client.get_regions_object()
            await self._upsert_regions(session, regions)
            await self._set_state_value(session, REGIONS_SYNCED_AT_KEY, now.isoformat())

    async def _apply_retention_if_needed(self, session: AsyncSession) -> None:
        last_applied_at = await self._get_state_datetime(session, RETENTION_APPLIED_AT_KEY)
        now = datetime.now(tz=UTC)
        if last_applied_at is not None and (now - last_applied_at) < timedelta(hours=6):
            return

        cutoff = now - timedelta(days=self._settings.raw_retention_days)
        await session.execute(delete(WageEventRaw).where(WageEventRaw.created_at < cutoff))
        await session.execute(delete(HourlyTaxRollup).where(HourlyTaxRollup.hour_start < cutoff))
        await self._set_state_value(session, RETENTION_APPLIED_AT_KEY, now.isoformat())

    async def _upsert_countries(self, session: AsyncSession, countries: list[dict[str, Any]]) -> None:
        existing = {
            row.id: row
            for row in await load_rows_by_in_batches(
                session,
                CountryCache,
                CountryCache.id,
                [str(country["_id"]) for country in countries],
            )
        }

        for payload in countries:
            country_id = str(payload["_id"])
            model = existing.get(country_id)
            if model is None:
                model = CountryCache(id=country_id)
                session.add(model)
            taxes = payload.get("taxes") or {}
            model.code = str(payload.get("code", "")).lower()
            model.name = str(payload.get("name", ""))
            model.income_tax = decimal_from_number(taxes.get("income"))
            model.market_tax = decimal_from_number(taxes.get("market"))
            model.self_work_tax = decimal_from_number(taxes.get("selfWork"))
            model.payload = payload

    async def _load_users_with_cache(
        self,
        session: AsyncSession,
        client: WareraClient,
        user_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not user_ids:
            return {}
        cached_rows = await load_rows_by_in_batches(session, UserCache, UserCache.id, user_ids)
        users = {
            row.id: dict(row.payload)
            for row in cached_rows
            if isinstance(row.payload, dict) and row.payload
        }
        missing_ids = [user_id for user_id in user_ids if user_id not in users]
        if missing_ids:
            users.update(await client.get_users_by_id(missing_ids))
        return users

    async def _load_companies_with_cache(
        self,
        session: AsyncSession,
        client: WareraClient,
        company_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not company_ids:
            return {}
        cached_rows = await load_rows_by_in_batches(session, CompanyCache, CompanyCache.id, company_ids)
        companies = {
            row.id: dict(row.payload)
            for row in cached_rows
            if isinstance(row.payload, dict) and row.payload
        }
        fresh_companies = await fetch_existing_companies_by_id(
            client,
            company_ids,
            batch_size=min(self._settings.warera_api_lookup_batch_size, 25),
        )
        companies.update(fresh_companies)
        return companies

    async def _upsert_regions(self, session: AsyncSession, regions: dict[str, dict[str, Any]]) -> None:
        region_ids = list(regions.keys())
        existing = {
            row.id: row
            for row in await load_rows_by_in_batches(session, RegionCache, RegionCache.id, region_ids)
        }

        for region_id, payload in regions.items():
            model = existing.get(region_id)
            if model is None:
                model = RegionCache(id=region_id)
                session.add(model)
            deposit = payload.get("deposit") or {}
            model.code = str(payload.get("code", ""))
            model.name = str(payload.get("name", ""))
            model.country_id = str(payload.get("country", ""))
            model.initial_country_id = str(payload.get("initialCountry", payload.get("country", "")))
            model.main_city = payload.get("mainCity")
            model.is_capital = bool(payload.get("isCapital", False))
            model.development = decimal_from_number(payload.get("development"))
            model.deposit_type = deposit.get("type") if isinstance(deposit, dict) else None
            model.payload = payload

    async def _upsert_users(self, session: AsyncSession, users: dict[str, dict[str, Any]]) -> None:
        if not users:
            return
        user_ids = list(users.keys())
        existing = {
            row.id: row
            for row in await load_rows_by_in_batches(session, UserCache, UserCache.id, user_ids)
        }

        for user_id, payload in users.items():
            model = existing.get(user_id)
            if model is None:
                model = UserCache(id=user_id)
                session.add(model)
            model.username = str(payload.get("username", ""))
            model.country_id = str(payload.get("country", ""))
            current_company = payload.get("company")
            model.current_company_id = str(current_company) if current_company else None
            model.payload = payload

    async def _upsert_companies(self, session: AsyncSession, companies: dict[str, dict[str, Any]]) -> None:
        if not companies:
            return
        company_ids = list(companies.keys())
        existing = {
            row.id: row
            for row in await load_rows_by_in_batches(session, CompanyCache, CompanyCache.id, company_ids)
        }

        for company_id, payload in companies.items():
            model = existing.get(company_id)
            if model is None:
                model = CompanyCache(id=company_id)
                session.add(model)
            model.owner_user_id = str(payload.get("user", ""))
            model.region_id = str(payload.get("region", ""))
            model.item_code = str(payload.get("itemCode", ""))
            model.name = str(payload.get("name", ""))
            model.worker_count = int(payload.get("workerCount", 0) or 0)
            model.production = decimal_from_number(payload.get("production"))
            model.estimated_value = decimal_from_number(payload.get("estimatedValue"))
            model.payload = payload

    async def _load_existing_events(
        self,
        session: AsyncSession,
        transaction_ids: list[str],
    ) -> dict[str, WageEventRaw]:
        if not transaction_ids:
            return {}
        return {
            row.transaction_id: row
            for row in await load_rows_by_in_batches(
                session,
                WageEventRaw,
                WageEventRaw.transaction_id,
                transaction_ids,
            )
        }

    async def _load_country_map(self, session: AsyncSession) -> dict[str, CountryMeta]:
        result = await session.execute(select(CountryCache))
        return {
            row.id: CountryMeta(
                id=row.id,
                code=row.code,
                name=row.name,
                income_tax=row.income_tax,
            )
            for row in result.scalars()
        }

    async def _load_region_map(self, session: AsyncSession) -> dict[str, RegionMeta]:
        result = await session.execute(select(RegionCache))
        return {
            row.id: RegionMeta(
                id=row.id,
                code=row.code,
                name=row.name,
                country_id=row.country_id,
                initial_country_id=row.initial_country_id,
                resistance=decimal_from_number((row.payload or {}).get("resistance")) if row.payload else None,
                resistance_max=decimal_from_number((row.payload or {}).get("resistanceMax")) if row.payload else None,
            )
            for row in result.scalars()
        }

    async def _get_state_row(self, session: AsyncSession, key: str) -> CollectorState | None:
        return await session.get(CollectorState, key)

    async def _get_state_json(self, session: AsyncSession, key: str) -> dict[str, Any] | None:
        row = await self._get_state_row(session, key)
        if row is None or row.json_value is None:
            return None
        return dict(row.json_value)

    async def _get_state_value(self, session: AsyncSession, key: str) -> str | None:
        row = await self._get_state_row(session, key)
        if row is None:
            return None
        return row.string_value

    async def _get_state_datetime(self, session: AsyncSession, key: str) -> datetime | None:
        value = await self._get_state_value(session, key)
        if not value:
            return None
        return parse_api_datetime(value)

    async def _set_state_json(self, session: AsyncSession, key: str, value: dict[str, Any]) -> None:
        row = await self._get_state_row(session, key)
        if row is None:
            row = CollectorState(key=key)
            session.add(row)
        row.json_value = value

    async def _set_state_value(self, session: AsyncSession, key: str, value: str) -> None:
        row = await self._get_state_row(session, key)
        if row is None:
            row = CollectorState(key=key)
            session.add(row)
        row.string_value = value
