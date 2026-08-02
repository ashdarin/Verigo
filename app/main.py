from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import secrets

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth import auth_router
from app.api.routes import require_prospecting_beta, router
from app.config import BASE_DIR, settings
from app.core.legacy import load_persistent_cache, save_persistent_cache
from app.db.jobs import job_store
from app.db.auth import auth_store
from app.db.metrics import metrics_store
from app.db.prospecting import prospecting_store


STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    # Keep the public process ready to serve requests. Migrations, repairs, and
    # remote-node supervision run as explicit maintenance services.
    job_store.initialize()
    auth_store.initialize()
    metrics_store.initialize()
    prospecting_store.initialize()
    load_persistent_cache()
    try:
        yield
    finally:
        save_persistent_cache()


app = FastAPI(
    title=f"{settings.app_name} API",
    description="Verigo email verification API. User endpoints accept a Bearer API Key.",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def record_home_visit(client_host: str, user_agent: str, session_id: str) -> None:
    """Persist analytics after the public page has started responding."""
    try:
        metrics_store.record_page_view(client_host, user_agent)
        metrics_store.record_session_page_view(session_id, user_agent)
    except Exception:
        # Statistics must never make the public application unavailable.
        pass


@app.middleware("http")
async def collect_page_views(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in {"/", "/pricing", "/email-verification", "/bulk-email-verification", "/email-list-cleaning"}:
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.method == "GET" and path == "/":
        # Do not put an analytics SQLite write on the visitor's critical path.
        forwarded_for = request.headers.get("x-forwarded-for", "")
        client_host = forwarded_for.split(",", 1)[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        session_id = request.cookies.get("verigo_analytics") or secrets.token_urlsafe(24)
        response.set_cookie(
            key="verigo_analytics", value=session_id, max_age=1800, httponly=True,
            secure=settings.secure_cookies, samesite="lax", path="/",
        )
        asyncio.create_task(asyncio.to_thread(
            record_home_visit, client_host, request.headers.get("user-agent", ""), session_id,
        ))
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/pricing", include_in_schema=False)
def pricing() -> FileResponse:
    return FileResponse(STATIC_DIR / "pricing.html")


@app.get("/api-docs", include_in_schema=False)
def api_docs() -> FileResponse:
    return FileResponse(STATIC_DIR / "api-docs.html")


@app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
def robots() -> str:
    return "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /api/",
            "Disallow: /dashboard",
            "Sitemap: https://verigo.site/sitemap.xml",
            "",
        ]
    )


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    body = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
  <url><loc>https://verigo.site/</loc></url>
  <url><loc>https://verigo.site/pricing</loc></url>
  <url><loc>https://verigo.site/privacy</loc></url>
  <url><loc>https://verigo.site/acceptable-use</loc></url>
  <url><loc>https://verigo.site/email-verification</loc></url>
  <url><loc>https://verigo.site/bulk-email-verification</loc></url>
  <url><loc>https://verigo.site/email-list-cleaning</loc></url>
</urlset>
"""
    return Response(content=body, media_type="application/xml")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html", headers={"X-Robots-Tag": "noindex, nofollow"}
    )


@app.get(
    "/prospecting-beta",
    include_in_schema=False,
    dependencies=[Depends(require_prospecting_beta)],
)
def prospecting_beta() -> FileResponse:
    return FileResponse(
        BASE_DIR / "private_pages" / "prospecting-beta.html",
        headers={"X-Robots-Tag": "noindex, nofollow, noarchive"},
    )


@app.get("/aigc-demo", include_in_schema=False)
def aigc_demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "aigc-demo" / "index.html")


@app.get("/admin/credits", include_in_schema=False)
def admin_credits() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html", headers={"X-Robots-Tag": "noindex, nofollow"}
    )

@app.get("/wallet", include_in_schema=False)
def wallet() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"X-Robots-Tag": "noindex, nofollow"})

@app.get("/workspace", include_in_schema=False)
def workspace() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"X-Robots-Tag": "noindex, nofollow"})

@app.get("/history", include_in_schema=False)
def history() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"X-Robots-Tag": "noindex, nofollow"})


@app.get("/privacy", include_in_schema=False)
def privacy() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy.html")


@app.get("/acceptable-use", include_in_schema=False)
def acceptable_use() -> FileResponse:
    return FileResponse(STATIC_DIR / "acceptable-use.html")


@app.get("/email-verification", include_in_schema=False)
def email_verification_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "email-verification.html")


@app.get("/bulk-email-verification", include_in_schema=False)
def bulk_email_verification_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "bulk-email-verification.html")


@app.get("/email-list-cleaning", include_in_schema=False)
def email_list_cleaning_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "email-list-cleaning.html")
