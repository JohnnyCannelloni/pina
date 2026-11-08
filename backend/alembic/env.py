"""Alembic's entry point — runs on every `alembic` command.

Its job is to answer two questions:
  1. Which database? (from app.config, so it matches the app exactly)
  2. What should the schema look like? (Base.metadata, so --autogenerate works)

The `x` argument lets the tests point Alembic at the test database without
editing any file:
    alembic -x db_url=postgresql+psycopg://... upgrade head
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings

# Importing the models has a side effect that matters: it registers every table
# on Base.metadata. Without this import, target_metadata would be empty and
# `alembic revision --autogenerate` would cheerfully generate a migration that
# drops all your tables.
from app.db import Base
from app import models  # noqa: F401  (imported for the side effect above)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Which database to migrate, most specific wins:

    1. `-x db_url=...` on the command line — a one-off override.
    2. `sqlalchemy.url` set on the Config object — how tests/conftest.py points
       this at pina_test without touching any file.
    3. The app's own DATABASE_URL, so a plain `alembic upgrade head` migrates
       exactly the database the API will use. This is the normal path.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_args:
        return x_args["db_url"]

    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    return settings.DATABASE_URL


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`).

    Useful when a DBA has to review or apply the SQL by hand. Not used locally,
    but it costs eight lines to keep the option open.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations. This is the normal path."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        # NullPool: this process runs a handful of statements and exits, so
        # pooling connections for reuse buys nothing.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Compare column types on autogenerate, so changing a String to a
            # Float is actually noticed. Off by default, which surprises people.
            compare_type=True,
        )
        # Postgres can run DDL inside a transaction, so a migration that fails
        # halfway rolls back completely and leaves no half-applied schema.
        # (MySQL cannot do this. It is a real reason to prefer Postgres.)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
