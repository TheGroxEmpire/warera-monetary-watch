from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

STATE_KEYS = [
    "collector.wage_cursor",
    "collector.last_success_at",
    "collector.last_error",
    "collector.last_stats",
    "collector.last_progress_at",
    "collector.countries_synced_at",
    "collector.regions_synced_at",
    "collector.retention_applied_at",
    "maintenance.backfill_wages",
]


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_duration(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    seconds = max(0, int(value.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def age(value: datetime | None, *, now: datetime) -> str:
    if value is None:
        return "never"
    return f"{format_duration(now - value)} ago"


def percent(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{max(0.0, min(100.0, value * 100)):.1f}%"


def estimate_eta(started_at: datetime | None, progress: float | None, *, now: datetime) -> str:
    if started_at is None or progress is None:
        return "unknown"
    progress = max(0.0, min(1.0, progress))
    if progress >= 1:
        return "done"
    if progress <= 0:
        return "unknown"
    elapsed = now - started_at
    remaining = elapsed * ((1 - progress) / progress)
    return format_duration(remaining)


def backfill_progress_ratio(stats: Mapping[str, Any]) -> float | None:
    if not stats.get("in_progress") and stats.get("stage") == "complete":
        return 1.0
    start = parse_datetime(stats.get("from"))
    end = parse_datetime(stats.get("to"))
    oldest_seen = parse_datetime(stats.get("oldest_seen_at"))
    if start is None or end is None or oldest_seen is None or start >= end:
        return None
    if stats.get("reached_start"):
        return 1.0
    covered_to = max(start, min(end, oldest_seen))
    return (end - covered_to).total_seconds() / (end - start).total_seconds()


def page_progress_ratio(stats: Mapping[str, Any]) -> float | None:
    pages = stats.get("pages_scanned")
    max_pages = stats.get("max_pages")
    if not isinstance(pages, int) or not isinstance(max_pages, int) or max_pages <= 0:
        return None
    if stats.get("reached_cursor"):
        return 1.0
    return pages / max_pages


def normalize_database_url(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def decode_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


async def table_exists(conn: asyncpg.Connection, table_name: str) -> bool:
    return await conn.fetchval("select to_regclass($1) is not null", f"public.{table_name}")


async def fetch_states(conn: asyncpg.Connection) -> dict[str, dict[str, Any]]:
    if not await table_exists(conn, "collector_state"):
        return {}
    rows = await conn.fetch(
        """
        select key, string_value, json_value, updated_at
        from collector_state
        where key = any($1::text[])
        """,
        STATE_KEYS,
    )
    return {
        row["key"]: {
            "string_value": row["string_value"],
            "json_value": decode_json(row["json_value"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


async def fetch_wage_summary(conn: asyncpg.Connection) -> asyncpg.Record | None:
    if not await table_exists(conn, "wage_events_raw"):
        return None
    return await conn.fetchrow(
        """
        select
            count(*) as total,
            count(*) filter (where resolved) as resolved,
            count(*) filter (where not resolved) as unresolved,
            min(event_hour) as oldest_event_at,
            max(created_at) as newest_event_at,
            max(ingested_at) as newest_ingested_at,
            count(*) filter (where ingested_at >= now() - interval '5 minutes') as ingested_5m,
            count(*) filter (where ingested_at >= now() - interval '1 hour') as ingested_1h
        from wage_events_raw
        """
    )


async def fetch_rollup_summary(conn: asyncpg.Connection) -> asyncpg.Record | None:
    if not await table_exists(conn, "hourly_tax_rollups"):
        return None
    return await conn.fetchrow(
        """
        select
            count(*) as total,
            min(hour_start) as oldest_hour,
            max(hour_start) as newest_hour,
            max(updated_at) as newest_updated_at
        from hourly_tax_rollups
        """
    )


async def fetch_schema_version(conn: asyncpg.Connection) -> str:
    if not await table_exists(conn, "alembic_version"):
        return "not initialized"
    return await conn.fetchval("select version_num from alembic_version") or "unknown"


async def fetch_active_queries(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        select
            pid,
            state,
            wait_event_type,
            wait_event,
            now() - query_start as duration,
            left(regexp_replace(query, '\\s+', ' ', 'g'), 110) as query
        from pg_stat_activity
        where datname = current_database()
          and pid <> pg_backend_pid()
          and state <> 'idle'
        order by query_start nulls last
        limit 8
        """
    )


def print_collector(states: Mapping[str, dict[str, Any]], *, now: datetime) -> None:
    stats = (states.get("collector.last_stats") or {}).get("json_value") or {}
    last_success = parse_datetime((states.get("collector.last_success_at") or {}).get("string_value"))
    last_progress = parse_datetime((states.get("collector.last_progress_at") or {}).get("string_value"))
    last_error = (states.get("collector.last_error") or {}).get("string_value")
    cursor = (states.get("collector.wage_cursor") or {}).get("json_value") or {}
    progress = page_progress_ratio(stats)
    eta = estimate_eta(parse_datetime(stats.get("started_at")), progress, now=now)

    print("Collector")
    print(f"  Last success: {age(last_success, now=now)}")
    print(f"  Last progress: {age(last_progress, now=now)}")
    print(f"  Cursor: {cursor.get('transaction_id', 'none')} at {cursor.get('created_at', 'unknown')}")
    if stats:
        status = "in progress" if stats.get("in_progress") else "idle/complete"
        print(f"  Status: {status}; stage={stats.get('stage', 'cycle_complete')}")
        print(
            "  Current cycle: "
            f"{stats.get('new_transactions', 0)} new, "
            f"{stats.get('resolved_transactions', 0)} resolved, "
            f"{stats.get('unresolved_transactions', 0)} unresolved, "
            f"{stats.get('affected_hours', 0)} hours"
        )
        if stats.get("in_progress"):
            print(f"  Page progress: {percent(progress)}; ETA to page cap/cursor: {eta}")
    if last_error:
        print(f"  Last error: {last_error}")
    print()


def print_backfill(states: Mapping[str, dict[str, Any]], *, now: datetime) -> None:
    stats = (states.get("maintenance.backfill_wages") or {}).get("json_value") or {}
    print("Backfill")
    if not stats:
        print("  No backfill progress has been recorded yet.")
        print()
        return
    progress = backfill_progress_ratio(stats)
    eta = estimate_eta(parse_datetime(stats.get("started_at")), progress, now=now)
    status = "in progress" if stats.get("in_progress") else stats.get("stage", "complete")
    print(f"  Status: {status}; stage={stats.get('stage', 'unknown')}")
    print(f"  Window: {stats.get('from', 'unknown')} to {stats.get('to', 'unknown')}")
    print(
        "  Progress: "
        f"{percent(progress)}; ETA {eta}; "
        f"pages {stats.get('pages_scanned', 0)}/{stats.get('max_pages', '?')}"
    )
    print(
        "  Transactions: "
        f"{stats.get('matched_transactions', 0)} matched, "
        f"{stats.get('resolved_transactions', 0)} resolved, "
        f"{stats.get('unresolved_transactions', 0)} unresolved"
    )
    print(f"  Last update: {age(parse_datetime(stats.get('updated_at')), now=now)}")
    print()


def print_database(summary: asyncpg.Record | None, rollups: asyncpg.Record | None, *, now: datetime) -> None:
    print("Data")
    if summary is None:
        print("  wage_events_raw is not available yet.")
    else:
        total = summary["total"] or 0
        print(
            f"  Wage events: {total} total, "
            f"{summary['resolved'] or 0} resolved, {summary['unresolved'] or 0} unresolved"
        )
        print(
            f"  Event window: {summary['oldest_event_at'] or 'none'} to "
            f"{summary['newest_event_at'] or 'none'}"
        )
        print(
            f"  New data: {summary['ingested_5m'] or 0} in 5m, "
            f"{summary['ingested_1h'] or 0} in 1h; latest ingest {age(summary['newest_ingested_at'], now=now)}"
        )
    if rollups is None:
        print("  hourly_tax_rollups is not available yet.")
    else:
        print(
            f"  Rollups: {rollups['total'] or 0} rows, "
            f"latest hour {rollups['newest_hour'] or 'none'}, "
            f"updated {age(rollups['newest_updated_at'], now=now)}"
        )
    print()


def print_active_queries(rows: list[asyncpg.Record]) -> None:
    print("Active Database Work")
    if not rows:
        print("  No active non-idle database queries.")
        print()
        return
    for row in rows:
        wait = f", wait={row['wait_event_type']}/{row['wait_event']}" if row["wait_event"] else ""
        print(f"  pid {row['pid']}: {format_duration(row['duration'])} {row['state']}{wait}")
        print(f"    {row['query']}")
    print()


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Show WarEra Monetary Watch processing status.")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is required.")

    now = datetime.now(tz=UTC)
    conn = await asyncpg.connect(normalize_database_url(args.database_url))
    try:
        states = await fetch_states(conn)
        schema_version = await fetch_schema_version(conn)
        wage_summary = await fetch_wage_summary(conn)
        rollup_summary = await fetch_rollup_summary(conn)
        active_queries = await fetch_active_queries(conn)
    finally:
        await conn.close()

    print(f"WarEra Monetary Watch Status ({now.isoformat(timespec='seconds')})")
    print(f"Schema: {schema_version}")
    print()
    print_collector(states, now=now)
    print_backfill(states, now=now)
    print_database(wage_summary, rollup_summary, now=now)
    print_active_queries(active_queries)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
