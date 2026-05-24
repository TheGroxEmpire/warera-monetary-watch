from datetime import UTC, datetime, timedelta

from warera_monetary_watch.status import (
    backfill_progress_ratio,
    estimate_eta,
    format_duration,
    normalize_database_url,
    page_progress_ratio,
)


def test_format_duration_compacts_values() -> None:
    assert format_duration(timedelta(seconds=4)) == "4s"
    assert format_duration(timedelta(seconds=65)) == "1m 5s"
    assert format_duration(timedelta(hours=2, minutes=3)) == "2h 3m"


def test_estimate_eta_uses_elapsed_time_and_progress() -> None:
    now = datetime(2026, 4, 26, 12, 10, tzinfo=UTC)
    started = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    assert estimate_eta(started, 0.5, now=now) == "10m 0s"


def test_page_progress_ratio_uses_cursor_completion() -> None:
    assert page_progress_ratio({"pages_scanned": 5, "max_pages": 20}) == 0.25
    assert page_progress_ratio({"pages_scanned": 5, "max_pages": 20, "reached_cursor": True}) == 1.0


def test_backfill_progress_ratio_tracks_oldest_seen_through_window() -> None:
    stats = {
        "from": "2026-04-26T10:00:00+00:00",
        "to": "2026-04-26T12:00:00+00:00",
        "oldest_seen_at": "2026-04-26T11:00:00+00:00",
    }

    assert backfill_progress_ratio(stats) == 0.5


def test_normalize_database_url_removes_sqlalchemy_asyncpg_driver() -> None:
    assert (
        normalize_database_url("postgresql+asyncpg://postgres:postgres@postgres/db")
        == "postgresql://postgres:postgres@postgres/db"
    )
