"""Engine/session helpers. Schema is owned by Alembic; tests may use create_all."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from leadcore import config

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def default_url() -> str:
    """The DB URL, read from the environment at call time (not frozen at import)."""
    return os.environ.get("LEADCORE_DB_URL") or os.environ.get("DATABASE_URL") or config.DB_URL


def engine(url: str | None = None) -> Engine:
    resolved = url or default_url()
    kwargs: dict = {"future": True}
    if resolved.startswith("postgresql"):
        kwargs["connect_args"] = {"connect_timeout": 5}
        kwargs["pool_pre_ping"] = True
    return create_engine(resolved, **kwargs)


def migrate(url: str | None = None) -> None:
    """Bring the database to Alembic head."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url or default_url())
    command.upgrade(cfg, "head")


@contextmanager
def session(url: str | None = None, *, bind: Engine | None = None):
    maker = sessionmaker(bind=bind or engine(url), future=True, expire_on_commit=False)
    with maker() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
