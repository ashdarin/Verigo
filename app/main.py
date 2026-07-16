from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import auth_router
from app.api.routes import router
from app.config import BASE_DIR, settings
from app.core.legacy import load_persistent_cache, save_persistent_cache
from app.db.jobs import job_store
from app.db.auth import auth_store


STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    job_store.initialize()
    auth_store.initialize()
    load_persistent_cache()
    yield
    save_persistent_cache()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(auth_router)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
