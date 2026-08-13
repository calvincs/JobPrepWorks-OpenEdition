import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import (
    ENABLE_DOCS,
    STATIC_DIR,
    TRUSTED_HOSTS,
    llm_config_warnings,
    settings,
)
from app.db import close_pool, init_db
from app.routers import (
    account,
    dashboard,
    insights,
    interviews,
    jobs,
    profile,
    search,
    study,
    tracking,
)
from app.services import pulse as pulse_service
from app.services import reaper as reaper_service
from app.services import usage as usage_service
from app.services.usage import QuotaExceeded
from app.user_errors import USER_ERROR_QUOTA
from app.web import templates

log = logging.getLogger(__name__)


async def _usage_sweeper() -> None:
    """Roll aged llm_requests ledger rows into llm_usage_daily and prune them
    every LLM_LEDGER_SWEEP_INTERVAL seconds (default daily; 0 disables — tests
    call rollup_and_prune() directly). The first sweep runs at boot."""
    while True:
        try:
            await asyncio.to_thread(usage_service.rollup_and_prune)
        except Exception:
            log.exception("usage ledger rollup failed")
        await asyncio.sleep(settings.llm_ledger_sweep_interval)


async def _reaper() -> None:
    """Flip pipeline rows stranded in-flight by a killed process to a retryable
    error every REAPER_INTERVAL seconds (0 disables — tests call reaper.sweep()
    directly). The first sweep runs at boot, which is what recovers the rows
    left behind when you last stopped the server mid-pipeline."""
    while True:
        try:
            await asyncio.to_thread(reaper_service.sweep)
        except Exception:
            log.exception("stale-pipeline sweep failed")
        await asyncio.sleep(settings.reaper_interval)


async def _pulse_poller() -> None:
    """Pick up Company Pulse research that is queued or was stranded by a
    restart, every PULSE_POLL_INTERVAL seconds (0 disables). The first sweep
    runs immediately so a pulse interrupted by Ctrl-C resumes on next start."""
    while True:
        try:
            await asyncio.to_thread(pulse_service.sweep)
        except Exception:
            log.exception("company pulse sweep failed")
        await asyncio.sleep(settings.pulse_poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync route handlers and sync BackgroundTasks (the LLM pipelines) share one
    # anyio threadpool; at the default ~40 tokens a burst of slow LLM calls
    # queues every page load behind it. Must run inside the event loop.
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.threadpool_tokens
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    for warning in llm_config_warnings():
        log.warning("Config: %s", warning)
    init_db()

    tasks = []
    if settings.pulse_poll_interval > 0:
        tasks.append(asyncio.create_task(_pulse_poller()))
    if settings.llm_ledger_sweep_interval > 0:
        tasks.append(asyncio.create_task(_usage_sweeper()))
    if settings.reaper_interval > 0:
        tasks.append(asyncio.create_task(_reaper()))
    yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    close_pool()


# Interactive docs (/docs, /redoc) and the OpenAPI schema are off unless a dev
# opts in — a local tool has no reason to publish its route map by default.
_docs_kwargs = {} if ENABLE_DOCS else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="JobPrep Works", lifespan=lifespan, **_docs_kwargs)

# Pin the Host header: a DNS-rebinding page can otherwise re-point its own
# domain at 127.0.0.1 and read your pages same-origin. "*" (the default) keeps
# local dev open; set TRUSTED_HOSTS if you ever bind this to a real interface.
if TRUSTED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The whole application lives under /app. There is no sign-in and no public
# marketing surface — "/" just sends you into the app. New feature routers go
# on app_router so every path stays under one prefix (templates hardcode
# /app/... paths; there is no URL helper).
app_router = APIRouter(prefix="/app")
app_router.add_api_route("", dashboard.dashboard, methods=["GET"])  # /app = dashboard
app_router.include_router(account.router)
app_router.include_router(profile.router)
app_router.include_router(jobs.router)
app_router.include_router(interviews.router)
app_router.include_router(insights.router)
app_router.include_router(study.router)
app_router.include_router(search.router)
app_router.include_router(tracking.router)
app.include_router(app_router)


# Security headers on every response. The CSP is strict on scripts (all JS is
# self-hosted files; the theme bootstrap was externalized for this) but allows
# inline *style attributes* — templates set CSS custom properties like
# style="--pct: 62%" on meters and trend bars. Do not add inline <script> to a
# template; it will be blocked (and tests/test_security.py fails the build).
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    """Reject cross-site state-changing requests. This app is a browser-facing
    server on localhost with no CSRF token, so without this guard any website
    you visit while it runs could fire a cross-origin form POST at 127.0.0.1
    and delete your data or burn your API credits. We trust the browser's
    Fetch-Metadata first (Sec-Fetch-Site must be same-origin/none) and fall
    back to comparing Origin against the request host. Requests with neither
    header (curl, the test client, same-origin navigations) are allowed — this
    blocks the cross-site browser attack without touching same-origin HTMX."""
    if request.method not in _SAFE_METHODS:
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            if site not in ("same-origin", "none"):
                return Response("Cross-site request blocked.", status_code=403)
        else:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlsplit

                if urlsplit(origin).netloc != request.url.netloc:
                    return Response("Cross-site request blocked.", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
    if request.url.path.startswith("/static/"):
        # static_url() versions asset URLs by mtime, so a long TTL is safe —
        # without an explicit policy browsers cache heuristically and serve
        # stale CSS/JS after an update.
        h.setdefault("Cache-Control", "public, max-age=604800")
    elif request.url.path.startswith("/app") and h.get("content-type", "").startswith("text/html"):
        h.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(QuotaExceeded)
async def quota_exceeded(request: Request, exc: QuotaExceeded):
    """The optional local daily brake (LLM_DAILY_LIMIT) refused the action.
    Raised only from synchronous route paths, before any row is created or task
    enqueued, so refusing here leaves no half-done state. HTMX actions get a
    429 the client won't swap plus a toast; plain form posts get a small page
    explaining which setting did it."""
    if "hx-request" in request.headers:
        return Response(
            status_code=429,
            headers={"HX-Trigger": json.dumps(
                {"toast": {"message": str(exc) or USER_ERROR_QUOTA, "tone": "error"}}
            )},
        )
    return templates.TemplateResponse(
        request,
        "limit.html",
        {"active_nav": None, "message": str(exc) or USER_ERROR_QUOTA},
        status_code=429,
    )


# HEAD as well as GET: `curl -I http://127.0.0.1:8000/` is the obvious way to
# check the server is up, and a 405 there reads like a broken install.
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root() -> RedirectResponse:
    """There is no landing page — the app is the product."""
    return RedirectResponse("/app", status_code=307)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def chrome_devtools_probe() -> Response:
    """Chromium DevTools auto-requests this whenever it's open. We don't use
    it, so answer 204 rather than let it show up as a noisy 404 in the log."""
    return Response(status_code=204)
