"""alembic entry point. the database url comes from app.config, or from
`-x db_url=...` on the command line."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings

# registers every table on Base.metadata, which autogenerate needs.
from app.db import Base
from app import models  # noqa: F401  (imported for the side effect above)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """which database to migrate, most specific first:

    1. `-x db_url=...` on the command line
    2. `sqlalchemy.url` on the Config (how tests/conftest.py uses pina_test)
    3. the app's DATABASE_URL
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_args:
        return x_args["db_url"]

    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    return settings.DATABASE_URL


def run_migrations_offline() -> None:
    """emit sql instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """connect and run the migrations."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        # short-lived process, no need for a pool.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # notice column type changes on autogenerate.
            compare_type=True,
        )
        # postgres runs ddl in a transaction, so a failed migration rolls back fully.
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
