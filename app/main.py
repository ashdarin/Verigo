from __future__ import annotations

from contextlib import asynccontextmanager
import secrets

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.auth import auth_router
from app.api.routes import router
from app.config import BASE_DIR, settings
from app.core.legacy import load_persistent_cache, save_persistent_cache
from app.core.worker_lifecycle import worker_lifecycle
from app.db.jobs import job_store
from app.db.auth import auth_store
from app.db.metrics import metrics_store


STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    job_store.initialize()
    auth_store.initialize()
    metrics_store.initialize()
    load_persistent_cache()
    worker_lifecycle.start()
    try:
        yield
    finally:
        worker_lifecycle.stop()
        save_persistent_cache()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def collect_page_views(request, call_next):
    response = await call_next(request)
    if request.method == "GET" and request.url.path == "/":
        try:
            forwarded_for = request.headers.get("x-forwarded-for", "")
            client_host = forwarded_for.split(",", 1)[0].strip() or (
                request.client.host if request.client else "unknown"
            )
            metrics_store.record_page_view(client_host, request.headers.get("user-agent", ""))
            existing_session = request.cookies.get("verigo_analytics")
            session_id = (
                existing_session
                if existing_session and metrics_store.session_is_active(existing_session)
                else secrets.token_urlsafe(24)
            )
            metrics_store.record_session_page_view(session_id, request.headers.get("user-agent", ""))
            response.set_cookie(
                key="verigo_analytics", value=session_id, max_age=1800, httponly=True,
                secure=settings.secure_cookies, samesite="lax", path="/",
            )
        except Exception:
            # Statistics must never make the public application unavailable.
            pass
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
