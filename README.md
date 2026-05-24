# WarEra Monetary Watch

Dockerized WarEra tax-income watcher with:

- a live wage transaction collector
- PostgreSQL storage for raw wage events and hourly rollups
- a FastAPI web UI for global country comparisons and per-country drilldown
- Traefik routing on `https://warera.xorgress.com/monetary-watch`

## Stack

- Python 3.13
- FastAPI
- PostgreSQL
- SQLAlchemy + Alembic
- Docker Compose

## Run

1. Copy `.env.example` to `.env`
2. Set `WARERA_API_TOKEN`
3. Start:

```bash
docker compose up -d --build
```

The app is routed through Traefik at:

```text
https://warera.xorgress.com/monetary-watch
```

Override the host with:

```bash
WARERA_HOST=warera.yourdomain.com docker compose up -d --build
```

## Services

- `web`: FastAPI application
- `collector`: background wage poller and enricher
- `migrate`: Alembic schema migration job
- `postgres`: application database

## Checks

```bash
docker compose run --rm test
```

## Authentik Access

Monetary Watch uses its own authentik OIDC application named
`Warera Monetary Watch`, not the shared Warera Intel proxy provider. Users must
belong to `warera-monetary-watch-access` to open `/monetary-watch`.

To add dashboard-managed users:

1. Open `https://authentik.xorgress.com/if/admin/`.
2. Go to `Directory > Users`, choose the user folder, and click `Create`.
3. Set a unique username, optional display name/email, and leave the user active.
4. Open the created user, use `Reset password`, and set the password.
5. Open the user's `Groups` tab and add them to `warera-monetary-watch-access`.

To remove access, remove the user from `warera-monetary-watch-access` or
deactivate the user.

## Retention

`RAW_RETENTION_DAYS` controls how long wage events and hourly tax rollups are
kept. The collector applies this retention pass periodically; the default is
90 days.

## Deploy

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

## Processing Status

Check collector/backfill progress, data freshness, schema version, rollups, and
active database work with:

```bash
scripts/status.sh
```

Continuously refresh it while a deploy, migration, or backfill is running:

```bash
scripts/status.sh --watch
scripts/status.sh --watch 10
```
