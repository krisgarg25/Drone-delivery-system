"""Alembic environment: async engine, UTC-everywhere, explicit model registry.

Three decisions here are load-bearing:

* the URL comes from the environment and never from ``alembic.ini``, and a missing URL is
  an error rather than a default -- see ``droneops_persistence.db.dsn_from_environment``;
* the connection pins ``TimeZone = UTC`` before any DDL, because several check constraints
  in this schema assert ``extract(timezone from col) = 0`` and a non-UTC migration
  connection would build a schema that refuses its own writes;
* ``target_metadata`` is the real model metadata, so ``alembic revision --autogenerate``
  diffs against the ORM instead of a blank page. Migrations are still hand-authored:
  exclusion constraints, RLS policies and GiST indexes are not something to trust to a
  diff.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "packages" / "persistence" / "src"))

from droneops_persistence.db import to_asyncpg_url  # noqa: E402
from droneops_persistence.declarative import metadata as target_metadata  # noqa: E402
import droneops_persistence.models  # noqa: E402,F401 - registers every table

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    cli_url = context.get_x_argument(as_dictionary=True).get("url")
    if cli_url:
        return to_asyncpg_url(cli_url)
    for key in ("DRONEOPS_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return to_asyncpg_url(value)
    raise RuntimeError(
        "Alembic has no database URL. Set DRONEOPS_DATABASE_URL (or DATABASE_URL) in "
        ".env, or pass -x url=<dsn>. There is deliberately no localhost:5432 fallback."
    )


config.set_main_option("sqlalchemy.url", _url().replace("%", "%%"))


def _configure_connection(connection: Connection) -> None:
    """UTC and a stable search path before a single statement of DDL runs."""

    connection.exec_driver_sql("SET TIME ZONE 'UTC'")
    connection.exec_driver_sql("SET search_path = public")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        # Extensions, enums, triggers and RLS are part of the schema; autogenerate would
        # otherwise try to drop them on the next revision.
        include_object=lambda obj, name, type_, reflected, compare_to: not (
            type_ == "index" and name in {"spatial_ref_sys_pkey", "gist_geometry_ops_and"}
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure_connection(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"TimeZone": "UTC"}},
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
