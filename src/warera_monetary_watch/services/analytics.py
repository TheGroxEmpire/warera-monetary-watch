from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from warera_monetary_watch.db.models import (
    CollectorState,
    CountryCache,
    HourlyTaxRollup,
    WageEventRaw,
)
from warera_monetary_watch.services.tax_rules import (
    FOREIGN_CITIZEN_TAX_EFFECTIVE_AT,
    FOREIGN_CITIZEN_TAX_SHARE,
)


class InvalidHourRange(ValueError):
    pass


def parse_requested_hour(value: str | None) -> datetime | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidHourRange("Date filters must be valid ISO 8601 date-time values.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def start_of_week_monday_utc(value: datetime) -> datetime:
    utc_value = value.astimezone(UTC)
    return (utc_value - timedelta(days=utc_value.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def resolve_game_week_range(week: str) -> tuple[datetime, datetime]:
    year, week_number = week.strip().split("-", 1)
    start = datetime.fromisocalendar(int(year), int(week_number.removeprefix("W")), 1).replace(
        tzinfo=UTC
    )
    return start, start + timedelta(days=7)


def resolve_hour_range(from_value: str | None, to_value: str | None) -> tuple[datetime, datetime]:
    now_hour = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    end = parse_requested_hour(to_value) or (now_hour + timedelta(hours=1))
    start = parse_requested_hour(from_value) or start_of_week_monday_utc(end)
    if start > end:
        raise InvalidHourRange("`from` must be earlier than or equal to `to`.")
    return start, end


def decimal_to_float(value: Decimal | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def format_game_compact_money(value: Decimal) -> str:
    scaled = int((value / Decimal("1000")) * Decimal("1000")) / 1000
    return f"{scaled:.3f}K"


def normalize_foreign_filter(value: str | None) -> str:
    normalized = (value or "all").strip().lower()
    if normalized in {"foreign", "noncore"}:
        return "foreign"
    if normalized in {"nonforeign", "non_foreign", "domestic", "core"}:
        return "nonforeign"
    return "all"


async def get_country_by_code(session: AsyncSession, country_code: str) -> CountryCache | None:
    result = await session.execute(
        select(CountryCache).where(func.lower(CountryCache.code) == country_code.lower())
    )
    return result.scalar_one_or_none()


async def get_filters(session: AsyncSession) -> dict[str, Any]:
    countries = list(
        (
            await session.execute(select(CountryCache).order_by(CountryCache.name.asc()))
        ).scalars()
    )
    items_result = await session.execute(
        select(HourlyTaxRollup.item_code).distinct().order_by(HourlyTaxRollup.item_code.asc())
    )
    items = [value for value in items_result.scalars() if value]
    bounds = (
        await session.execute(
            select(
                func.min(WageEventRaw.created_at),
                func.max(WageEventRaw.created_at),
            ).where(WageEventRaw.resolved.is_(True))
        )
    ).one()

    return {
        "countries": [
            {"id": country.id, "code": country.code, "name": country.name, "income_tax": decimal_to_float(country.income_tax)}
            for country in countries
        ],
        "items": items,
        "data_bounds": {
            "from": bounds[0].isoformat() if bounds[0] else None,
            "to": bounds[1].isoformat() if bounds[1] else None,
        },
    }


async def get_status(session: AsyncSession) -> dict[str, Any]:
    states = {
        row.key: row
        for row in (
            await session.execute(
                select(CollectorState).where(
                    CollectorState.key.in_(
                        [
                            "collector.last_success_at",
                            "collector.last_error",
                            "collector.last_stats",
                            "collector.last_progress_at",
                            "collector.wage_cursor",
                        ]
                    )
                )
            )
        ).scalars()
    }
    counts = (
        await session.execute(
            select(
                func.count(WageEventRaw.transaction_id),
                func.count(case((WageEventRaw.resolved.is_(False), 1))),
                func.min(WageEventRaw.created_at),
                func.max(WageEventRaw.created_at),
                func.max(WageEventRaw.ingested_at),
            )
        )
    ).one()
    last_stats = states.get("collector.last_stats")
    return {
        "last_success_at": states.get("collector.last_success_at").string_value if states.get("collector.last_success_at") else None,
        "last_progress_at": states.get("collector.last_progress_at").string_value if states.get("collector.last_progress_at") else None,
        # Keep raw collector errors in the database for operator debugging, but do not
        # surface them through the public status payload used by the UI.
        "last_error": None,
        "cursor": states.get("collector.wage_cursor").json_value if states.get("collector.wage_cursor") else None,
        "last_stats": last_stats.json_value if last_stats else None,
        "raw_events": int(counts[0] or 0),
        "unresolved_events": int(counts[1] or 0),
        "latest_ingested_at": counts[4].isoformat() if counts[4] else None,
        "data_bounds": {
            "from": counts[2].isoformat() if counts[2] else None,
            "to": counts[3].isoformat() if counts[3] else None,
        },
    }


async def get_overview(
    session: AsyncSession,
    from_value: str | None,
    to_value: str | None,
    item_code: str | None = None,
    owner_country_id: str | None = None,
    foreign_filter: str = "all",
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    attributed_entries = _operating_tax_entries_subquery(start, end)
    stmt = (
        select(
            attributed_entries.c.recipient_country_id,
            func.sum(attributed_entries.c.tax_income).label("tax_income"),
            func.sum(attributed_entries.c.wages_paid).label("wages_paid"),
            func.sum(attributed_entries.c.transaction_count).label("transactions"),
        )
        .where(
            attributed_entries.c.event_hour >= start,
            attributed_entries.c.event_hour < end,
        )
    )
    if item_code:
        stmt = stmt.where(attributed_entries.c.item_code == item_code)
    if owner_country_id:
        stmt = stmt.where(attributed_entries.c.owner_country_id == owner_country_id)
    normalized_foreign_filter = normalize_foreign_filter(foreign_filter)
    if normalized_foreign_filter == "foreign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(True))
    elif normalized_foreign_filter == "nonforeign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(False))

    stmt = stmt.group_by(attributed_entries.c.recipient_country_id)
    rows = (await session.execute(stmt)).all()
    countries = {
        row.id: row
        for row in (await session.execute(select(CountryCache))).scalars()
    }

    leaderboard = []
    total_tax = Decimal("0")
    total_wages = Decimal("0")
    for row in rows:
        country = countries.get(row.recipient_country_id)
        if country is None:
            continue
        tax_income = row.tax_income or Decimal("0")
        wages_paid = row.wages_paid or Decimal("0")
        total_tax += tax_income
        total_wages += wages_paid
        effective_rate = (tax_income / wages_paid) if wages_paid else Decimal("0")
        leaderboard.append(
            {
                "country_id": country.id,
                "country_code": country.code,
                "country_name": country.name,
                "country_income_tax_rate": decimal_to_float(country.income_tax),
                "tax_income": decimal_to_float(tax_income),
                "wages_paid": decimal_to_float(wages_paid),
                "transactions": int(row.transactions or 0),
                "avg_tax_rate": decimal_to_float(effective_rate * Decimal("100")) if wages_paid else 0.0,
            }
        )

    leaderboard.sort(key=lambda item: item["tax_income"], reverse=True)
    return {
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "summary": {
            "tax_income": decimal_to_float(total_tax),
            "wages_paid": decimal_to_float(total_wages),
            "countries": len(leaderboard),
        },
        "countries": leaderboard,
    }


async def get_global_dataset(
    session: AsyncSession,
    *,
    from_value: str | None,
    to_value: str | None,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    attributed_entries = _operating_tax_entries_subquery(start, end)
    countries = {
        row.id: row
        for row in (await session.execute(select(CountryCache))).scalars()
    }
    stmt = (
        select(
            attributed_entries.c.event_hour,
            attributed_entries.c.recipient_country_id,
            attributed_entries.c.owner_country_id,
            attributed_entries.c.item_code,
            attributed_entries.c.is_core_region,
            attributed_entries.c.is_foreign_worker,
            attributed_entries.c.tax_income,
            attributed_entries.c.wages_paid,
            attributed_entries.c.transaction_count,
            attributed_entries.c.company_count,
            attributed_entries.c.worker_count,
        )
        .where(
            attributed_entries.c.event_hour >= start,
            attributed_entries.c.event_hour < end,
        )
        .order_by(
            attributed_entries.c.event_hour.asc(),
            attributed_entries.c.recipient_country_id.asc(),
            attributed_entries.c.owner_country_id.asc(),
            attributed_entries.c.item_code.asc(),
        )
    )
    rows = (await session.execute(stmt)).all()

    entries = []
    for row in rows:
        recipient_country = countries.get(row.recipient_country_id)
        owner_country = countries.get(row.owner_country_id)
        entries.append(
            {
                "hour_start": row.event_hour.isoformat(),
                "recipient_country_id": row.recipient_country_id,
                "recipient_country_code": recipient_country.code if recipient_country else None,
                "recipient_country_name": recipient_country.name if recipient_country else "Unknown",
                "owner_country_id": row.owner_country_id,
                "owner_country_code": owner_country.code if owner_country else None,
                "owner_country_name": owner_country.name if owner_country else "Unknown",
                "item_code": row.item_code or "unknown",
                "is_core_region": bool(row.is_core_region),
                "is_foreign_worker": bool(row.is_foreign_worker),
                "tax_income": decimal_to_float(row.tax_income or Decimal("0")),
                "wages_paid": decimal_to_float(row.wages_paid or Decimal("0")),
                "transactions": int(row.transaction_count or 0),
                "companies": int(row.company_count or 0),
                "workers": int(row.worker_count or 0),
            }
        )

    return {
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "entries": entries,
    }


async def get_game_weekly_income_taxes(
    session: AsyncSession,
    *,
    week: str,
    country_codes: list[str] | None = None,
) -> dict[str, Any]:
    start, end = resolve_game_week_range(week)
    codes = [code.strip().lower() for code in country_codes or [] if code.strip()]

    base_filters = (
        WageEventRaw.created_at >= start,
        WageEventRaw.created_at < end,
        WageEventRaw.resolved.is_(True),
        WageEventRaw.income_tax_money.is_not(None),
        WageEventRaw.operating_country_id.is_not(None),
    )
    is_core_region = func.coalesce(WageEventRaw.is_core_region, false())
    old_rule_filters = (
        *base_filters,
        WageEventRaw.created_at < FOREIGN_CITIZEN_TAX_EFFECTIVE_AT,
        WageEventRaw.region_initial_country_id.is_not(None),
    )
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
    old_core_owner_share = resistance_ratio * Decimal("0.4")
    old_work_country_share = Decimal("1") - old_core_owner_share
    new_rule_condition = WageEventRaw.created_at >= FOREIGN_CITIZEN_TAX_EFFECTIVE_AT
    foreign_worker_condition = (
        new_rule_condition
        & WageEventRaw.worker_country_id.is_not(None)
        & (WageEventRaw.worker_country_id != WageEventRaw.operating_country_id)
    )
    new_work_country_share = case(
        (
            foreign_worker_condition,
            Decimal("1") - FOREIGN_CITIZEN_TAX_SHARE,
        ),
        else_=Decimal("1"),
    )
    old_operating_entries = (
        select(
            WageEventRaw.operating_country_id.label("recipient_country_id"),
            case(
                (is_core_region.is_(False), WageEventRaw.income_tax_money * old_work_country_share),
                else_=WageEventRaw.income_tax_money,
            ).label("tax_income"),
            WageEventRaw.transaction_id.label("transaction_id"),
        )
        .where(*old_rule_filters)
    )
    old_original_country_entries = (
        select(
            WageEventRaw.region_initial_country_id.label("recipient_country_id"),
            (WageEventRaw.income_tax_money * old_core_owner_share).label("tax_income"),
            WageEventRaw.transaction_id.label("transaction_id"),
        )
        .where(
            *old_rule_filters,
            is_core_region.is_(False),
            WageEventRaw.region_initial_country_id != WageEventRaw.operating_country_id,
            WageEventRaw.region_resistance.is_not(None),
            WageEventRaw.region_resistance > 0,
        )
    )
    new_operating_entries = (
        select(
            WageEventRaw.operating_country_id.label("recipient_country_id"),
            (WageEventRaw.income_tax_money * new_work_country_share).label("tax_income"),
            WageEventRaw.transaction_id.label("transaction_id"),
        )
        .where(*base_filters, new_rule_condition)
    )
    new_citizenship_entries = (
        select(
            WageEventRaw.worker_country_id.label("recipient_country_id"),
            (WageEventRaw.income_tax_money * FOREIGN_CITIZEN_TAX_SHARE).label("tax_income"),
            WageEventRaw.transaction_id.label("transaction_id"),
        )
        .where(*base_filters, foreign_worker_condition)
    )
    attributed_entries = (
        old_operating_entries.union_all(
            old_original_country_entries,
            new_operating_entries,
            new_citizenship_entries,
        ).subquery("game_week_income_entries")
    )
    stmt = (
        select(
            CountryCache.id,
            CountryCache.code,
            CountryCache.name,
            func.sum(attributed_entries.c.tax_income),
            func.count(attributed_entries.c.transaction_id),
        )
        .join(attributed_entries, attributed_entries.c.recipient_country_id == CountryCache.id)
        .group_by(CountryCache.id, CountryCache.code, CountryCache.name)
        .order_by(CountryCache.code.asc())
    )
    if codes:
        stmt = stmt.where(func.lower(CountryCache.code).in_(codes))

    countries = []
    for country_id, code, name, income_taxes, transactions in (await session.execute(stmt)).all():
        income_taxes = income_taxes or Decimal("0")
        countries.append(
            {
                "country_id": country_id,
                "country_code": code,
                "country_name": name,
                "income_taxes": decimal_to_float(income_taxes),
                "income_taxes_display": format_game_compact_money(income_taxes),
                "transactions": int(transactions or 0),
            }
        )

    return {
        "week": week,
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "countries": countries,
    }


def _apply_country_filters(
    stmt: Select[Any],
    attributed_entries: Any,
    *,
    country_id: str,
    start: datetime,
    end: datetime,
    item_code: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> Select[Any]:
    stmt = stmt.where(
        attributed_entries.c.recipient_country_id == country_id,
        attributed_entries.c.event_hour >= start,
        attributed_entries.c.event_hour < end,
    )
    if item_code:
        stmt = stmt.where(attributed_entries.c.item_code == item_code)
    if owner_country_id:
        stmt = stmt.where(attributed_entries.c.owner_country_id == owner_country_id)
    normalized_foreign_filter = normalize_foreign_filter(foreign_filter)
    if normalized_foreign_filter == "foreign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(True))
    elif normalized_foreign_filter == "nonforeign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(False))
    return stmt


def _operating_tax_entries_subquery(start: datetime, end: datetime):
    return (
        select(
            HourlyTaxRollup.hour_start.label("event_hour"),
            HourlyTaxRollup.operating_country_id.label("recipient_country_id"),
            HourlyTaxRollup.owner_country_id.label("owner_country_id"),
            HourlyTaxRollup.item_code.label("item_code"),
            HourlyTaxRollup.is_core_region.label("is_core_region"),
            HourlyTaxRollup.is_foreign_worker.label("is_foreign_worker"),
            HourlyTaxRollup.tax_income_sum.label("tax_income"),
            HourlyTaxRollup.wage_money_sum.label("wages_paid"),
            HourlyTaxRollup.transaction_count.label("transaction_count"),
            HourlyTaxRollup.company_count.label("company_count"),
            HourlyTaxRollup.worker_count.label("worker_count"),
        )
        .select_from(HourlyTaxRollup)
        .where(
            HourlyTaxRollup.hour_start >= start,
            HourlyTaxRollup.hour_start < end,
        )
        .subquery("operating_tax_entries")
    )


async def get_country_summary(
    session: AsyncSession,
    country: CountryCache,
    *,
    from_value: str | None,
    to_value: str | None,
    item_code: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    entries = await _get_reference_country_tax_entries(session, country.id, start, end)
    filtered_entries = _filter_reference_entries(
        entries,
        item_code=item_code,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    )
    local_counts = await _get_local_country_summary_counts(
        session,
        country_id=country.id,
        start=start,
        end=end,
        item_code=item_code,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    )
    tax_income = _sum_reference_entries(filtered_entries, "taxes")
    wages_paid = _sum_reference_entries(filtered_entries, "wages")
    effective_rate = (tax_income / wages_paid) if wages_paid else Decimal("0")
    return {
        "country_id": country.id,
        "country_code": country.code,
        "country_name": country.name,
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "summary": {
            "tax_income": decimal_to_float(tax_income),
            "wages_paid": decimal_to_float(wages_paid),
            "transactions": local_counts["transactions"],
            "companies": _sum_reference_companies(filtered_entries),
            "workers": local_counts["workers"],
            "items": len({str(entry.get("itemCode")) for entry in filtered_entries if entry.get("itemCode")}),
            "non_foreign_tax_income": decimal_to_float(
                _sum_reference_entries(
                    [entry for entry in filtered_entries if not bool(entry.get("foreign"))],
                    "taxes",
                )
            ),
            "foreign_tax_income": decimal_to_float(
                _sum_reference_entries(
                    [entry for entry in filtered_entries if bool(entry.get("foreign"))],
                    "taxes",
                )
            ),
            "avg_tax_rate": decimal_to_float(effective_rate * Decimal("100")) if wages_paid else 0.0,
        },
    }


async def get_country_timeseries(
    session: AsyncSession,
    country: CountryCache,
    *,
    from_value: str | None,
    to_value: str | None,
    item_code: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    attributed_entries = _operating_tax_entries_subquery(start, end)
    stmt = (
        select(
            attributed_entries.c.event_hour,
            func.sum(attributed_entries.c.tax_income),
            func.sum(attributed_entries.c.wages_paid),
            func.sum(attributed_entries.c.transaction_count),
        )
        .where(
            attributed_entries.c.recipient_country_id == country.id,
            attributed_entries.c.event_hour >= start,
            attributed_entries.c.event_hour < end,
        )
    )
    if item_code:
        stmt = stmt.where(attributed_entries.c.item_code == item_code)
    if owner_country_id:
        stmt = stmt.where(attributed_entries.c.owner_country_id == owner_country_id)
    normalized_foreign_filter = normalize_foreign_filter(foreign_filter)
    if normalized_foreign_filter == "foreign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(True))
    elif normalized_foreign_filter == "nonforeign":
        stmt = stmt.where(attributed_entries.c.is_foreign_worker.is_(False))

    stmt = stmt.group_by(attributed_entries.c.event_hour).order_by(attributed_entries.c.event_hour.asc())
    rows = (await session.execute(stmt)).all()
    return {
        "country_code": country.code,
        "points": [
            {
                "hour_start": row[0].isoformat(),
                "tax_income": decimal_to_float(row[1] or Decimal("0")),
                "wages_paid": decimal_to_float(row[2] or Decimal("0")),
                "transactions": int(row[3] or 0),
            }
            for row in rows
        ],
    }


async def get_country_item_breakdown(
    session: AsyncSession,
    country: CountryCache,
    *,
    from_value: str | None,
    to_value: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    entries = _filter_reference_entries(
        await _get_reference_country_tax_entries(session, country.id, start, end),
        item_code=None,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    )
    total_tax = _sum_reference_entries(entries, "taxes")
    local_counts = await _get_local_country_item_counts(
        session,
        country_id=country.id,
        start=start,
        end=end,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    )
    grouped_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped_entries.setdefault(str(entry.get("itemCode") or "unknown"), []).append(entry)

    entries = []
    for item, item_entries in grouped_entries.items():
        tax_income = _sum_reference_entries(item_entries, "taxes")
        wages_paid = _sum_reference_entries(item_entries, "wages")
        effective_rate = _average_reference_tax_rate(item_entries)
        entries.append(
            {
                "item_code": item,
                "tax_income": decimal_to_float(tax_income),
                "wages_paid": decimal_to_float(wages_paid),
                "avg_tax_rate": decimal_to_float(effective_rate),
                "companies": _sum_reference_companies(item_entries),
                "transactions": local_counts.get(item, 0),
                "share": decimal_to_float((tax_income / total_tax) * Decimal("100")) if total_tax else 0.0,
            }
        )
    entries.sort(key=lambda item: item["tax_income"], reverse=True)
    return {"country_code": country.code, "items": entries}


async def get_country_owner_breakdown(
    session: AsyncSession,
    country: CountryCache,
    *,
    from_value: str | None,
    to_value: str | None,
    item_code: str | None,
    foreign_filter: str,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    reference_entries = _filter_reference_entries(
        await _get_reference_country_tax_entries(session, country.id, start, end),
        item_code=item_code,
        owner_country_id=None,
        foreign_filter=foreign_filter,
    )
    total_tax = _sum_reference_entries(reference_entries, "taxes")
    local_counts = await _get_local_country_owner_counts(
        session,
        country_id=country.id,
        start=start,
        end=end,
        item_code=item_code,
        foreign_filter=foreign_filter,
    )
    grouped_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in reference_entries:
        grouped_entries.setdefault(str(entry.get("ownerCountryId") or "unknown"), []).append(entry)

    countries = {
        row.id: row
        for row in (await session.execute(select(CountryCache))).scalars()
    }
    entries = []
    for owner_country_id, owner_entries in grouped_entries.items():
        owner_country = countries.get(owner_country_id)
        tax_income = _sum_reference_entries(owner_entries, "taxes")
        wages_paid = _sum_reference_entries(owner_entries, "wages")
        effective_rate = _average_reference_tax_rate(owner_entries)
        entries.append(
            {
                "owner_country_id": owner_country_id,
                "owner_country_code": owner_country.code if owner_country else None,
                "owner_country_name": owner_country.name if owner_country else "Unknown",
                "tax_income": decimal_to_float(tax_income),
                "wages_paid": decimal_to_float(wages_paid),
                "avg_tax_rate": decimal_to_float(effective_rate),
                "companies": _sum_reference_companies(owner_entries),
                "transactions": local_counts.get(owner_country_id, 0),
                "share": decimal_to_float((tax_income / total_tax) * Decimal("100")) if total_tax else 0.0,
            }
        )
    entries.sort(key=lambda item: item["tax_income"], reverse=True)
    return {"country_code": country.code, "owners": entries}


async def get_country_dataset(
    session: AsyncSession,
    country: CountryCache,
    *,
    from_value: str | None,
    to_value: str | None,
) -> dict[str, Any]:
    start, end = resolve_hour_range(from_value, to_value)
    countries = {
        row.id: row
        for row in (await session.execute(select(CountryCache))).scalars()
    }
    reference_entries = await _get_reference_country_tax_entries(session, country.id, start, end)
    entries = []
    for entry in reference_entries:
        owner_country_id = str(entry.get("ownerCountryId") or "")
        owner_country = countries.get(owner_country_id)
        entries.append(
            {
                "hour_start": entry.get("hourStart") or start.isoformat(),
                "region_id": entry.get("regionId"),
                "owner_country_id": owner_country_id,
                "owner_country_code": owner_country.code if owner_country else None,
                "owner_country_name": owner_country.name if owner_country else "Unknown",
                "item_code": str(entry.get("itemCode") or "unknown"),
                "is_core_region": bool(entry.get("core")),
                "is_foreign_worker": bool(entry.get("foreign")),
                "tax_income": decimal_to_float(_reference_decimal(entry, "taxes")),
                "wages_paid": decimal_to_float(_reference_decimal(entry, "wages")),
                "transactions": int(entry.get("transactions") or 0),
                "companies": int(entry.get("companies") or 0),
                "workers": int(entry.get("workers") or 0),
            }
        )

    return {
        "country_id": country.id,
        "country_code": country.code,
        "country_name": country.name,
        "country_income_tax_rate": decimal_to_float(country.income_tax),
        "from": start.isoformat(),
        "to_exclusive": end.isoformat(),
        "entries": entries,
    }


async def _get_local_country_summary_counts(
    session: AsyncSession,
    *,
    country_id: str,
    start: datetime,
    end: datetime,
    item_code: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> dict[str, int]:
    attributed_entries = _operating_tax_entries_subquery(start, end)
    stmt = _apply_country_filters(
        select(
            func.sum(attributed_entries.c.transaction_count),
            func.sum(attributed_entries.c.worker_count),
        ).select_from(attributed_entries),
        attributed_entries,
        country_id=country_id,
        start=start,
        end=end,
        item_code=item_code,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    )
    row = (await session.execute(stmt)).one()
    return {
        "transactions": int(row[0] or 0),
        "workers": int(row[1] or 0),
    }


async def _get_local_country_item_counts(
    session: AsyncSession,
    *,
    country_id: str,
    start: datetime,
    end: datetime,
    owner_country_id: str | None,
    foreign_filter: str,
) -> dict[str, int]:
    attributed_entries = _operating_tax_entries_subquery(start, end)
    stmt = _apply_country_filters(
        select(
            attributed_entries.c.item_code,
            func.sum(attributed_entries.c.transaction_count),
        ).select_from(attributed_entries),
        attributed_entries,
        country_id=country_id,
        start=start,
        end=end,
        item_code=None,
        owner_country_id=owner_country_id,
        foreign_filter=foreign_filter,
    ).group_by(attributed_entries.c.item_code)
    return {
        str(row[0] or "unknown"): int(row[1] or 0)
        for row in (await session.execute(stmt)).all()
    }


async def _get_local_country_owner_counts(
    session: AsyncSession,
    *,
    country_id: str,
    start: datetime,
    end: datetime,
    item_code: str | None,
    foreign_filter: str,
) -> dict[str, int]:
    attributed_entries = _operating_tax_entries_subquery(start, end)
    stmt = _apply_country_filters(
        select(
            attributed_entries.c.owner_country_id,
            func.sum(attributed_entries.c.transaction_count),
        ).select_from(attributed_entries),
        attributed_entries,
        country_id=country_id,
        start=start,
        end=end,
        item_code=item_code,
        owner_country_id=None,
        foreign_filter=foreign_filter,
    ).group_by(attributed_entries.c.owner_country_id)
    return {
        str(row[0] or "unknown"): int(row[1] or 0)
        for row in (await session.execute(stmt)).all()
    }


async def _get_reference_country_tax_entries(
    session: AsyncSession,
    country_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(HourlyTaxRollup)
        .where(
            HourlyTaxRollup.operating_country_id == country_id,
            HourlyTaxRollup.hour_start >= start,
            HourlyTaxRollup.hour_start < end,
        )
        .order_by(
            HourlyTaxRollup.hour_start.asc(),
            HourlyTaxRollup.item_code.asc(),
            HourlyTaxRollup.owner_country_id.asc(),
        )
    )

    entries: list[dict[str, Any]] = []
    for row in result.scalars():
        tax_income = row.tax_income_sum or Decimal("0")
        wages_paid = row.wage_money_sum or Decimal("0")
        entries.append(
            {
                "hourStart": row.hour_start.isoformat(),
                "itemCode": row.item_code,
                "ownerCountryId": row.owner_country_id,
                "core": row.is_core_region,
                "foreign": row.is_foreign_worker,
                "taxes": float(tax_income),
                "wages": float(wages_paid),
                "taxRate": float((tax_income / wages_paid) * Decimal("100")) if wages_paid else 0.0,
                "transactions": row.transaction_count,
                "companies": row.company_count,
                "workers": row.worker_count,
            }
        )
    return entries


def _filter_reference_entries(
    entries: list[dict[str, Any]],
    *,
    item_code: str | None,
    owner_country_id: str | None,
    foreign_filter: str,
) -> list[dict[str, Any]]:
    filtered = entries
    if item_code:
        filtered = [entry for entry in filtered if entry.get("itemCode") == item_code]
    if owner_country_id:
        filtered = [entry for entry in filtered if entry.get("ownerCountryId") == owner_country_id]
    normalized_foreign_filter = normalize_foreign_filter(foreign_filter)
    if normalized_foreign_filter == "foreign":
        filtered = [entry for entry in filtered if bool(entry.get("foreign"))]
    elif normalized_foreign_filter == "nonforeign":
        filtered = [entry for entry in filtered if not bool(entry.get("foreign"))]
    return filtered


def _reference_decimal(entry: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(entry.get(key) or "0"))


def _sum_reference_entries(entries: list[dict[str, Any]], key: str) -> Decimal:
    return sum((_reference_decimal(entry, key) for entry in entries), Decimal("0"))


def _sum_reference_companies(entries: list[dict[str, Any]]) -> int:
    return sum(int(entry.get("companies") or 0) for entry in entries)


def _average_reference_tax_rate(entries: list[dict[str, Any]]) -> Decimal:
    if not entries:
        return Decimal("0")
    total_taxes = _sum_reference_entries(entries, "taxes")
    total_wages = _sum_reference_entries(entries, "wages")
    return (total_taxes / total_wages) * Decimal("100") if total_wages else Decimal("0")
