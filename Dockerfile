FROM python:3.13-slim

LABEL org.opencontainers.image.title="WarEra Monetary Watch" \
    org.opencontainers.image.description="WarEra tax income tracker with a live collector and web UI." \
    org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY tests ./tests

RUN pip install --no-cache-dir ".[dev]" \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "warera_monetary_watch.web"]
