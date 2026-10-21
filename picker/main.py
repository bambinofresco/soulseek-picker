from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import downloading, importing, lidarr_feed, prefetching, searching  # noqa: F401  (register job handlers)
from .lidarr_feed import stuck_scheduler
from .beets import ensure_config as ensure_beets_config
from .clients import close_services
from .config import get_settings
from .db import init_db
from .health import path_warnings
from .jobs import worker
from .web import router

log = logging.getLogger("picker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    ensure_beets_config(settings)
    for warning in path_warnings(settings):
        log.warning(warning)
    worker.start()
    scheduler = asyncio.create_task(stuck_scheduler(), name="stuck-scheduler")
    yield
    scheduler.cancel()
    await worker.stop()
    await close_services()


def create_app() -> FastAPI:
    app = FastAPI(title="Soulseek picker", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(router)
    return app


app = create_app()
