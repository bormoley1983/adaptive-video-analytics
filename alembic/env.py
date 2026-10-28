"""Alembic environment for AVA PostgreSQL migrations."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_dsn: str = os.environ.get("AVA_PG_DSN", "")


def _render_url(dsn: str) -> str:
    """SQLAlchemy URL for Alembic bookkeeping; real connection uses _dsn."""
    if dsn.startswith(("postgresql://", "postgres://")):
        url = make_url(dsn)
        if url.drivername in ("postgres", "postgresql"):
            url = url.set(drivername="postgresql+psycopg")
        return url.render_as_string(hide_password=False).replace("%", "%%")
    return "postgresql+psycopg://localhost/placeholder".replace("%", "%%")


if _dsn:
    config.set_main_option("sqlalchemy.url", _render_url(_dsn))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the database)."""
    url = config.get_main_option("sqlalchemy.url")

    def _creator():
        import psycopg

        return psycopg.connect(_dsn)

    connectable = create_engine(url, creator=_creator)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with connection.begin():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
