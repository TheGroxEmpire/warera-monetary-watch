import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from warera_monetary_watch.api.app import STATIC_DIR, TEMPLATES_DIR, create_app, resolve_asset_dir
from warera_monetary_watch.config import Settings
from warera_monetary_watch.db.base import Base


def test_asset_directories_exist() -> None:
    assert STATIC_DIR.is_dir()
    assert TEMPLATES_DIR.is_dir()
    assert resolve_asset_dir("static") == STATIC_DIR
    assert resolve_asset_dir("templates") == TEMPLATES_DIR


def test_country_page_bootstrap_embeds_valid_selected_country_code() -> None:
    app = create_app(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            AUTHENTIK_AUTH_ENABLED=False,
        )
    )
    client = TestClient(app)

    response = client.get("/monetary-watch/countries/eg")

    assert response.status_code == 200
    assert 'selectedCountryCode: "eg"' in response.text
    assert "showGlobalLeaderboard: false" in response.text
    assert '<section class="panel" id="overview-panel" hidden>' in response.text
    assert "Global Tax Leaderboard" in response.text
    assert "&#39;eg&#39;" not in response.text


def test_home_page_shows_global_leaderboard() -> None:
    app = create_app(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            AUTHENTIK_AUTH_ENABLED=False,
        )
    )
    client = TestClient(app)

    response = client.get("/monetary-watch/")

    assert response.status_code == 200
    assert "showGlobalLeaderboard: true" in response.text
    assert "Global Tax Leaderboard" in response.text
    assert ">Host<" not in response.text
    assert "Refresh Data" not in response.text
    assert "Open Country View" not in response.text


def test_auth_enabled_redirects_home_to_authentik_login() -> None:
    app = create_app(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            AUTHENTIK_AUTH_ENABLED=True,
            AUTHENTIK_CLIENT_ID="client-id",
            AUTHENTIK_CLIENT_SECRET="client-secret",
            AUTH_SESSION_SECRET_KEY="session-secret",
        )
    )
    client = TestClient(app)

    response = client.get("/monetary-watch", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("/monetary-watch/auth/login?")


def test_auth_login_uses_warera_monetary_watch_client() -> None:
    app = create_app(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            WARERA_HOST="warera.example.com",
            AUTHENTIK_AUTH_ENABLED=True,
            AUTHENTIK_BASE_URL="https://auth.example.com",
            AUTHENTIK_CLIENT_ID="warera-monetary-watch-client",
            AUTHENTIK_CLIENT_SECRET="client-secret",
            AUTH_SESSION_SECRET_KEY="session-secret",
        )
    )
    client = TestClient(app)

    response = client.get(
        "/monetary-watch/auth/login?next=/monetary-watch",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert "client_id=warera-monetary-watch-client" in response.headers["location"]
    assert "redirect_uri=https%3A%2F%2Fwarera.example.com%2Fmonetary-watch%2Fauth%2Fcallback" in response.headers["location"]
    assert "warera_monetary_watch_auth_state=" in response.headers["set-cookie"]


def test_frontend_fetches_aggregate_tax_endpoints() -> None:
    app_js = (STATIC_DIR / "app.js").read_text()

    assert 'fetchJson("/api/v1/overview", selectedAggregateParams())' in app_js
    assert "fetchJson(`/api/v1/countries/${countryCode}/dataset`, params)" in app_js
    assert '<option value="${escapeHtml(country.id)}">${escapeHtml(country.name)}</option>' in app_js
    assert 'fetchJson("/api/v1/status")' in app_js
    assert "applyDataBounds(filters, status)" in app_js
    assert "startOfWeekMondayUtc(maxTo)" in app_js
    assert "dataWindow.maxTo ?? ceilHourUtc(new Date())" in app_js
    assert "const maxTo = toDate ? ceilHourUtc(toDate) : null;" in app_js
    assert "addHours(maxTo, -24)" not in app_js
    assert '"/api/v1/dataset"' not in app_js
    assert "fetchJson(`/api/v1/countries/${currentCountryCode}/summary`" not in app_js
    assert "fetchJson(`/api/v1/countries/${currentCountryCode}/timeseries`" not in app_js


def test_reversed_dataset_range_returns_bad_request() -> None:
    app = create_app(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            AUTHENTIK_AUTH_ENABLED=False,
        )
    )
    client = TestClient(app)

    response = client.get(
        "/monetary-watch/api/v1/dataset",
        params={"from": "2026-04-23T17:00", "to": "2026-04-22T17:00"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "`from` must be earlier than or equal to `to`."}


@pytest.mark.asyncio
async def test_api_uses_create_app_database_settings(tmp_path) -> None:
    database_path = tmp_path / "app.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    engine = create_async_engine(database_url, future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()

    app = create_app(
        Settings(
            DATABASE_URL=database_url,
            WARERA_API_BASE_URL="https://example.com/trpc",
            WARERA_API_TOKEN="test-token",
            AUTHENTIK_AUTH_ENABLED=False,
        )
    )
    client = TestClient(app)

    response = client.get("/monetary-watch/api/v1/status")

    assert response.status_code == 200
    assert response.json()["raw_events"] == 0
