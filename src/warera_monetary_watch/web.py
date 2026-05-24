from __future__ import annotations

import logging

import uvicorn

from warera_monetary_watch.api.app import create_app
from warera_monetary_watch.config import get_settings

settings = get_settings()
app = create_app(settings)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "warera_monetary_watch.web:app",
        host=settings.web_host,
        port=settings.web_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()

