"""initial schema"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260421_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collector_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("string_value", sa.Text(), nullable=True),
        sa.Column("json_value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "country_cache",
        sa.Column("id", sa.String(length=24), primary_key=True),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("income_tax", sa.Numeric(10, 3), nullable=False),
        sa.Column("market_tax", sa.Numeric(10, 3), nullable=False),
        sa.Column("self_work_tax", sa.Numeric(10, 3), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_country_cache_code", "country_cache", ["code"], unique=True)
    op.create_index("ix_country_cache_name", "country_cache", ["name"], unique=False)

    op.create_table(
        "region_cache",
        sa.Column("id", sa.String(length=24), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("country_id", sa.String(length=24), nullable=False),
        sa.Column("initial_country_id", sa.String(length=24), nullable=False),
        sa.Column("main_city", sa.String(length=128), nullable=True),
        sa.Column("is_capital", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("development", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("deposit_type", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_region_cache_code", "region_cache", ["code"], unique=True)
    op.create_index("ix_region_cache_country_id", "region_cache", ["country_id"], unique=False)

    op.create_table(
        "user_cache",
        sa.Column("id", sa.String(length=24), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("country_id", sa.String(length=24), nullable=False),
        sa.Column("current_company_id", sa.String(length=24), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_cache_country_id", "user_cache", ["country_id"], unique=False)
    op.create_index("ix_user_cache_current_company_id", "user_cache", ["current_company_id"], unique=False)

    op.create_table(
        "company_cache",
        sa.Column("id", sa.String(length=24), primary_key=True),
        sa.Column("owner_user_id", sa.String(length=24), nullable=False),
        sa.Column("region_id", sa.String(length=24), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("worker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("production", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("estimated_value", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_company_cache_owner_user_id", "company_cache", ["owner_user_id"], unique=False)
    op.create_index("ix_company_cache_region_id", "company_cache", ["region_id"], unique=False)
    op.create_index("ix_company_cache_item_code", "company_cache", ["item_code"], unique=False)

    op.create_table(
        "wage_events_raw",
        sa.Column("transaction_id", sa.String(length=24), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("money", sa.Numeric(18, 6), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("seller_user_id", sa.String(length=24), nullable=False),
        sa.Column("buyer_user_id", sa.String(length=24), nullable=False),
        sa.Column("seller_company_id", sa.String(length=24), nullable=True),
        sa.Column("operating_region_id", sa.String(length=24), nullable=True),
        sa.Column("operating_country_id", sa.String(length=24), nullable=True),
        sa.Column("owner_country_id", sa.String(length=24), nullable=True),
        sa.Column("item_code", sa.String(length=64), nullable=True),
        sa.Column("is_core_region", sa.Boolean(), nullable=True),
        sa.Column("income_tax_rate", sa.Numeric(10, 3), nullable=True),
        sa.Column("income_tax_money", sa.Numeric(18, 6), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("unresolved_reason", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wage_events_raw_created_at", "wage_events_raw", ["created_at"], unique=False)
    op.create_index("ix_wage_events_raw_event_hour", "wage_events_raw", ["event_hour"], unique=False)
    op.create_index("ix_wage_events_raw_operating_country_id", "wage_events_raw", ["operating_country_id"], unique=False)
    op.create_index("ix_wage_events_raw_owner_country_id", "wage_events_raw", ["owner_country_id"], unique=False)
    op.create_index("ix_wage_events_raw_item_code", "wage_events_raw", ["item_code"], unique=False)
    op.create_index("ix_wage_events_raw_resolved", "wage_events_raw", ["resolved"], unique=False)
    op.create_index(
        "ix_wage_events_raw_country_hour_resolved",
        "wage_events_raw",
        ["operating_country_id", "event_hour", "resolved"],
        unique=False,
    )

    op.create_table(
        "hourly_tax_rollups",
        sa.Column("hour_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("operating_country_id", sa.String(length=24), primary_key=True),
        sa.Column("item_code", sa.String(length=64), primary_key=True),
        sa.Column("owner_country_id", sa.String(length=24), primary_key=True),
        sa.Column("is_core_region", sa.Boolean(), primary_key=True),
        sa.Column("tax_income_sum", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("wage_money_sum", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("company_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_hourly_tax_rollups_country_hour",
        "hourly_tax_rollups",
        ["operating_country_id", "hour_start"],
        unique=False,
    )
    op.create_index("ix_hourly_tax_rollups_hour_start", "hourly_tax_rollups", ["hour_start"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_hourly_tax_rollups_hour_start", table_name="hourly_tax_rollups")
    op.drop_index("ix_hourly_tax_rollups_country_hour", table_name="hourly_tax_rollups")
    op.drop_table("hourly_tax_rollups")

    op.drop_index("ix_wage_events_raw_country_hour_resolved", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_resolved", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_item_code", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_owner_country_id", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_operating_country_id", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_event_hour", table_name="wage_events_raw")
    op.drop_index("ix_wage_events_raw_created_at", table_name="wage_events_raw")
    op.drop_table("wage_events_raw")

    op.drop_index("ix_company_cache_item_code", table_name="company_cache")
    op.drop_index("ix_company_cache_region_id", table_name="company_cache")
    op.drop_index("ix_company_cache_owner_user_id", table_name="company_cache")
    op.drop_table("company_cache")

    op.drop_index("ix_user_cache_current_company_id", table_name="user_cache")
    op.drop_index("ix_user_cache_country_id", table_name="user_cache")
    op.drop_table("user_cache")

    op.drop_index("ix_region_cache_country_id", table_name="region_cache")
    op.drop_index("ix_region_cache_code", table_name="region_cache")
    op.drop_table("region_cache")

    op.drop_index("ix_country_cache_name", table_name="country_cache")
    op.drop_index("ix_country_cache_code", table_name="country_cache")
    op.drop_table("country_cache")

    op.drop_table("collector_state")
