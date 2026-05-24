#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-warera-monetary-watch}"
STATUS_SOURCE="$ROOT_DIR/src/warera_monetary_watch/status.py"
WATCH_INTERVAL=0

usage() {
  cat <<'USAGE'
Usage: scripts/status.sh [options]

Shows current processing status: collector progress, backfill progress, schema
version, data freshness, rollups, and active database work.

Options:
  -w, --watch [SECONDS]  Refresh continuously. Default interval: 5 seconds.
  -h, --help             Show this help.

Environment overrides:
  COMPOSE_PROJECT        Compose project name. Default: warera-monetary-watch
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -w|--watch)
      if [[ $# -gt 1 && ! "$2" =~ ^- ]]; then
        WATCH_INTERVAL="$2"
        shift
      else
        WATCH_INTERVAL=5
      fi
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

if [[ ! -f "$STATUS_SOURCE" ]]; then
  echo "Missing status reporter: $STATUS_SOURCE" >&2
  exit 1
fi

if ! [[ "$WATCH_INTERVAL" =~ ^[0-9]+$ ]]; then
  echo "--watch interval must be a positive integer number of seconds." >&2
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

run_once() {
  echo "Services"
  "${COMPOSE[@]}" ps
  echo

  if "${COMPOSE[@]}" ps --services --status running | grep -qx "web"; then
    "${COMPOSE[@]}" exec -T web python - "$@" < "$STATUS_SOURCE"
  else
    "${COMPOSE[@]}" run --rm --no-deps web python - "$@" < "$STATUS_SOURCE"
  fi
}

if [[ "$WATCH_INTERVAL" -gt 0 ]]; then
  while true; do
    if [[ -t 1 && -n "${TERM:-}" ]]; then
      clear || true
    fi
    run_once
    sleep "$WATCH_INTERVAL"
  done
else
  run_once
fi
