"""align income tax with country account ledger rules"""

from __future__ import annotations

from alembic import op

revision = "20260525_0005"
down_revision = "20260513_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        update wage_events_raw
        set income_tax_money = round((money * income_tax_rate) / 100, 6)
        where income_tax_money is not null
          and income_tax_rate is not null
          and income_tax_rate > 0
        """
    )
    op.execute(
        """
        update wage_events_raw
        set income_tax_money = 0
        where income_tax_money is not null
          and income_tax_rate is not null
          and income_tax_rate <= 0
        """
    )
    _rebuild_split_rollups()


def downgrade() -> None:
    op.execute(
        """
        update wage_events_raw
        set income_tax_money = round((money * income_tax_rate) / (100 + income_tax_rate), 6)
        where income_tax_money is not null
          and income_tax_rate is not null
          and income_tax_rate > 0
        """
    )
    op.execute(
        """
        update wage_events_raw
        set income_tax_money = 0
        where income_tax_money is not null
          and income_tax_rate is not null
          and income_tax_rate <= 0
        """
    )
    _rebuild_operating_rollups()


def _rebuild_split_rollups() -> None:
    op.execute("delete from hourly_tax_rollups")
    op.execute(
        """
        insert into hourly_tax_rollups (
            hour_start,
            operating_country_id,
            item_code,
            owner_country_id,
            is_core_region,
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
            is_core_region
        """
    )


def _rebuild_operating_rollups() -> None:
    op.execute("delete from hourly_tax_rollups")
    op.execute(
        """
        insert into hourly_tax_rollups (
            hour_start,
            operating_country_id,
            item_code,
            owner_country_id,
            is_core_region,
            tax_income_sum,
            wage_money_sum,
            transaction_count,
            company_count,
            worker_count
        )
        select
            event_hour as hour_start,
            operating_country_id,
            item_code,
            owner_country_id,
            coalesce(is_core_region, false) as is_core_region,
            sum(income_tax_money) as tax_income_sum,
            sum(money) as wage_money_sum,
            count(transaction_id) as transaction_count,
            count(distinct seller_company_id) as company_count,
            count(distinct seller_user_id) as worker_count
        from wage_events_raw
        where resolved is true
          and operating_country_id is not null
          and owner_country_id is not null
          and item_code is not null
          and income_tax_money is not null
        group by
            event_hour,
            operating_country_id,
            item_code,
            owner_country_id,
            coalesce(is_core_region, false)
        """
    )
