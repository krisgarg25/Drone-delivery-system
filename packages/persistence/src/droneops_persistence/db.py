"""Engine, session and tenant context.

The runtime connects as a **non-superuser application role** (created by the baseline
migration) and always pins the session to UTC before anything else, because several check
constraints in this schema are ``extract(timezone from col) = 0`` -- a check that is only
meaningful if the connection is honest about its timezone.

``tenant_context`` sets ``app.tenant_id`` with ``set_config(..., is_local => true)`` so the
value is scoped to the transaction: a session cannot drift from one tenant to another and
row-level security has nothing left to leak through.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from droneops_platform.ports import TenantContext

DEFAULT_TIMEZONE = "UTC"


def dsn_from_environment() -> str:
    """The DSN, with no silent fallback to a server that happens to own port 5432."""

    for key in ("DRONEOPS_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError(
        "no database URL: set DRONEOPS_DATABASE_URL (or DATABASE_URL) in .env. "
        "There is deliberately no default host/port -- a wrong guess would run "
        "destructive DDL against an unrelated PostgreSQL service."
    )


def to_asyncpg_url(url: str) -> str:
    """Accept a plain ``postgresql://`` DSN and return the async driver form."""

    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        return parsed.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    if parsed.drivername == "postgresql+asyncpg":
        return url
    raise ValueError(f"only PostgreSQL DSNs are supported, got {parsed.drivername!r}")


def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """An async engine whose every connection is UTC and read-committed.

    ``server_settings`` is applied per connection rather than per engine so a pool
    recycle cannot inherit a stranger's timezone.
    """

    resolved = to_asyncpg_url(url or dsn_from_environment())
    return create_async_engine(
        resolved,
        echo=echo,
        pool_pre_ping=True,
        connect_args={"server_settings": {"TimeZone": DEFAULT_TIMEZONE}},
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def set_tenant(session: Any, tenant_id: uuid.UUID) -> None:
    """Bind the transaction to one tenant. RLS is fail-closed without this."""

    await session.execute(
        text("select set_config('app.tenant_id', $1::text, true)"),
        {"tenant_id": str(tenant_id)},
    )
    await session.execute(text("select set_config('timezone', 'UTC', true)"))


async def set_actor(session: Any, context: TenantContext) -> None:
    """Publish the actor and release so a trigger or audit readback can see them."""

    await set_tenant(session, context.tenant_id)
    await session.execute(
        text("select set_config('app.actor', $1::text, true)"), {"actor": context.actor}
    )
    await session.execute(
        text("select set_config('app.config_release_sha', $1::text, true)"),
        {"sha": context.config_release_sha},
    )


class SqlAlchemyUnitOfWork:
    """One transaction implementing the ``UnitOfWork`` port.

    Used as ``with unit_of_work(tenant_context) as uow:`` -- the context is applied inside
    the transaction so ``set_config(..., true)`` is rolled back with everything else.
    """

    def __init__(self, session_factory: async_sessionmaker[Any], context: TenantContext) -> None:
        self._session_factory = session_factory
        self._context = context
        self.session: Any = None

    async def __aenter__(self) -> Any:
        self.session = self._session_factory()
        await self.session.__aenter__()
        await set_actor(self.session, self._context)
        return self.session

    async def __aexit__(self, *exc_info: Any) -> bool:
        assert self.session is not None
        return bool(await self.session.__aexit__(*exc_info))

    async def commit(self) -> None:
        assert self.session is not None
        await self.session.commit()

    async def rollback(self) -> None:
        assert self.session is not None
        await self.session.rollback()


__all__ = [
    "DEFAULT_TIMEZONE",
    "SqlAlchemyUnitOfWork",
    "create_engine",
    "dsn_from_environment",
    "make_session_factory",
    "set_actor",
    "set_tenant",
    "to_asyncpg_url",
]
