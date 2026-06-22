from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

FOREIGN_CITIZEN_TAX_SHARE = Decimal("0.3")
FOREIGN_CITIZEN_TAX_EFFECTIVE_AT = datetime(2026, 6, 13, 16, 45, tzinfo=UTC)
FOREIGN_CITIZEN_TAX_EFFECTIVE_HOUR = datetime(2026, 6, 13, 16, 0, tzinfo=UTC)


def calculate_citizenship_tax_share(
    *,
    work_country_id: str | None,
    worker_country_id: str | None,
) -> Decimal:
    if not work_country_id or not worker_country_id:
        return Decimal("0")
    if work_country_id == worker_country_id:
        return Decimal("0")
    return FOREIGN_CITIZEN_TAX_SHARE


def split_tax_for_citizenship(
    *,
    total_tax: Decimal,
    total_wages: Decimal,
    work_country_id: str | None,
    worker_country_id: str | None,
) -> dict[str, Decimal]:
    citizenship_ratio = calculate_citizenship_tax_share(
        work_country_id=work_country_id,
        worker_country_id=worker_country_id,
    )
    work_ratio = Decimal("1") - citizenship_ratio
    return {
        "citizenship_country_tax": total_tax * citizenship_ratio,
        "work_country_tax": total_tax * work_ratio,
        "citizenship_country_wages": total_wages * citizenship_ratio,
        "work_country_wages": total_wages * work_ratio,
    }
