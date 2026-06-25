#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-warera-monetary-watch}"
PROXY_NETWORK="${PROXY_NETWORK:-proxy}"
ENABLE_TRAEFIK="${ENABLE_TRAEFIK:-0}"
TRAEFIK_COMPOSE_FILE="${TRAEFIK_COMPOSE_FILE:-docker-compose.traefik.yml}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-warera_monetary_watch}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"

RUN_TESTS=1
RUN_BACKUP=1
REBUILD_ROLLUPS=0
FOLLOW_LOGS=0

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh [options]

Builds the current app image, optionally tests it, backs up Postgres, applies
Alembic migrations, and restarts the web + collector services.

Options:
  --skip-tests        Do not run the docker-compose test service.
  --skip-backup       Do not create a pg_dump backup before migrating.
  --rebuild-rollups   Run the rollup rebuild maintenance command after migration.
  --logs              Follow web and collector logs after deploy.
  -h, --help          Show this help.

Environment overrides:
  COMPOSE_PROJECT     Compose project name. Default: warera-monetary-watch
  ENABLE_TRAEFIK      Include docker-compose.traefik.yml when set to 1. Default: 0
  TRAEFIK_COMPOSE_FILE Optional Traefik compose overlay. Default: docker-compose.traefik.yml
  PROXY_NETWORK       External Traefik network name when ENABLE_TRAEFIK=1. Default: proxy
  POSTGRES_USER       Postgres user for backups. Default: postgres
  POSTGRES_DB         Postgres database for backups. Default: warera_monetary_watch
  BACKUP_DIR          Local backup directory. Default: ./backups
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests)
      RUN_TESTS=0
      ;;
    --skip-backup)
      RUN_BACKUP=0
      ;;
    --rebuild-rollups)
      REBUILD_ROLLUPS=1
      ;;
    --logs)
      FOLLOW_LOGS=1
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
  echo "Missing .env. Create it from .env.example and set WARERA_API_TOKEN before deploying." >&2
  exit 1
fi

COMPOSE_FILES=(-f docker-compose.yml)
if [[ "$ENABLE_TRAEFIK" == "1" ]]; then
  COMPOSE_FILES+=(-f "$TRAEFIK_COMPOSE_FILE")
fi
COMPOSE_ARGS=("${COMPOSE_FILES[@]}" -p "$COMPOSE_PROJECT")

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "Docker is required, but this user cannot access the Docker daemon." >&2
  exit 1
fi

if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  COMPOSE=("${DOCKER[@]}" compose "${COMPOSE_ARGS[@]}")
elif command -v docker-compose >/dev/null 2>&1; then
  if [[ "${DOCKER[*]}" == "docker" ]]; then
    COMPOSE=(docker-compose "${COMPOSE_ARGS[@]}")
  else
    COMPOSE=(sudo -n docker-compose "${COMPOSE_ARGS[@]}")
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

echo "Deploying WarEra Monetary Watch from $ROOT_DIR"

if [[ "$ENABLE_TRAEFIK" == "1" ]] && ! "${DOCKER[@]}" network inspect "$PROXY_NETWORK" >/dev/null 2>&1; then
  run "${DOCKER[@]}" network create "$PROXY_NETWORK"
fi

run "${COMPOSE[@]}" up -d "$POSTGRES_SERVICE"
wait_for_postgres

if [[ "$RUN_TESTS" -eq 1 ]]; then
  run "${COMPOSE[@]}" build
  run "${COMPOSE[@]}" run --rm test
else
  run "${COMPOSE[@]}" build
fi

if [[ "$RUN_BACKUP" -eq 1 ]]; then
  mkdir -p "$BACKUP_DIR"
  backup_file="$BACKUP_DIR/${POSTGRES_DB}_$(date -u +%Y%m%dT%H%M%SZ).sql"
  echo
  echo "+ pg_dump > $backup_file"
  "${COMPOSE[@]}" exec -T "$POSTGRES_SERVICE" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$backup_file"
fi

run "${COMPOSE[@]}" run --rm migrate

if [[ "$REBUILD_ROLLUPS" -eq 1 ]]; then
  run "${COMPOSE[@]}" run --rm web python -m warera_monetary_watch.maintenance rebuild-rollups
fi

run "${COMPOSE[@]}" up -d --no-deps --force-recreate web collector
run "${COMPOSE[@]}" ps

echo
echo "Deploy complete."

if [[ "$FOLLOW_LOGS" -eq 1 ]]; then
  run "${COMPOSE[@]}" logs -f --tail=100 web collector
fi
