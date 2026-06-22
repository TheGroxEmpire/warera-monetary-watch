"""attribute foreign citizen income tax from cutover forward"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260614_0006"
down_revision = "20260525_0005"
branch_labels = None
depends_on = None

EFFECTIVE_AT = "2026-06-13 16:45:00+00"
EFFECTIVE_HOUR = "2026-06-13 16:00:00+00"


def upgrade() -> None:
    op.add_column(
        "wage_events_raw",
        sa.Column("worker_country_id", sa.String(length=24), nullable=True),
    )
    op.create_index(
        "ix_wage_events_raw_worker_country_id",
        "wage_events_raw",
        ["worker_country_id"],
        unique=False,
    )
    op.add_column(
        "hourly_tax_rollups",
        sa.Column(
            "is_foreign_worker",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.drop_constraint("hourly_tax_rollups_pkey", "hourly_tax_rollups", type_="primary")
    op.create_primary_key(
        "hourly_tax_rollups_pkey",
        "hourly_tax_rollups",
        [
            "hour_start",
            "operating_country_id",
            "item_code",
            "owner_country_id",
            "is_core_region",
            "is_foreign_worker",
        ],
    )
    op.execute(
        """
        update wage_events_raw as w
        set worker_country_id = nullif(u.country_id, '')
        from user_cache as u
        where w.seller_user_id = u.id
          and w.worker_country_id is null
          and nullif(u.country_id, '') is not null
        """
    )
    _rebuild_cutover_rollups()


def downgrade() -> None:
    _rebuild_resistance_split_rollups()
    op.drop_constraint("hourly_tax_rollups_pkey", "hourly_tax_rollups", type_="primary")
    op.create_primary_key(
        "hourly_tax_rollups_pkey",
        "hourly_tax_rollups",
        [
            "hour_start",
            "operating_country_id",
            "item_code",
            "owner_country_id",
            "is_core_region",
        ],
    )
    op.drop_column("hourly_tax_rollups", "is_foreign_worker")
    op.drop_index("ix_wage_events_raw_worker_country_id", table_name="wage_events_raw")
    op.drop_column("wage_events_raw", "worker_country_id")


def _rebuild_cutover_rollups() -> None:
    op.execute(
        f"""
        delete from hourly_tax_rollups
        where hour_start >= timestamp with time zone '{EFFECTIVE_HOUR}'
        """
    )
    op.execute(
        f"""
        insert into hourly_tax_rollups (
            hour_start,
            operating_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker,
            tax_income_sum,
            wage_money_sum,
            transaction_count,
            company_count,
            worker_count
        )
        with base as (
            select
                event_hour,
                created_at,
                operating_country_id,
                region_initial_country_id,
                worker_country_id,
                item_code,
                owner_country_id,
                coalesce(is_core_region, false) as is_core_region,
                income_tax_money,
                money,
                transaction_id,
                seller_company_id,
                seller_user_id,
                case
                    when region_resistance is null
                      or region_resistance_max is null
                      or region_resistance_max <= 0 then 0
                    when region_resistance < 0 then 0
                    when region_resistance > region_resistance_max then 1
                    else region_resistance / region_resistance_max
                end as resistance_ratio,
                case
                    when created_at >= timestamp with time zone '{EFFECTIVE_AT}'
                      and worker_country_id is not null
                      and worker_country_id <> operating_country_id then true
                    else false
                end as is_foreign_worker
            from wage_events_raw
            where resolved is true
              and event_hour >= timestamp with time zone '{EFFECTIVE_HOUR}'
              and operating_country_id is not null
              and owner_country_id is not null
              and item_code is not null
              and income_tax_money is not null
        ),
        attributed_entries as (
            select
                event_hour as hour_start,
                operating_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                is_core_region,
                false as is_foreign_worker,
                case
                    when is_core_region then income_tax_money
                    else income_tax_money * (1 - (resistance_ratio * 0.4))
                end as tax_income,
                case
                    when is_core_region then money
                    else money * (1 - (resistance_ratio * 0.4))
                end as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            where created_at < timestamp with time zone '{EFFECTIVE_AT}'
              and region_initial_country_id is not null
            union all
            select
                event_hour as hour_start,
                region_initial_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                true as is_core_region,
                false as is_foreign_worker,
                income_tax_money * resistance_ratio * 0.4 as tax_income,
                money * resistance_ratio * 0.4 as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            where created_at < timestamp with time zone '{EFFECTIVE_AT}'
              and is_core_region is false
              and region_initial_country_id is not null
              and region_initial_country_id <> operating_country_id
              and resistance_ratio > 0
            union all
            select
                event_hour as hour_start,
                operating_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                is_core_region,
                is_foreign_worker,
                income_tax_money * (
                    case when is_foreign_worker then 0.7 else 1 end
                ) as tax_income,
                money * (
                    case when is_foreign_worker then 0.7 else 1 end
                ) as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            where created_at >= timestamp with time zone '{EFFECTIVE_AT}'
            union all
            select
                event_hour as hour_start,
                worker_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                is_core_region,
                true as is_foreign_worker,
                income_tax_money * 0.3 as tax_income,
                money * 0.3 as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            where created_at >= timestamp with time zone '{EFFECTIVE_AT}'
              and is_foreign_worker is true
        )
        select
            hour_start,
            recipient_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker,
            sum(tax_income) as tax_income_sum,
            sum(wage_money) as wage_money_sum,
            count(transaction_id) as transaction_count,
            count(distinct seller_company_id) as company_count,
            count(distinct seller_user_id) as worker_count
        from attributed_entries
        group by
            hour_start,
            recipient_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker
        """
    )


def _rebuild_resistance_split_rollups() -> None:
    op.execute("delete from hourly_tax_rollups")
    op.execute(
        """
        insert into hourly_tax_rollups (
            hour_start,
            operating_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker,
            tax_income_sum,
            wage_money_sum,
            transaction_count,
            company_count,
            worker_count
        )
        with base as (
            select
                event_hour,
                operating_country_id,
                region_initial_country_id,
                item_code,
                owner_country_id,
                coalesce(is_core_region, false) as is_core_region,
                income_tax_money,
                money,
                transaction_id,
                seller_company_id,
                seller_user_id,
                case
                    when region_resistance is null
                      or region_resistance_max is null
                      or region_resistance_max <= 0 then 0
                    when region_resistance < 0 then 0
                    when region_resistance > region_resistance_max then 1
                    else region_resistance / region_resistance_max
                end as resistance_ratio
            from wage_events_raw
            where resolved is true
              and operating_country_id is not null
              and region_initial_country_id is not null
              and owner_country_id is not null
              and item_code is not null
              and income_tax_money is not null
        ),
        split_entries as (
            select
                event_hour as hour_start,
                operating_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                is_core_region,
                false as is_foreign_worker,
                case
                    when is_core_region then income_tax_money
                    else income_tax_money * (1 - (resistance_ratio * 0.4))
                end as tax_income,
                case
                    when is_core_region then money
                    else money * (1 - (resistance_ratio * 0.4))
                end as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            union all
            select
                event_hour as hour_start,
                region_initial_country_id as recipient_country_id,
                item_code,
                owner_country_id,
                true as is_core_region,
                false as is_foreign_worker,
                income_tax_money * resistance_ratio * 0.4 as tax_income,
                money * resistance_ratio * 0.4 as wage_money,
                transaction_id,
                seller_company_id,
                seller_user_id
            from base
            where is_core_region is false
              and region_initial_country_id <> operating_country_id
              and resistance_ratio > 0
        )
        select
            hour_start,
            recipient_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker,
            sum(tax_income) as tax_income_sum,
            sum(wage_money) as wage_money_sum,
            count(transaction_id) as transaction_count,
            count(distinct seller_company_id) as company_count,
            count(distinct seller_user_id) as worker_count
        from split_entries
        group by
            hour_start,
            recipient_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            is_foreign_worker
        """
    )
