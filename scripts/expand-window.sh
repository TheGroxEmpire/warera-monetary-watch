#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-warera-monetary-watch}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-warera_monetary_watch}"

FROM_VALUE="2026-04-20T00:00:00Z"
TO_VALUE=""
MAX_PAGES=12000
BATCH_SIZE=2000
RUN_BUILD=1
STOP_COLLECTOR=1

usage() {
  cat <<'USAGE'
Usage: scripts/expand-window.sh [options]

Backfills wage events before the current database minimum so the stored event
window starts earlier. By default it expands to 2026-04-20 00:00 UTC.

Options:
  --from VALUE        Desired start timestamp. Default: 2026-04-20T00:00:00Z
  --to VALUE          Backfill end timestamp. Default: current min(created_at)
  --max-pages N       Maximum API pages to scan. Default: 12000
  --batch-size N      Events per ingest batch. Default: 2000
  --skip-build        Do not rebuild the app image before running maintenance.
  --keep-collector    Leave the live collector running during the backfill.
  -h, --help          Show this help.

Environment overrides:
  COMPOSE_PROJECT     Compose project name. Default: warera-monetary-watch
  POSTGRES_USER       Postgres user. Default: postgres
  POSTGRES_DB         Postgres database. Default: warera_monetary_watch
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      FROM_VALUE="${2:?--from requires a value}"
      shift
      ;;
    --to)
      TO_VALUE="${2:?--to requires a value}"
      shift
      ;;
    --max-pages)
      MAX_PAGES="${2:?--max-pages requires a value}"
      shift
      ;;
    --batch-size)
      BATCH_SIZE="${2:?--batch-size requires a value}"
      shift
      ;;
    --skip-build)
      RUN_BUILD=0
      ;;
    --keep-collector)
      STOP_COLLECTOR=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! -f .env ]]; then
  echo "Missing .env. Create it from .env.example and set WARERA_API_TOKEN before running backfill." >&2
  exit 1
fi

if ! [[ "$MAX_PAGES" =~ ^[0-9]+$ && "$MAX_PAGES" -gt 0 ]]; then
  echo "--max-pages must be a positive integer." >&2
  exit 2
fi

if ! [[ "$BATCH_SIZE" =~ ^[0-9]+$ && "$BATCH_SIZE" -gt 0 ]]; then
  echo "--batch-size must be a positive integer." >&2
  exit 2
fi

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "Docker is required, but this user cannot access the Docker daemon." >&2
  exit 1
fi

if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose -p "$COMPOSE_PROJECT")
elif command -v docker-compose >/dev/null 2>&1; then
  if [[ "${DOCKER[*]}" == "docker" ]]; then
    COMPOSE=(docker-compose -p "$COMPOSE_PROJECT")
  else
    COMPOSE=(sudo -n docker-compose -p "$COMPOSE_PROJECT")
  fi
else
  echo "Docker Compose is required, but neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

run() {
  echo
  echo "+ $*"
  "$@"
}

psql_scalar() {
  "${COMPOSE[@]}" exec -T "$POSTGRES_SERVICE" \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"
}

psql_table() {
  "${COMPOSE[@]}" exec -T "$POSTGRES_SERVICE" \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

wait_for_postgres() {
  echo
  echo "Waiting for Postgres to accept connections..."
  for _ in {1..60}; do
    if "${COMPOSE[@]}" exec -T "$POSTGRES_SERVICE" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      echo "Postgres is ready."
      return 0
    fi
    sleep 2
  done

  echo "Postgres did not become ready in time." >&2
  return 1
}

echo "Expanding WarEra wage event window from $ROOT_DIR"

run "${COMPOSE[@]}" up -d "$POSTGRES_SERVICE"
wait_for_postgres

if [[ "$RUN_BUILD" -eq 1 ]]; then
  run "${COMPOSE[@]}" build web
fi

if [[ -z "$TO_VALUE" ]]; then
  TO_VALUE="$(psql_scalar "select min(created_at) from wage_events_raw;")"
fi

if [[ -z "$TO_VALUE" ]]; then
  echo "No wage events exist yet, so there is no current window to expand." >&2
  exit 1
fi

echo
echo "Current data window:"
psql_table "select min(created_at) as oldest, max(created_at) as newest, count(*) as total from wage_events_raw;"

echo
echo "Backfill target: $FROM_VALUE to $TO_VALUE"

if [[ "$STOP_COLLECTOR" -eq 1 ]]; then
  run "${COMPOSE[@]}" stop collector
fi

cleanup() {
  if [[ "$STOP_COLLECTOR" -eq 1 ]]; then
    "${COMPOSE[@]}" up -d --no-deps collector >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run "${COMPOSE[@]}" run --rm --no-deps web \
  python -m warera_monetary_watch.maintenance backfill-wages \
  --from "$FROM_VALUE" \
  --to "$TO_VALUE" \
  --max-pages "$MAX_PAGES" \
  --batch-size "$BATCH_SIZE"

echo
echo "Rebuilding rollups after window expansion..."
run "${COMPOSE[@]}" run --rm --no-deps web \
  python -m warera_monetary_watch.maintenance rebuild-rollups

echo
echo "Updated data window:"
psql_table "select min(created_at) as oldest, max(created_at) as newest, count(*) as total from wage_events_raw;"

echo
echo "Unresolved breakdown:"
psql_table "select unresolved_reason, count(*) from wage_events_raw where not resolved group by unresolved_reason order by count(*) desc;"

echo
echo "Window expansion complete."
