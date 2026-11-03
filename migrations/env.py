"""Alembic environment. Migrations run as the `migrator` role (owner of all objects).

URL precedence: `-x url=...` on the command line, then `sqlalchemy.url` set programmatically
(test harness), then the MIGRATOR_DATABASE_URL environment variable.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from gateway.adapters.postgres.database import to_sqlalchemy_url

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    url = (
        context.get_x_argument(as_dictionary=True).get("url")
        or config.get_main_option("sqlalchemy.url")
        or os.environ.get("MIGRATOR_DATABASE_URL")
    )
    if not url:
        raise RuntimeError("set MIGRATOR_DATABASE_URL to run migrations")
    return to_sqlalchemy_url(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
