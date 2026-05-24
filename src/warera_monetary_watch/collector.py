from __future__ import annotations

import asyncio
import logging

from warera_monetary_watch.config import get_settings
from warera_monetary_watch.db.session import SessionLocal
from warera_monetary_watch.services.collector import WageCollectorService


async def amain() -> None:
    settings = get_settings()
    service = WageCollectorService(session_factory=SessionLocal, settings=settings)
    await service.run_forever()


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
