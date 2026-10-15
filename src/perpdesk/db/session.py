"""Engine and session factory. Serverless-friendly: NullPool on Postgres (Supabase/Neon poolers do the
pooling), prepared statements off so PgBouncer transaction mode works."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from perpdesk.config import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _wal(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng
    return create_engine(url, poolclass=NullPool, connect_args={"prepare_threshold": None}, pool_pre_ping=True)


@lru_cache
def _factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    s = _factory(url)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def migrate(url: str | None = None) -> None:
    """Bring the schema to the latest Alembic revision (idempotent, fast when already current)."""
    from alembic import command
    from alembic.config import Config

    eng = get_engine(url)
    root = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["connection_url"] = str(eng.url.render_as_string(hide_password=False))
    insp = inspect(eng)
    if insp.has_table("alembic_version"):
        with eng.connect() as conn:
            from sqlalchemy import text

            cur = conn.execute(text("select version_num from alembic_version")).scalar()
        from alembic.script import ScriptDirectory

        if cur == ScriptDirectory.from_config(cfg).get_current_head():
            return
    command.upgrade(cfg, "head")
