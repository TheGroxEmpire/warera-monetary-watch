"""correct income tax calculation for gross wage transactions"""

from __future__ import annotations

from alembic import op

revision = "20260513_0004"
down_revision = "20260425_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    _rebuild_rollups()


def downgrade() -> None:
    op.execute(
        """
        update wage_events_raw
        set income_tax_money = round((money * income_tax_rate) / 100, 6)
        where income_tax_money is not null
          and income_tax_rate is not null
          and income_tax_rate > 0
        """
    )
    _rebuild_rollups()


def _rebuild_rollups() -> None:
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
