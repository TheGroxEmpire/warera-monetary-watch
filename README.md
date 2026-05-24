# WarEra Monetary Watch

WarEra Monetary Watch collects wage transactions from the WarEra API, stores
them in PostgreSQL, and serves a FastAPI dashboard for country tax-income
analysis.

Live site: [https://warera.xorgress.com/monetary-watch](https://warera.xorgress.com/monetary-watch)

## Features

- Live wage-event collector with configurable API rate limits
- PostgreSQL storage for raw wage events and hourly rollups
- Global country leaderboard and per-country drilldowns
- Item, owner-country, core-region, and hourly time-window filters
- Optional authentik OIDC gate for private deployments

## Stack

- Python 3.12+
- FastAPI
- PostgreSQL
- SQLAlchemy + Alembic
- Docker Compose

## Quick Start

Copy the example environment and set your WarEra API token:

```bash
cp .env.example .env
$EDITOR .env
```

Start the app:

```bash
docker compose up -d --build
```

Open:

```text
http://localhost:8000/monetary-watch
```

The default Compose file is local-first and publishes the web service on
`WEB_PUBLISHED_PORT`, which defaults to `8000`.

## Configuration

Key settings live in `.env`:

- `WARERA_API_TOKEN`: required for the collector
- `WARERA_API_BASE_URL`: WarEra API endpoint
- `APP_BASE_PATH`: dashboard base path, default `/monetary-watch`
- `WEB_PORT`: port used inside the web container
- `WEB_PUBLISHED_PORT`: host port exposed by Docker Compose
- `RAW_RETENTION_DAYS`: retention window for raw wage events and hourly rollups

Do not commit `.env`, SQL dumps, database files, or files under `backups/`.

## Services

- `web`: FastAPI application
- `collector`: background wage poller and enricher
- `migrate`: Alembic schema migration job
- `postgres`: application database
- `test`: containerized test runner

## Checks

With Docker:

```bash
docker compose run --rm test
```

With a local Python environment:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
```

## Optional Traefik

For Traefik deployments, use the overlay file and set a real host in `.env`:

```bash
WARERA_HOST=warera.example.com \
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
```

The overlay expects an external Docker network named `proxy` by default. Change
it with `PROXY_NETWORK`.

## Optional Authentik

Set these values to enable the authentik OIDC gate:

```env
AUTHENTIK_AUTH_ENABLED=true
AUTHENTIK_BASE_URL=https://auth.example.com
AUTHENTIK_CLIENT_ID=...
AUTHENTIK_CLIENT_SECRET=...
AUTH_SESSION_SECRET_KEY=...
AUTHENTIK_ALLOWED_GROUP=warera-monetary-watch-access
WARERA_HOST=warera.example.com
```

Configure the authentik redirect URI as:

```text
https://<WARERA_HOST><APP_BASE_PATH>/auth/callback
```

## Operations

Apply the current code and database migrations with:

```bash
scripts/deploy.sh
```

The deploy script builds the image, runs tests, creates a PostgreSQL backup in
`backups/`, stops the app services, runs Alembic migrations, and restarts
`web` and `collector`.

Useful options:

```bash
scripts/deploy.sh --skip-tests
scripts/deploy.sh --skip-backup
scripts/deploy.sh --rebuild-rollups
scripts/deploy.sh --logs
```

For Traefik deploys:

```bash
ENABLE_TRAEFIK=1 scripts/deploy.sh
```

Check collector/backfill progress, data freshness, schema version, rollups, and
active database work with:

```bash
scripts/status.sh
scripts/status.sh --watch
scripts/status.sh --watch 10
```

Expand the collected historical window with:

```bash
scripts/expand-window.sh
```

## License

MIT. See [LICENSE](LICENSE).
