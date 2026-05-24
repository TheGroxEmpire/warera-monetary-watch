from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, text

from warera_monetary_watch.config import get_settings
from warera_monetary_watch.db.models import (
    CollectorState,
    CompanyCache,
    HourlyTaxRollup,
    WageEventRaw,
)
from warera_monetary_watch.db.session import SessionLocal
from warera_monetary_watch.integrations.warera.client import WareraClient
from warera_monetary_watch.services.collector import (
    WageCollectorService,
    batched_lookup_values,
    fetch_existing_companies_by_id,
    parse_api_datetime,
)

logger = logging.getLogger(__name__)

BACKFILL_WAGES_STATE_KEY = "maintenance.backfill_wages"


async def rebuild_hourly_tax_rollups() -> int:
    async with SessionLocal() as session:
        await session.execute(delete(HourlyTaxRollup))
        hours = set((await session.execute(select(WageEventRaw.event_hour).distinct())).scalars())
        service = WageCollectorService(session_factory=SessionLocal, settings=get_settings())
        await service._rebuild_rollups(session, hours)
        await session.commit()
        return len(hours)


async def purge_unresolved_wage_events() -> int:
    async with SessionLocal() as session:
        result = await session.execute(delete(WageEventRaw).where(WageEventRaw.resolved.is_(False)))
        purged = int(result.rowcount or 0)
        state = await session.get(CollectorState, BACKFILL_WAGES_STATE_KEY)
        if state is not None and state.json_value is not None:
            state_value = dict(state.json_value)
            state_value["unresolved_transactions"] = 0
            state_value["purged_unresolved_transactions"] = (
                int(state_value.get("purged_unresolved_transactions") or 0) + purged
            )
            state.json_value = state_value
        await session.commit()
        return purged


async def refresh_company_regions_for_wages(
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    if start >= end:
        raise ValueError("--from must be earlier than --to")

    settings = get_settings()
    service = WageCollectorService(session_factory=SessionLocal, settings=settings)
    async with SessionLocal() as session:
        async with WareraClient(
            base_url=settings.warera_api_base_url,
            token=settings.warera_api_token,
            rate_limit_per_minute=settings.warera_api_rate_limit_per_minute,
            lookup_batch_size=settings.warera_api_lookup_batch_size,
            lookup_concurrency=settings.warera_api_lookup_concurrency,
        ) as client:
            await service._sync_metadata_if_needed(session, client)
            refresh_cutoff = datetime.now(tz=UTC) - timedelta(hours=1)
            company_ids = list(
                (
                    await session.execute(
                        select(WageEventRaw.seller_company_id)
                        .distinct()
                        .outerjoin(CompanyCache, CompanyCache.id == WageEventRaw.seller_company_id)
                        .where(
                            WageEventRaw.resolved.is_(True),
                            WageEventRaw.seller_company_id.is_not(None),
                            WageEventRaw.created_at >= start,
                            WageEventRaw.created_at < end,
                            or_(
                                CompanyCache.id.is_(None),
                                CompanyCache.updated_at < refresh_cutoff,
                            ),
                        )
                    )
                ).scalars()
            )

            refreshed_companies = 0
            refresh_batch_size = max(
                settings.warera_api_lookup_batch_size,
                settings.warera_api_lookup_batch_size * settings.warera_api_lookup_concurrency,
            )
            for company_batch in batched_lookup_values(company_ids, refresh_batch_size):
                companies = await fetch_existing_companies_by_id(
                    client,
                    company_batch,
                    batch_size=min(settings.warera_api_lookup_batch_size, 25),
                )
                await service._upsert_companies(session, companies)
                refreshed_companies += len(companies)
                await session.commit()

            changed_hours = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            """
                            with changed as (
                                update wage_events_raw as w
                                set
                                    operating_region_id = c.region_id,
                                    operating_country_id = r.country_id,
                                    region_initial_country_id = r.initial_country_id,
                                    item_code = c.item_code,
                                    is_core_region = (r.country_id = r.initial_country_id),
                                    region_resistance = nullif(r.payload ->> 'resistance', '')::numeric,
                                    region_resistance_max = nullif(r.payload ->> 'resistanceMax', '')::numeric
                                from company_cache as c
                                join region_cache as r on r.id = c.region_id
                                join country_cache as country on country.id = r.country_id
                                where w.seller_company_id = c.id
                                  and w.resolved is true
                                  and w.seller_company_id is not null
                                  and w.created_at >= :start
                                  and w.created_at < :end
                                  and (
                                      w.operating_region_id is distinct from c.region_id
                                      or w.operating_country_id is distinct from r.country_id
                                      or w.region_initial_country_id is distinct from r.initial_country_id
                                      or w.item_code is distinct from c.item_code
                                      or w.is_core_region is distinct from (r.country_id = r.initial_country_id)
                                      or w.region_resistance is distinct from nullif(r.payload ->> 'resistance', '')::numeric
                                      or w.region_resistance_max is distinct from nullif(r.payload ->> 'resistanceMax', '')::numeric
                                  )
                                returning w.event_hour
                            )
                            select event_hour from changed
                            """
                        ),
                        {"start": start, "end": end},
                    )
                ).all()
            }
            if changed_hours:
                await service._rebuild_rollups(session, changed_hours)
            await session.commit()

            return {
                "company_ids": len(company_ids),
                "refreshed_companies": refreshed_companies,
                "affected_hours": len(changed_hours),
            }


def parse_cli_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def backfill_wage_transactions(
    *,
    start: datetime,
    end: datetime,
    max_pages: int,
    batch_size: int,
) -> dict[str, Any]:
    if start >= end:
        raise ValueError("--from must be earlier than --to")

    settings = get_settings()
    service = WageCollectorService(session_factory=SessionLocal, settings=settings)
    async with SessionLocal() as session:
        async with WareraClient(
            base_url=settings.warera_api_base_url,
            token=settings.warera_api_token,
            rate_limit_per_minute=settings.warera_api_rate_limit_per_minute,
            lookup_batch_size=settings.warera_api_lookup_batch_size,
            lookup_concurrency=settings.warera_api_lookup_concurrency,
        ) as client:
            await service._sync_metadata_if_needed(session, client)
            countries_by_id = await service._load_country_map(session)
            regions_by_id = await service._load_region_map(session)

            cursor: str | None = None
            batch: list[dict[str, Any]] = []
            affected_hours: set[datetime] = set()
            pages_scanned = 0
            matched_transactions = 0
            resolved_transactions = 0
            unresolved_transactions = 0
            reached_start = False
            started_at = datetime.now(tz=UTC)
            oldest_seen_at: datetime | None = None
            newest_seen_at: datetime | None = None

            async def set_backfill_progress(*, stage: str, in_progress: bool = True) -> None:
                now = datetime.now(tz=UTC)
                row = await session.get(CollectorState, BACKFILL_WAGES_STATE_KEY)
                if row is None:
                    row = CollectorState(key=BACKFILL_WAGES_STATE_KEY)
                    session.add(row)
                row.json_value = {
                    "operation": "backfill-wages",
                    "in_progress": in_progress,
                    "stage": stage,
                    "started_at": started_at.isoformat(),
                    "updated_at": now.isoformat(),
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "max_pages": max_pages,
                    "batch_size": batch_size,
                    "pages_scanned": pages_scanned,
                    "matched_transactions": matched_transactions,
                    "resolved_transactions": resolved_transactions,
                    "unresolved_transactions": unresolved_transactions,
                    "affected_hours": len(affected_hours),
                    "oldest_seen_at": oldest_seen_at.isoformat() if oldest_seen_at else None,
                    "newest_seen_at": newest_seen_at.isoformat() if newest_seen_at else None,
                    "reached_start": reached_start,
                }
                row.string_value = now.isoformat()

            await set_backfill_progress(stage="starting")
            await session.commit()

            async def flush_batch() -> None:
                nonlocal resolved_transactions, unresolved_transactions
                if not batch:
                    return
                batch_size_flushed = len(batch)
                await set_backfill_progress(stage="enriching_batch")
                await session.commit()
                stats = await service._ingest_transactions(
                    session,
                    client,
                    countries_by_id,
                    regions_by_id,
                    list(batch),
                )
                batch_hours = stats["affected_hours"]
                affected_hours.update(batch_hours)
                if batch_hours:
                    await service._rebuild_rollups(session, batch_hours)
                await session.commit()
                resolved_transactions += stats["resolved_transactions"]
                unresolved_transactions += stats["unresolved_transactions"]
                batch.clear()
                await set_backfill_progress(stage="committed_batch")
                logger.info(
                    "Backfill committed batch: batch=%s pages=%s matched=%s resolved=%s unresolved=%s hours=%s",
                    batch_size_flushed,
                    pages_scanned,
                    matched_transactions,
                    resolved_transactions,
                    unresolved_transactions,
                    len(affected_hours),
                )

            for _ in range(max_pages):
                page = await client.get_wage_transactions(limit=100, cursor=cursor)
                pages_scanned += 1
                items = page.get("items", [])
                if not items:
                    break

                for item in items:
                    created_at = parse_api_datetime(str(item["createdAt"]))
                    if newest_seen_at is None or created_at > newest_seen_at:
                        newest_seen_at = created_at
                    if oldest_seen_at is None or created_at < oldest_seen_at:
                        oldest_seen_at = created_at
                    if created_at < start:
                        reached_start = True
                        break
                    if created_at < end:
                        matched_transactions += 1
                        batch.append(item)
                    if len(batch) >= batch_size:
                        await flush_batch()

                if reached_start or not page.get("nextCursor"):
                    break
                cursor = str(page["nextCursor"])
                await set_backfill_progress(stage="scanning_pages")
                await session.commit()

            await flush_batch()
            await set_backfill_progress(stage="complete", in_progress=False)
            await session.commit()

            return {
                "pages_scanned": pages_scanned,
                "matched_transactions": matched_transactions,
                "resolved_transactions": resolved_transactions,
                "unresolved_transactions": unresolved_transactions,
                "affected_hours": len(affected_hours),
                "reached_start": reached_start,
            }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="WarEra Monetary Watch maintenance tools.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("rebuild-rollups")
    subparsers.add_parser("purge-unresolved-wages")
    refresh_companies_parser = subparsers.add_parser("refresh-company-regions")
    refresh_companies_parser.add_argument("--from", dest="from_value", required=True)
    refresh_companies_parser.add_argument("--to", dest="to_value", required=True)
    backfill_parser = subparsers.add_parser("backfill-wages")
    backfill_parser.add_argument("--from", dest="from_value", required=True)
    backfill_parser.add_argument("--to", dest="to_value", required=True)
    backfill_parser.add_argument("--max-pages", type=int, default=5000)
    backfill_parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args()

    if args.command in {None, "rebuild-rollups"}:
        rebuilt = asyncio.run(rebuild_hourly_tax_rollups())
        logger.info("Rebuilt %s hourly tax rollup rows.", rebuilt)
        return

    if args.command == "purge-unresolved-wages":
        purged = asyncio.run(purge_unresolved_wage_events())
        logger.info("Purged %s unresolved wage event rows.", purged)
        return

    if args.command == "refresh-company-regions":
        stats = asyncio.run(
            refresh_company_regions_for_wages(
                start=parse_cli_datetime(args.from_value),
                end=parse_cli_datetime(args.to_value),
            )
        )
        logger.info("Refreshed company regions: %s", stats)
        return

    if args.command == "backfill-wages":
        stats = asyncio.run(
            backfill_wage_transactions(
                start=parse_cli_datetime(args.from_value),
                end=parse_cli_datetime(args.to_value),
                max_pages=args.max_pages,
                batch_size=args.batch_size,
            )
        )
        logger.info("Backfill complete: %s", stats)


if __name__ == "__main__":
    main()
