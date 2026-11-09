"""Alembic environment. The database URL comes from DATABASE_URL."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from cognitive_twin.db.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
# configparser treats "%" as interpolation, and URL-encoded hosts contain it
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
