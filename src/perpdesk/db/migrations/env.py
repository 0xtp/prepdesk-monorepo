from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from perpdesk.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("connection_url") or config.get_main_option("sqlalchemy.url")
    if not url:
        from perpdesk.config import get_settings

        url = get_settings().database_url
    return url


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    url = _url()
    kwargs = {} if url.startswith("sqlite") else {"connect_args": {"prepare_threshold": None}}
    eng = create_engine(url, poolclass=pool.NullPool, **kwargs)
    with eng.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
