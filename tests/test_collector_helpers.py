from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from warera_monetary_watch.config import Settings
from warera_monetary_watch.db.base import Base
from warera_monetary_watch.db.models import CompanyCache, HourlyTaxRollup, WageEventRaw
from warera_monetary_watch.services.collector import (
    CountryMeta,
    RegionMeta,
    WageCollectorService,
    batched_lookup_values,
    compute_income_tax_money,
    enrich_wage_transactions,
    find_company_id_for_wage,
    normalize_lookup_id,
)


class FakeWageClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls = 0

    async def get_wage_transactions(self, *, limit: int = 100, cursor: str | None = None) -> dict:
        page = self.pages[self.calls]
        self.calls += 1
        return page


class FakeLookupClient:
    async def get_users_by_id(self, user_ids: list[str], *, batch_size: int | None = None) -> dict:
        users = {
            "seller-1": {"_id": "seller-1", "username": "Seller", "country": "country-owner"},
            "buyer-1": {"_id": "buyer-1", "username": "Buyer", "country": "country-owner"},
        }
        return {user_id: users[user_id] for user_id in user_ids if user_id in users}

    async def get_workers_by_employer_user_ids(self, user_ids: list[str]) -> dict:
        return {user_id: {"workersPerCompany": []} for user_id in user_ids}

    async def get_companies_by_id(
        self,
        company_ids: list[str],
        *,
        batch_size: int | None = None,
    ) -> dict:
        return {}


class MovingCompanyClient:
    async def get_users_by_id(self, user_ids: list[str], *, batch_size: int | None = None) -> dict:
        users = {
            "seller-1": {"_id": "seller-1", "username": "Seller", "country": "country-owner"},
            "buyer-1": {"_id": "buyer-1", "username": "Buyer", "country": "country-owner"},
        }
        return {user_id: users[user_id] for user_id in user_ids if user_id in users}

    async def get_workers_by_employer_user_ids(self, user_ids: list[str]) -> dict:
        return {
            "buyer-1": {
                "workersPerCompany": [
                    {
                        "company": {"_id": "company-1"},
                        "workers": [
                            {
                                "user": "seller-1",
                                "company": "company-1",
                                "employer": "buyer-1",
                                "wage": 0.137,
                            }
                        ],
                    }
                ]
            }
        }

    async def get_companies_by_id(
        self,
        company_ids: list[str],
        *,
        batch_size: int | None = None,
    ) -> dict:
        return {
            "company-1": {
                "_id": "company-1",
                "user": "buyer-1",
                "region": "region-new",
                "itemCode": "steel",
                "name": "Moved Company",
                "workerCount": 1,
                "production": 0,
                "estimatedValue": 0,
            }
        }


def make_settings(max_pages: int) -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite:///./test.db",
        WARERA_API_BASE_URL="https://example.com/trpc",
        WARERA_API_TOKEN="test-token",
        COLLECTOR_MAX_PAGES_PER_CYCLE=max_pages,
    )


def test_compute_income_tax_money_uses_decimal_precision() -> None:
    assert compute_income_tax_money(Decimal("4.11"), Decimal("10")) == Decimal("0.411000")
    assert compute_income_tax_money(Decimal("4.11"), Decimal("0")) == Decimal("0.000000")


def make_wage_event(
    transaction_id: str,
    *,
    created_at: datetime,
    event_hour: datetime,
) -> WageEventRaw:
    return WageEventRaw(
        transaction_id=transaction_id,
        created_at=created_at,
        event_hour=event_hour,
        money=Decimal("100"),
        quantity=Decimal("1"),
        seller_user_id="seller-1",
        buyer_user_id="buyer-1",
        seller_company_id="company-1",
        operating_region_id="region-1",
        operating_country_id="country-operating",
        region_initial_country_id="country-operating",
        owner_country_id="country-owner",
        item_code="iron",
        is_core_region=True,
        region_resistance=Decimal("0"),
        region_resistance_max=Decimal("100"),
        income_tax_rate=Decimal("10"),
        income_tax_money=Decimal("10"),
        resolved=True,
        unresolved_reason=None,
        raw_payload={},
    )


def make_hourly_rollup(
    *,
    hour_start: datetime,
) -> HourlyTaxRollup:
    return HourlyTaxRollup(
        hour_start=hour_start,
        operating_country_id="country-operating",
        item_code="iron",
        owner_country_id="country-owner",
        is_core_region=True,
        tax_income_sum=Decimal("10"),
        wage_money_sum=Decimal("100"),
        transaction_count=1,
        company_count=1,
        worker_count=1,
    )


@pytest.mark.asyncio
async def test_retention_deletes_old_raw_events_and_hourly_rollups() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    old_hour = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=100)
    recent_hour = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=10)
    async with session_factory() as session:
        session.add_all(
            [
                make_wage_event("tx-old", created_at=old_hour, event_hour=old_hour),
                make_wage_event("tx-recent", created_at=recent_hour, event_hour=recent_hour),
            ]
        )
        session.add(make_hourly_rollup(hour_start=old_hour))
        await session.flush()
        session.add(make_hourly_rollup(hour_start=recent_hour))
        await session.commit()

        service = WageCollectorService(
            session_factory=session_factory,
            settings=make_settings(max_pages=1),
        )
        await service._apply_retention_if_needed(session)
        await session.commit()

        raw_ids = list(
            (
                await session.execute(
                    select(WageEventRaw.transaction_id).order_by(WageEventRaw.transaction_id)
                )
            ).scalars()
        )
        rollup_hours = list(
            (await session.execute(select(HourlyTaxRollup.hour_start))).scalars()
        )

    await engine.dispose()

    assert raw_ids == ["tx-recent"]
    assert rollup_hours == [recent_hour.replace(tzinfo=None)]


@pytest.mark.asyncio
async def test_rebuild_rollups_splits_non_core_tax_between_occupier_and_original_country() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        event_hour = datetime(2026, 5, 4, 10, 0, tzinfo=UTC)
        session.add(
            WageEventRaw(
                transaction_id="tx-noncore-1",
                created_at=event_hour,
                event_hour=event_hour,
                money=Decimal("100"),
                quantity=Decimal("1"),
                seller_user_id="seller-1",
                buyer_user_id="buyer-1",
                seller_company_id="company-1",
                operating_region_id="region-occupied",
                operating_country_id="country-occupier",
                region_initial_country_id="country-core-owner",
                owner_country_id="country-owner",
                item_code="iron",
                is_core_region=False,
                region_resistance=Decimal("100"),
                region_resistance_max=Decimal("100"),
                income_tax_rate=Decimal("10"),
                income_tax_money=Decimal("10"),
                resolved=True,
                unresolved_reason=None,
                raw_payload={},
            )
        )
        await session.commit()

        service = WageCollectorService(
            session_factory=session_factory,
            settings=make_settings(max_pages=1),
        )
        await service._rebuild_rollups(session, {event_hour})
        await session.commit()

        rows = list(
            (
                await session.execute(
                    select(HourlyTaxRollup).order_by(HourlyTaxRollup.operating_country_id)
                )
            ).scalars()
        )

    await engine.dispose()

    assert len(rows) == 2
    assert rows[0].operating_country_id == "country-core-owner"
    assert rows[0].is_core_region is True
    assert rows[0].tax_income_sum == Decimal("4.000000")
    assert rows[0].wage_money_sum == Decimal("40.000000")
    assert rows[1].operating_country_id == "country-occupier"
    assert rows[1].is_core_region is False
    assert rows[1].tax_income_sum == Decimal("6.000000")
    assert rows[1].wage_money_sum == Decimal("60.000000")


@pytest.mark.asyncio
async def test_ingest_transactions_refreshes_cached_company_regions() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            CompanyCache(
                id="company-1",
                owner_user_id="buyer-1",
                region_id="region-old",
                item_code="steel",
                name="Stale Company",
                worker_count=1,
                production=Decimal("0"),
                estimated_value=Decimal("0"),
                payload={
                    "_id": "company-1",
                    "user": "buyer-1",
                    "region": "region-old",
                    "itemCode": "steel",
                    "name": "Stale Company",
                },
            )
        )
        await session.commit()

        service = WageCollectorService(session_factory=session_factory, settings=make_settings(max_pages=1))
        stats = await service._ingest_transactions(
            session,
            MovingCompanyClient(),
            countries_by_id={
                "country-new": CountryMeta("country-new", "ve", "Venezuela", Decimal("10")),
                "country-old": CountryMeta("country-old", "iq", "Iraq", Decimal("10")),
                "country-owner": CountryMeta("country-owner", "eg", "Egypt", Decimal("10")),
            },
            regions_by_id={
                "region-new": RegionMeta(
                    "region-new",
                    "ve-region",
                    "Venezuela Region",
                    "country-new",
                    "country-new",
                    Decimal("100"),
                    Decimal("100"),
                ),
                "region-old": RegionMeta(
                    "region-old",
                    "iq-region",
                    "Old Region",
                    "country-old",
                    "country-old",
                    Decimal("100"),
                    Decimal("100"),
                ),
            },
            transactions=[
                {
                    "_id": "tx-1",
                    "createdAt": "2026-05-09T08:44:52.178Z",
                    "money": 4.11,
                    "quantity": 30,
                    "sellerId": "seller-1",
                    "buyerId": "buyer-1",
                }
            ],
        )
        await session.flush()
        event = await session.get(WageEventRaw, "tx-1")
        company = await session.get(CompanyCache, "company-1")

    await engine.dispose()

    assert stats["resolved_transactions"] == 1
    assert event is not None
    assert event.operating_region_id == "region-new"
    assert event.operating_country_id == "country-new"
    assert company is not None
    assert company.region_id == "region-new"


def test_enrich_wage_transaction_uses_historical_tax_rate_when_available() -> None:
    transactions = [
        {
            "_id": "tx-1",
            "createdAt": "2026-04-21T09:44:52.178Z",
            "money": 4.11,
            "quantity": 30,
            "sellerId": "seller-1",
            "buyerId": "buyer-1",
            "incomeTaxRate": "7",
        }
    ]
    enriched = enrich_wage_transactions(
        transactions,
        seller_users={"seller-1": {}},
        buyer_users={"buyer-1": {"country": "country-owner"}},
        workers_by_employer_user_id={
            "buyer-1": {
                "workersPerCompany": [
                    {
                        "company": {"_id": "company-1"},
                        "workers": [
                            {
                                "user": "seller-1",
                                "company": "company-1",
                                "employer": "buyer-1",
                                "joinedAt": "2026-04-01T00:00:00.000Z",
                            }
                        ],
                    }
                ]
            }
        },
        companies={"company-1": {"user": "buyer-1", "region": "region-1", "itemCode": "steel"}},
        countries_by_id={
            "country-operating": CountryMeta("country-operating", "eg", "Egypt", Decimal("10")),
            "country-owner": CountryMeta("country-owner", "nl", "Netherlands", Decimal("8")),
        },
        regions_by_id={
            "region-1": RegionMeta(
                "region-1",
                "eg-cairo",
                "Cairo",
                "country-operating",
                "country-operating",
                Decimal("100"),
                Decimal("100"),
            )
        },
    )

    assert enriched[0].income_tax_rate == Decimal("7")
    assert enriched[0].income_tax_money == Decimal("0.287700")


@pytest.mark.asyncio
async def test_ingest_transactions_preserves_existing_resolved_event_when_reenrichment_fails() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            WageEventRaw(
                transaction_id="tx-1",
                created_at=datetime(2026, 4, 21, 9, 44, 52, tzinfo=UTC),
                event_hour=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
                money=Decimal("4.110000"),
                quantity=Decimal("30.000000"),
                seller_user_id="seller-1",
                buyer_user_id="buyer-1",
                seller_company_id="company-1",
                operating_region_id="region-1",
                operating_country_id="country-operating",
                region_initial_country_id="country-operating",
                owner_country_id="country-owner",
                item_code="steel",
                is_core_region=True,
                region_resistance=Decimal("100"),
                region_resistance_max=Decimal("100"),
                income_tax_rate=Decimal("10"),
                income_tax_money=Decimal("0.411000"),
                resolved=True,
                unresolved_reason=None,
                raw_payload={},
            )
        )
        await session.commit()

        service = WageCollectorService(session_factory=session_factory, settings=make_settings(max_pages=1))
        stats = await service._ingest_transactions(
            session,
            FakeLookupClient(),
            countries_by_id={
                "country-operating": CountryMeta("country-operating", "eg", "Egypt", Decimal("10")),
                "country-owner": CountryMeta("country-owner", "nl", "Netherlands", Decimal("8")),
            },
            regions_by_id={
                "region-1": RegionMeta(
                    "region-1",
                    "eg-cairo",
                    "Cairo",
                    "country-operating",
                    "country-operating",
                    Decimal("100"),
                    Decimal("100"),
                )
            },
            transactions=[
                {
                    "_id": "tx-1",
                    "createdAt": "2026-04-21T09:44:52.178Z",
                    "money": 4.11,
                    "quantity": 30,
                    "sellerId": "seller-1",
                    "buyerId": "buyer-1",
                }
            ],
        )
        await session.flush()
        model = await session.get(WageEventRaw, "tx-1")

    await engine.dispose()

    assert stats["resolved_transactions"] == 0
    assert stats["unresolved_transactions"] == 1
    assert model is not None
    assert model.resolved is True
    assert model.income_tax_money == Decimal("0.411000")
    assert model.unresolved_reason is None


@pytest.mark.asyncio
async def test_ingest_transactions_keeps_new_unresolved_event_for_later_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        service = WageCollectorService(session_factory=session_factory, settings=make_settings(max_pages=1))
        stats = await service._ingest_transactions(
            session,
            FakeLookupClient(),
            countries_by_id={
                "country-operating": CountryMeta("country-operating", "eg", "Egypt", Decimal("10")),
                "country-owner": CountryMeta("country-owner", "nl", "Netherlands", Decimal("8")),
            },
            regions_by_id={},
            transactions=[
                {
                    "_id": "tx-1",
                    "createdAt": "2026-04-21T09:44:52.178Z",
                    "money": 4.11,
                    "quantity": 30,
                    "sellerId": "seller-1",
                    "buyerId": "buyer-1",
                }
            ],
        )
        await session.commit()
        rows = (await session.execute(select(WageEventRaw))).scalars().all()

    await engine.dispose()

    assert stats["resolved_transactions"] == 0
    assert stats["unresolved_transactions"] == 1
    assert len(rows) == 1
    assert rows[0].transaction_id == "tx-1"
    assert rows[0].resolved is False
    assert rows[0].unresolved_reason == "missing_matching_worker_company"
    assert rows[0].raw_payload["_id"] == "tx-1"


@pytest.mark.asyncio
async def test_collect_new_transactions_reports_when_cursor_is_reached() -> None:
    service = WageCollectorService(session_factory=None, settings=make_settings(max_pages=2))
    client = FakeWageClient(
        [
            {
                "items": [
                    {"_id": "tx-3", "createdAt": "2026-04-21T11:00:00.000Z"},
                    {"_id": "tx-2", "createdAt": "2026-04-21T10:00:00.000Z"},
                    {"_id": "tx-1", "createdAt": "2026-04-21T09:00:00.000Z"},
                ],
                "nextCursor": "next",
            }
        ]
    )

    collected = await service._collect_new_transactions(
        client,
        {"transaction_id": "tx-1", "created_at": "2026-04-21T09:00:00.000Z"},
    )

    assert [item["_id"] for item in collected.transactions] == ["tx-2", "tx-3"]
    assert collected.newest_marker == {
        "transaction_id": "tx-3",
        "created_at": "2026-04-21T11:00:00.000Z",
    }
    assert collected.reached_cursor is True
    assert collected.exhausted_pages is False


@pytest.mark.asyncio
async def test_collect_new_transactions_marks_exhausted_without_reaching_cursor() -> None:
    service = WageCollectorService(session_factory=None, settings=make_settings(max_pages=1))
    client = FakeWageClient(
        [
            {
                "items": [
                    {"_id": "tx-5", "createdAt": "2026-04-21T13:00:00.000Z"},
                    {"_id": "tx-4", "createdAt": "2026-04-21T12:00:00.000Z"},
                ],
                "nextCursor": "next",
            }
        ]
    )

    collected = await service._collect_new_transactions(
        client,
        {"transaction_id": "tx-1", "created_at": "2026-04-21T09:00:00.000Z"},
    )

    assert [item["_id"] for item in collected.transactions] == ["tx-4", "tx-5"]
    assert collected.reached_cursor is False
    assert collected.exhausted_pages is True


def test_enrich_wage_transaction_success() -> None:
    transactions = [
        {
            "_id": "tx-1",
            "createdAt": "2026-04-21T09:44:52.178Z",
            "money": 4.11,
            "quantity": 30,
            "sellerId": "seller-1",
            "buyerId": "buyer-1",
        }
    ]
    seller_users = {"seller-1": {}}
    buyer_users = {"buyer-1": {"country": "country-owner"}}
    workers_by_employer_user_id = {
        "buyer-1": {
            "workersPerCompany": [
                {
                    "company": {"_id": "company-1", "itemCode": "steel"},
                    "workers": [
                        {
                            "user": "seller-1",
                            "company": "company-1",
                            "employer": "buyer-1",
                            "wage": 0.137,
                        }
                    ],
                }
            ]
        }
    }
    companies = {"company-1": {"user": "buyer-1", "region": "region-1", "itemCode": "steel"}}
    countries = {
        "country-operating": CountryMeta("country-operating", "eg", "Egypt", Decimal("10")),
        "country-owner": CountryMeta("country-owner", "nl", "Netherlands", Decimal("8")),
    }
    regions = {
        "region-1": RegionMeta(
            "region-1",
            "eg-cairo",
            "Cairo",
            "country-operating",
            "country-operating",
            Decimal("100"),
            Decimal("100"),
        )
    }

    enriched = enrich_wage_transactions(
        transactions,
        seller_users=seller_users,
        buyer_users=buyer_users,
        workers_by_employer_user_id=workers_by_employer_user_id,
        companies=companies,
        countries_by_id=countries,
        regions_by_id=regions,
    )

    assert len(enriched) == 1
    event = enriched[0]
    assert event.resolved is True
    assert event.seller_company_id == "company-1"
    assert event.operating_country_id == "country-operating"
    assert event.region_initial_country_id == "country-operating"
    assert event.owner_country_id == "country-owner"
    assert event.item_code == "steel"
    assert event.is_core_region is True
    assert event.region_resistance == Decimal("100")
    assert event.region_resistance_max == Decimal("100")
    assert event.income_tax_money == Decimal("0.411000")
    assert event.event_hour == datetime(2026, 4, 21, 9, 0, tzinfo=UTC)


def test_enrich_wage_transaction_marks_missing_company_as_unresolved() -> None:
    transactions = [
        {
            "_id": "tx-1",
            "createdAt": "2026-04-21T09:44:52.178Z",
            "money": 4.11,
            "quantity": 30,
            "sellerId": "seller-1",
            "buyerId": "buyer-1",
        }
    ]
    seller_users = {"seller-1": {}}
    buyer_users = {"buyer-1": {"country": "country-owner"}}
    workers_by_employer_user_id = {
        "buyer-1": {
            "workersPerCompany": [
                {
                    "company": {"_id": "company-1"},
                    "workers": [{"user": "seller-1", "company": "company-1", "employer": "buyer-1"}],
                }
            ]
        }
    }

    enriched = enrich_wage_transactions(
        transactions,
        seller_users=seller_users,
        buyer_users=buyer_users,
        workers_by_employer_user_id=workers_by_employer_user_id,
        companies={},
        countries_by_id={},
        regions_by_id={},
    )

    assert enriched[0].resolved is False
    assert enriched[0].unresolved_reason == "missing_company_detail"


def test_find_company_id_for_wage_matches_employer_worker_record_by_wage() -> None:
    company_id = find_company_id_for_wage(
        seller_user_id="worker-1",
        buyer_user_id="employer-1",
        created_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        wage_money=Decimal("4.11"),
        quantity=Decimal("30"),
        employer_workers={
            "workersPerCompany": [
                {
                    "company": {"_id": "company-a"},
                    "workers": [
                        {
                            "user": "worker-1",
                            "employer": "employer-1",
                            "company": "company-a",
                            "wage": 0.120,
                            "joinedAt": "2026-04-01T00:00:00.000Z",
                        }
                    ],
                },
                {
                    "company": {"_id": "company-b"},
                    "workers": [
                        {
                            "user": "worker-1",
                            "employer": "employer-1",
                            "company": "company-b",
                            "wage": 0.137,
                            "joinedAt": "2026-04-01T00:00:00.000Z",
                        }
                    ],
                },
            ]
        },
    )

    assert company_id == "company-b"


def test_find_company_id_for_wage_rejects_workers_who_joined_after_wage() -> None:
    company_id = find_company_id_for_wage(
        seller_user_id="worker-1",
        buyer_user_id="employer-1",
        created_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        wage_money=Decimal("4.11"),
        quantity=Decimal("30"),
        employer_workers={
            "workersPerCompany": [
                {
                    "company": {"_id": "company-a"},
                    "workers": [
                        {
                            "user": "worker-1",
                            "employer": "employer-1",
                            "company": "company-a",
                            "wage": 0.137,
                            "joinedAt": "2026-04-22T00:00:00.000Z",
                        }
                    ],
                }
            ]
        },
    )

    assert company_id is None


def test_enrich_wage_transaction_does_not_use_worker_current_company() -> None:
    transactions = [
        {
            "_id": "tx-1",
            "createdAt": "2026-04-21T09:44:52.178Z",
            "money": 4.11,
            "quantity": 30,
            "sellerId": "seller-1",
            "buyerId": "buyer-1",
        }
    ]

    enriched = enrich_wage_transactions(
        transactions,
        seller_users={"seller-1": {"company": "wrong-current-company"}},
        buyer_users={"buyer-1": {"country": "country-owner"}},
        workers_by_employer_user_id={},
        companies={"wrong-current-company": {"user": "buyer-1", "region": "region-1", "itemCode": "steel"}},
        countries_by_id={},
        regions_by_id={},
    )

    assert enriched[0].resolved is False
    assert enriched[0].unresolved_reason == "missing_matching_worker_company"


def test_normalize_lookup_id_rejects_missing_and_sentinel_values() -> None:
    assert normalize_lookup_id(None) is None
    assert normalize_lookup_id("") is None
    assert normalize_lookup_id("  ") is None
    assert normalize_lookup_id("None") is None
    assert normalize_lookup_id("undefined") is None
    assert normalize_lookup_id(" user-1 ") == "user-1"


def test_batched_lookup_values_deduplicates_and_splits_batches() -> None:
    values = ["a", "b", "a", "c", "d", "e"]

    assert batched_lookup_values(values, batch_size=2) == [["a", "b"], ["c", "d"], ["e"]]


def test_enrich_wage_transaction_marks_missing_user_ids_as_unresolved() -> None:
    transactions = [
        {
            "_id": "tx-1",
            "createdAt": "2026-04-21T09:44:52.178Z",
            "money": 4.11,
            "quantity": 30,
            "sellerId": None,
            "buyerId": "buyer-1",
        }
    ]

    enriched = enrich_wage_transactions(
        transactions,
        seller_users={},
        buyer_users={},
        workers_by_employer_user_id={},
        companies={},
        countries_by_id={},
        regions_by_id={},
    )

    assert enriched[0].seller_user_id == ""
    assert enriched[0].buyer_user_id == "buyer-1"
    assert enriched[0].resolved is False
    assert enriched[0].unresolved_reason == "missing_seller_user_id"
