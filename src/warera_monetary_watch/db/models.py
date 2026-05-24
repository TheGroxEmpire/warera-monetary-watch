from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from warera_monetary_watch.db.base import Base


class CollectorState(Base):
    __tablename__ = "collector_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    string_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CountryCache(Base):
    __tablename__ = "country_cache"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    income_tax: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    market_tax: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    self_work_tax: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class RegionCache(Base):
    __tablename__ = "region_cache"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    country_id: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    initial_country_id: Mapped[str] = mapped_column(String(24), nullable=False)
    main_city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_capital: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    development: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    deposit_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserCache(Base):
    __tablename__ = "user_cache"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    country_id: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    current_company_id: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CompanyCache(Base):
    __tablename__ = "company_cache"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    region_id: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    item_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    worker_count: Mapped[int] = mapped_column(nullable=False, default=0)
    production: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    estimated_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WageEventRaw(Base):
    __tablename__ = "wage_events_raw"
    __table_args__ = (
        Index(
            "ix_wage_events_raw_country_hour_resolved",
            "operating_country_id",
            "event_hour",
            "resolved",
        ),
    )

    transaction_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    event_hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    money: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    seller_user_id: Mapped[str] = mapped_column(String(24), nullable=False)
    buyer_user_id: Mapped[str] = mapped_column(String(24), nullable=False)
    seller_company_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    operating_region_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    operating_country_id: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    region_initial_country_id: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    owner_country_id: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    item_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_core_region: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    region_resistance: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    region_resistance_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    income_tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    income_tax_money: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    unresolved_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HourlyTaxRollup(Base):
    __tablename__ = "hourly_tax_rollups"

    hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    operating_country_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    item_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_country_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    is_core_region: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    tax_income_sum: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    wage_money_sum: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    transaction_count: Mapped[int] = mapped_column(nullable=False, default=0)
    company_count: Mapped[int] = mapped_column(nullable=False, default=0)
    worker_count: Mapped[int] = mapped_column(nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "CollectorState",
    "CompanyCache",
    "CountryCache",
    "HourlyTaxRollup",
    "RegionCache",
    "UserCache",
    "WageEventRaw",
]
