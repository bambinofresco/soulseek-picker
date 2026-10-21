from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (registers tables)
from .models import CacheEntry, utcnow

_engine: Engine | None = None


def init_db(path: Path | str) -> Engine:
    global _engine
    url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{path}"
    _engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    SQLModel.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """create_all never alters existing tables; add columns that later versions introduced.

    New columns must be nullable (existing rows get NULL), which every model field added
    after the first release has to allow.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    ddl = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))


def session() -> Session:
    if _engine is None:
        raise RuntimeError("database not initialised")
    return Session(_engine, expire_on_commit=False)


def cache_get(key: str, max_age: timedelta) -> Any | None:
    with session() as s:
        entry = s.get(CacheEntry, key)
        if entry and utcnow() - entry.fetched_at < max_age:
            return entry.data
    return None


def cache_put(key: str, data: Any) -> None:
    with session() as s:
        s.merge(CacheEntry(key=key, data=data, fetched_at=utcnow()))
        s.commit()
