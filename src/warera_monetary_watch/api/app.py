from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from secrets import token_urlsafe
from time import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from warera_monetary_watch.config import Settings, get_settings
from warera_monetary_watch.db.session import create_session_dependency, create_sessionmaker
from warera_monetary_watch.services.analytics import (
    InvalidHourRange,
    get_country_by_code,
    get_country_dataset,
    get_country_item_breakdown,
    get_country_owner_breakdown,
    get_country_summary,
    get_country_timeseries,
    get_filters,
    get_global_dataset,
    get_overview,
    get_status,
)

BASE_DIR = Path(__file__).resolve().parents[1]


def resolve_asset_dir(name: str) -> Path:
    candidates = [
        BASE_DIR / name,
        Path.cwd() / "src" / "warera_monetary_watch" / name,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Asset directory '{name}' does not exist. Checked: {searched}")


STATIC_DIR = resolve_asset_dir("static")
TEMPLATES_DIR = resolve_asset_dir("templates")
AUTH_STATE_COOKIE = "warera_monetary_watch_auth_state"
AUTH_SESSION_SECONDS = 12 * 60 * 60
AUTH_STATE_SECONDS = 10 * 60
ASSET_VERSION = str(
    max(
        int((STATIC_DIR / "app.js").stat().st_mtime),
        int((STATIC_DIR / "styles.css").stat().st_mtime),
    )
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def encode_signed_payload(payload: dict[str, object], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded_body = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
    signature = hmac.new(secret.encode(), encoded_body.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded_body}.{encoded_signature}"


def decode_signed_payload(value: str | None, secret: str) -> dict[str, object] | None:
    if not value or "." not in value:
        return None
    encoded_body, encoded_signature = value.rsplit(".", 1)
    expected_signature = hmac.new(secret.encode(), encoded_body.encode(), hashlib.sha256).digest()
    try:
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
    except ValueError:
        return None
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        body = base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4))
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    expires_at = payload.get("exp")
    if not isinstance(expires_at, int | float) or expires_at < time():
        return None
    return payload if isinstance(payload, dict) else None


def safe_next_path(value: str | None, base_path: str) -> str:
    if not value or not value.startswith(base_path) or value.startswith("//"):
        return base_path
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    base_path = settings.normalized_base_path
    session_dependency = create_session_dependency(create_sessionmaker(settings.database_url))
    auth_enabled = settings.authentik_auth_enabled

    app = FastAPI(title="WarEra Monetary Watch", docs_url=None, redoc_url=None)
    if auth_enabled and (
        not settings.authentik_client_id
        or not settings.authentik_client_secret
        or not settings.auth_session_secret_key
    ):
        raise RuntimeError(
            "AUTHENTIK_CLIENT_ID, AUTHENTIK_CLIENT_SECRET, and AUTH_SESSION_SECRET_KEY "
            "must be set when AUTHENTIK_AUTH_ENABLED=true."
        )

    app.mount(
        f"{base_path}/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    def auth_cookie_path() -> str:
        return base_path if base_path != "/" else "/"

    def request_next_path(request: Request) -> str:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        return safe_next_path(path, base_path)

    def get_session_user(request: Request) -> dict[str, object] | None:
        return decode_signed_payload(
            request.cookies.get(settings.auth_session_cookie_name),
            settings.auth_session_secret_key,
        )

    def set_signed_cookie(
        response: Response,
        name: str,
        payload: dict[str, object],
        max_age: int,
    ) -> None:
        response.set_cookie(
            name,
            encode_signed_payload(payload, settings.auth_session_secret_key),
            max_age=max_age,
            httponly=True,
            secure=True,
            samesite="lax",
            path=auth_cookie_path(),
        )

    def clear_auth_cookies(response: Response) -> None:
        response.delete_cookie(settings.auth_session_cookie_name, path=auth_cookie_path())
        response.delete_cookie(AUTH_STATE_COOKIE, path=auth_cookie_path())

    if auth_enabled:

        @app.middleware("http")
        async def require_authentication(request: Request, call_next):  # type: ignore[no-untyped-def]
            path = request.url.path
            protected_root = path == base_path or path.startswith(f"{base_path}/")
            bypass_prefixes = (
                f"{base_path}/auth/",
                f"{base_path}/static/",
            )
            if not protected_root or any(path.startswith(prefix) for prefix in bypass_prefixes):
                return await call_next(request)
            user = get_session_user(request)
            if user is not None:
                request.state.auth_user = user
                return await call_next(request)
            next_path = quote(request_next_path(request), safe="")
            return RedirectResponse(f"{base_path}/auth/login?next={next_path}")

    auth_router = APIRouter(prefix=f"{base_path}/auth")

    @auth_router.get("/login", include_in_schema=False)
    async def login(request: Request, next: str | None = None) -> RedirectResponse:
        if not auth_enabled:
            return RedirectResponse(base_path)
        state = token_urlsafe(32)
        nonce = token_urlsafe(32)
        next_path = safe_next_path(next, base_path)
        redirect_uri = f"https://{settings.warera_host}{base_path}/auth/callback"
        authorize_url = f"{settings.authentik_base_url.rstrip('/')}/application/o/authorize/"
        params = {
            "client_id": settings.authentik_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        response = RedirectResponse(f"{authorize_url}?{urlencode(params)}")
        set_signed_cookie(
            response,
            AUTH_STATE_COOKIE,
            {
                "state": state,
                "nonce": nonce,
                "next": next_path,
                "exp": int(time()) + AUTH_STATE_SECONDS,
            },
            AUTH_STATE_SECONDS,
        )
        return response

    @auth_router.get("/callback", include_in_schema=False)
    async def auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        if not auth_enabled:
            return RedirectResponse(base_path)
        state_payload = decode_signed_payload(
            request.cookies.get(AUTH_STATE_COOKIE),
            settings.auth_session_secret_key,
        )
        if error:
            return PlainTextResponse(f"authentik login failed: {error}", status_code=401)
        if not code or not state_payload or state_payload.get("state") != state:
            return PlainTextResponse("Invalid authentik login state.", status_code=401)

        redirect_uri = f"https://{settings.warera_host}{base_path}/auth/callback"
        token_url = f"{settings.authentik_base_url.rstrip('/')}/application/o/token/"
        userinfo_url = f"{settings.authentik_base_url.rstrip('/')}/application/o/userinfo/"
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": settings.authentik_client_id,
                    "client_secret": settings.authentik_client_secret,
                },
            )
            if token_response.status_code >= 400:
                return PlainTextResponse("Failed to exchange authentik code.", status_code=401)
            access_token = token_response.json().get("access_token")
            if not access_token:
                return PlainTextResponse("authentik did not return an access token.", status_code=401)
            userinfo_response = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if userinfo_response.status_code >= 400:
                return PlainTextResponse("Failed to load authentik user info.", status_code=401)
            userinfo = userinfo_response.json()

        groups = userinfo.get("groups")
        if not isinstance(groups, list):
            groups = []
        if settings.authentik_allowed_group not in groups:
            response = PlainTextResponse("You do not have access to Warera Monetary Watch.", status_code=403)
            clear_auth_cookies(response)
            return response

        next_path = safe_next_path(str(state_payload.get("next", base_path)), base_path)
        response = RedirectResponse(next_path)
        set_signed_cookie(
            response,
            settings.auth_session_cookie_name,
            {
                "username": userinfo.get("preferred_username") or userinfo.get("sub") or "",
                "email": userinfo.get("email") or "",
                "name": userinfo.get("name") or "",
                "groups": groups,
                "exp": int(time()) + AUTH_SESSION_SECONDS,
            },
            AUTH_SESSION_SECONDS,
        )
        response.delete_cookie(AUTH_STATE_COOKIE, path=auth_cookie_path())
        return response

    @auth_router.get("/logout", include_in_schema=False)
    async def logout() -> RedirectResponse:
        response = RedirectResponse(base_path)
        if auth_enabled:
            clear_auth_cookies(response)
        return response

    app.include_router(auth_router)

    @app.exception_handler(InvalidHourRange)
    async def invalid_hour_range_handler(
        _request: Request,
        exc: InvalidHourRange,
    ) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    page_router = APIRouter(prefix=base_path)
    api_router = APIRouter(prefix=f"{base_path}/api/v1")

    def render_home(request: Request, country_code: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            "app.html",
            {
                "request": request,
                "base_path": base_path,
                "selected_country_code": country_code.lower() if country_code else None,
                "show_global_leaderboard": country_code is None,
                "warera_host": settings.warera_host,
                "raw_retention_days": settings.raw_retention_days,
                "asset_version": ASSET_VERSION,
            },
        )

    @page_router.get("", response_class=HTMLResponse, include_in_schema=False)
    @page_router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return render_home(request)

    @page_router.get("/countries/{country_code}", response_class=HTMLResponse)
    async def country_page(request: Request, country_code: str) -> HTMLResponse:
        return render_home(request, country_code=country_code)

    @api_router.get("/filters")
    async def filters(session: AsyncSession = Depends(session_dependency)) -> dict[str, object]:
        return await get_filters(session)

    @api_router.get("/status")
    async def status(session: AsyncSession = Depends(session_dependency)) -> dict[str, object]:
        return await get_status(session)

    @api_router.get("/overview")
    async def overview(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        item: str | None = None,
        owner_country: str | None = None,
        core: str = "all",
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        return await get_overview(
            session,
            from_,
            to,
            item_code=item,
            owner_country_id=owner_country,
            core_filter=core,
        )

    @api_router.get("/dataset")
    async def global_dataset(
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        return await get_global_dataset(
            session,
            from_value=from_,
            to_value=to,
        )

    @api_router.get("/countries/{country_code}/summary")
    async def country_summary(
        country_code: str,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        item: str | None = None,
        owner_country: str | None = None,
        core: str = "all",
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        country = await get_country_by_code(session, country_code)
        if country is None:
            raise HTTPException(status_code=404, detail="Country not found.")
        return await get_country_summary(
            session,
            country,
            from_value=from_,
            to_value=to,
            item_code=item,
            owner_country_id=owner_country,
            core_filter=core,
        )

    @api_router.get("/countries/{country_code}/timeseries")
    async def country_timeseries(
        country_code: str,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        item: str | None = None,
        owner_country: str | None = None,
        core: str = "all",
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        country = await get_country_by_code(session, country_code)
        if country is None:
            raise HTTPException(status_code=404, detail="Country not found.")
        return await get_country_timeseries(
            session,
            country,
            from_value=from_,
            to_value=to,
            item_code=item,
            owner_country_id=owner_country,
            core_filter=core,
        )

    @api_router.get("/countries/{country_code}/dataset")
    async def country_dataset(
        country_code: str,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        country = await get_country_by_code(session, country_code)
        if country is None:
            raise HTTPException(status_code=404, detail="Country not found.")
        return await get_country_dataset(
            session,
            country,
            from_value=from_,
            to_value=to,
        )

    @api_router.get("/countries/{country_code}/breakdown/items")
    async def country_item_breakdown(
        country_code: str,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        owner_country: str | None = None,
        core: str = "all",
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        country = await get_country_by_code(session, country_code)
        if country is None:
            raise HTTPException(status_code=404, detail="Country not found.")
        return await get_country_item_breakdown(
            session,
            country,
            from_value=from_,
            to_value=to,
            owner_country_id=owner_country,
            core_filter=core,
        )

    @api_router.get("/countries/{country_code}/breakdown/owners")
    async def country_owner_breakdown(
        country_code: str,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        item: str | None = None,
        core: str = "all",
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, object]:
        country = await get_country_by_code(session, country_code)
        if country is None:
            raise HTTPException(status_code=404, detail="Country not found.")
        return await get_country_owner_breakdown(
            session,
            country,
            from_value=from_,
            to_value=to,
            item_code=item,
            core_filter=core,
        )

    app.include_router(page_router)
    app.include_router(api_router)
    return app
