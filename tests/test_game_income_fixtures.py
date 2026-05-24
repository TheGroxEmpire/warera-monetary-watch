from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from warera_monetary_watch.db.base import Base
from warera_monetary_watch.db.models import CountryCache, WageEventRaw
from warera_monetary_watch.services.analytics import (
    get_game_weekly_income_taxes,
    resolve_game_week_range,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "game_weekly_country_income_2026_17.json"


def load_game_income_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_game_weekly_country_income_fixture_documents_source_of_truth() -> None:
    fixture = load_game_income_fixture()

    assert fixture["source"] == "War Era game API inventoryAccount.getInventoryAccounts"
    assert fixture["source_payload_path"] == "result.data[].incomes.incomeTaxes"
    assert fixture["week"] == "2026-17"
    assert fixture["countries"]
    assert all("expected_income_taxes_display" in country for country in fixture["countries"])


def test_game_week_range_matches_account_bucket() -> None:
    assert resolve_game_week_range("2026-17") == (
        datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 27, 0, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_collected_income_taxes_are_compared_against_game_api_fixture_amounts() -> None:
    fixture = load_game_income_fixture()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        venezuela = CountryCache(
            id="country-ve",
            code="ve",
            name="Venezuela",
            income_tax=Decimal("10"),
            market_tax=Decimal("1"),
            self_work_tax=Decimal("0.01"),
            payload={},
        )
        session.add(venezuela)
        await session.flush()
        session.add(
            WageEventRaw(
                transaction_id="tx-ve-1",
                created_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
                event_hour=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
                money=Decimal("193748.444794"),
                quantity=Decimal("1"),
                seller_user_id="seller-ve",
                buyer_user_id="buyer-ve",
                seller_company_id="company-ve",
                operating_region_id="region-ve",
                operating_country_id=venezuela.id,
                region_initial_country_id=venezuela.id,
                owner_country_id=venezuela.id,
                item_code="oil",
                is_core_region=True,
                region_resistance=Decimal("100"),
                region_resistance_max=Decimal("100"),
                income_tax_rate=Decimal("10"),
                income_tax_money=Decimal("16437.825334"),
                resolved=True,
                unresolved_reason=None,
                raw_payload={},
            )
        )
        await session.commit()

        collected = await get_game_weekly_income_taxes(
            session,
            week=fixture["week"],
            country_codes=["ve"],
        )

    await engine.dispose()

    assert {
        country["country_code"]: country["income_taxes_display"]
        for country in collected["countries"]
    } == {"ve": "16.437K"}


@pytest.mark.asyncio
async def test_live_database_matches_game_api_income_fixture_when_enabled() -> None:
    if os.getenv("WARERA_COMPARE_GAME_INCOME_FIXTURES") != "1":
        pytest.skip("Set WARERA_COMPARE_GAME_INCOME_FIXTURES=1 to compare fixtures with a live DB.")

    database_url = os.environ["DATABASE_URL"]
    fixture = load_game_income_fixture()
    expected_display = {
        country["country_code"].lower(): country["expected_income_taxes_display"]
        for country in fixture["countries"]
    }
    engine = create_async_engine(database_url)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        collected = await get_game_weekly_income_taxes(
            session,
            week=fixture["week"],
            country_codes=list(expected_display),
        )
    await engine.dispose()

    assert {
        country["country_code"]: country["income_taxes_display"]
        for country in collected["countries"]
    } == expected_display
