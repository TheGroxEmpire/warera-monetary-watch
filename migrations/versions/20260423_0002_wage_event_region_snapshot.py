"""store per-event region control snapshot"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260423_0002"
down_revision = "20260421_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wage_events_raw", sa.Column("region_initial_country_id", sa.String(length=24), nullable=True))
    op.add_column("wage_events_raw", sa.Column("region_resistance", sa.Numeric(18, 6), nullable=True))
    op.add_column("wage_events_raw", sa.Column("region_resistance_max", sa.Numeric(18, 6), nullable=True))
    op.create_index(
        "ix_wage_events_raw_region_initial_country_id",
        "wage_events_raw",
        ["region_initial_country_id"],
        unique=False,
    )

    op.execute(
        """
        UPDATE wage_events_raw AS wer
        SET
          region_initial_country_id = rc.initial_country_id,
          region_resistance = NULLIF(rc.payload->>'resistance', '')::numeric,
          region_resistance_max = NULLIF(rc.payload->>'resistanceMax', '')::numeric
        FROM region_cache AS rc
        WHERE wer.operating_region_id = rc.id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_wage_events_raw_region_initial_country_id", table_name="wage_events_raw")
    op.drop_column("wage_events_raw", "region_resistance_max")
    op.drop_column("wage_events_raw", "region_resistance")
    op.drop_column("wage_events_raw", "region_initial_country_id")
