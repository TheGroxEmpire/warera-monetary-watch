"""optimize tax rollup queries and shrink resolved raw payloads"""

from __future__ import annotations

from alembic import op

revision = "20260425_0003"
down_revision = "20260423_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_wage_events_raw_event_hour_resolved",
        "wage_events_raw",
        ["event_hour", "resolved"],
        unique=False,
    )
    op.create_index(
        "ix_hourly_tax_rollups_owner_hour",
        "hourly_tax_rollups",
        ["owner_country_id", "hour_start"],
        unique=False,
    )
    op.create_index(
        "ix_hourly_tax_rollups_item_hour",
        "hourly_tax_rollups",
        ["item_code", "hour_start"],
        unique=False,
    )
    op.execute("UPDATE wage_events_raw SET raw_payload = '{}'::json WHERE resolved IS TRUE")


def downgrade() -> None:
    op.drop_index("ix_hourly_tax_rollups_item_hour", table_name="hourly_tax_rollups")
    op.drop_index("ix_hourly_tax_rollups_owner_hour", table_name="hourly_tax_rollups")
    op.drop_index("ix_wage_events_raw_event_hour_resolved", table_name="wage_events_raw")
