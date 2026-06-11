import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.api.routes import router
from src.config import get_settings
from src.logging_config import setup_logging
from src.services import central_metrics
from src.services import docker_images_cache
from src.services import jump_access
from src.services import local_dns
from src.services import state_snapshot

setup_logging()


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_session_secret() -> str:
    configured_secret = str(get_settings().get("auth", {}).get("session_secret") or "").strip()
    if configured_secret:
        return configured_secret

    if _env_flag("CENTRAL_ALLOW_INSECURE_SESSION_SECRET", False):
        logging.getLogger(__name__).warning(
            "SECURITY: auth.session_secret is missing; using insecure fallback because CENTRAL_ALLOW_INSECURE_SESSION_SECRET is enabled"
        )
        return "vlm-dev-session-secret-change-me"

    raise RuntimeError(
        "Configuration invalide: auth.session_secret est obligatoire. "
        "Definissez-le dans central/config.yaml (ou activez temporairement CENTRAL_ALLOW_INSECURE_SESSION_SECRET=1)."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    central_metrics.start_sampler(interval_seconds=60)
    state_snapshot.start_reconciler()
    docker_images_poll_interval = int(os.getenv("CENTRAL_DOCKER_IMAGES_POLL_INTERVAL", "300"))
    docker_images_cache.start_poller(interval_seconds=docker_images_poll_interval)
    jump_interval_seconds = int(os.getenv("CENTRAL_JUMP_FILTER_INTERVAL", "20"))
    jump_access.start_reconciler(interval_seconds=jump_interval_seconds)
    dns_interval_seconds = int(os.getenv("CENTRAL_LOCAL_DNS_INTERVAL", "20"))
    local_dns.start_reconciler(interval_seconds=dns_interval_seconds)
    yield
    # --- shutdown ---
    central_metrics.stop_sampler()
    state_snapshot.stop_reconciler()
    docker_images_cache.stop_poller()
    jump_access.stop_reconciler()
    local_dns.stop_reconciler()


app = FastAPI(title="Clab Master", version="0.1.0", lifespan=lifespan)
session_secret = _resolve_session_secret()
central_settings = get_settings().get("central", {})
tls_enabled = bool(os.getenv("CENTRAL_TLS_CERT", "").strip() and os.getenv("CENTRAL_TLS_KEY", "").strip())
secure_session_cookie = tls_enabled or _env_flag("CENTRAL_FORCE_HTTPS", bool(central_settings.get("force_https", False)))
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    same_site="lax",
    https_only=secure_session_cookie,
    session_cookie="vlm_session",
)


def _remove_sensitive_query_params(url_string: str) -> str:
    sensitive_keys = {"username", "password", "pass", "pwd"}
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    split = urlsplit(url_string)
    query_items = parse_qsl(split.query, keep_blank_values=True)
    filtered_items = [(key, value) for key, value in query_items if key.lower() not in sensitive_keys]
    if len(filtered_items) == len(query_items):
        return url_string
    new_query = urlencode(filtered_items, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))


class _SecurityRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        force_https = _env_flag("CENTRAL_FORCE_HTTPS", bool(central_settings.get("force_https", False)))
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()

        url = str(request.url)
        sanitized_url = _remove_sensitive_query_params(url)
        if sanitized_url != url:
            return RedirectResponse(url=sanitized_url, status_code=303)

        if force_https and proto == "http":
            https_url = str(request.url.replace(scheme="https"))
            return RedirectResponse(url=https_url, status_code=308)

        return await call_next(request)


class _NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

app.add_middleware(_NoCacheStaticMiddleware)
app.add_middleware(_SecurityRedirectMiddleware)
app.include_router(router)

app.mount("/static", StaticFiles(directory="src/static"), name="static")
templates = Jinja2Templates(directory="src/templates")

_app_logger = logging.getLogger(__name__)


def _get_app_js_version() -> int:
    app_js_path = os.path.join(os.path.dirname(__file__), "static", "js", "app.js")
    try:
        return int(os.path.getmtime(app_js_path))
    except OSError:
        return 0


def _render_home(
    request: Request,
    *,
    login_error: str = "",
    login_username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    refresh_seconds = get_settings().get("central", {}).get("refresh_seconds", 3)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "refresh_seconds": refresh_seconds,
            "auth_enabled": True,
            "app_js_version": _get_app_js_version(),
            "login_error": login_error,
            "login_username": login_username,
            "login_next": str(request.query_params.get("next") or ""),
        },
        status_code=status_code,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    _app_logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"ok": False, "detail": "Erreur interne du serveur."})


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render_home(request)


@app.post("/", response_class=HTMLResponse)
def home_login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next_path: str = Form(default=""),
):
    from src.services.auth import authenticate_user

    normalized_username = username.strip()
    user = authenticate_user(normalized_username, password)
    if user is None:
        return _render_home(
            request,
            login_error="Identifiants invalides",
            login_username=normalized_username,
            status_code=401,
        )

    request.session.clear()
    request.session["username"] = user.username
    request.session["last_activity"] = time.monotonic()

    if next_path and next_path.startswith("/"):
        return RedirectResponse(url=next_path, status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.get("/topology-builder", response_class=HTMLResponse)
def topology_builder_page(request: Request):
    """Éditeur visuel de topologies containerlab (standalone, intégrable dans VLM)."""
    # Auth guard — redirect to home/login if no valid session
    from src.services.auth import get_user
    username = request.session.get("username") or ""
    if not username or get_user(username) is None:
        redirect_url = "/"
        qs = str(request.url.query)
        # Preserve query params so the page can be reopened after login
        next_path = "/topology-builder" + ("?" + qs if qs else "")
        return RedirectResponse(url=f"/?next={next_path}", status_code=303)
    topo_js_path = os.path.join(os.path.dirname(__file__), "static", "js", "topology-builder.js")
    try:
        topo_js_version = int(os.path.getmtime(topo_js_path))
    except OSError:
        topo_js_version = 0
    return templates.TemplateResponse(
        "topology-builder.html",
        {"request": request, "topo_js_version": topo_js_version},
    )
