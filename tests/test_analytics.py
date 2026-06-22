from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import warera_monetary_watch.services.analytics as analytics
from warera_monetary_watch.db.base import Base
from warera_monetary_watch.db.models import CountryCache, HourlyTaxRollup
from warera_monetary_watch.services.analytics import (
    get_country_dataset,
    resolve_hour_range,
    start_of_week_monday_utc,
)
from warera_monetary_watch.services.tax_rules import (
    calculate_citizenship_tax_share,
    split_tax_for_citizenship,
)


def test_calculate_citizenship_tax_share_returns_zero_for_domestic_workers() -> None:
    assert calculate_citizenship_tax_share(
        work_country_id="country-work",
        worker_country_id="country-work",
    ) == Decimal("0")


def test_split_tax_for_citizenship_sends_thirty_percent_to_citizenship_country() -> None:
    split = split_tax_for_citizenship(
        total_tax=Decimal("10"),
        total_wages=Decimal("100"),
        work_country_id="country-work",
        worker_country_id="country-citizen",
    )

    assert split["citizenship_country_tax"] == Decimal("3.0")
    assert split["work_country_tax"] == Decimal("7.0")
    assert split["citizenship_country_wages"] == Decimal("30.0")
    assert split["work_country_wages"] == Decimal("70.0")


def test_resolve_hour_range_treats_to_as_exact_exclusive_boundary() -> None:
    start, end = resolve_hour_range("2026-04-23T00:00:00Z", "2026-04-23T12:00:00Z")

    assert start == datetime(2026, 4, 23, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


def test_start_of_week_monday_utc_returns_monday_midnight() -> None:
    assert start_of_week_monday_utc(datetime(2026, 4, 26, 16, 45, tzinfo=UTC)) == datetime(
        2026,
        4,
        20,
        0,
        0,
        tzinfo=UTC,
    )


def test_resolve_hour_range_defaults_from_to_this_week_monday(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2026, 4, 26, 16, 30, tzinfo=tz or UTC)

    monkeypatch.setattr(analytics, "datetime", FixedDateTime)

    start, end = resolve_hour_range(None, None)

    assert start == datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 4, 26, 17, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_country_dataset_uses_hourly_rollups_from_database_only() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        operating_country = CountryCache(
            id="country-operating",
            code="op",
            name="Operating Country",
            income_tax=Decimal("10"),
            market_tax=Decimal("0"),
            self_work_tax=Decimal("0"),
            payload={},
        )
        owner_country = CountryCache(
            id="country-owner",
            code="ow",
            name="Owner Country",
            income_tax=Decimal("5"),
            market_tax=Decimal("0"),
            self_work_tax=Decimal("0"),
            payload={},
        )
        session.add_all(
            [
                operating_country,
                owner_country,
                HourlyTaxRollup(
                    hour_start=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
                    operating_country_id=operating_country.id,
                    item_code="steel",
                    owner_country_id=owner_country.id,
                    is_core_region=True,
                    is_foreign_worker=False,
                    tax_income_sum=Decimal("12.5"),
                    wage_money_sum=Decimal("125"),
                    transaction_count=3,
                    company_count=2,
                    worker_count=3,
                ),
            ]
        )
        await session.commit()

        dataset = await get_country_dataset(
            session,
            operating_country,
            from_value="2026-04-23T00:00:00Z",
            to_value="2026-04-24T00:00:00Z",
        )

    await engine.dispose()

    assert dataset["entries"] == [
        {
            "hour_start": "2026-04-23T10:00:00",
            "region_id": None,
            "owner_country_id": "country-owner",
            "owner_country_code": "ow",
            "owner_country_name": "Owner Country",
            "item_code": "steel",
            "is_core_region": True,
            "is_foreign_worker": False,
            "tax_income": 12.5,
            "wages_paid": 125.0,
            "transactions": 3,
            "companies": 2,
            "workers": 3,
        }
    ]
