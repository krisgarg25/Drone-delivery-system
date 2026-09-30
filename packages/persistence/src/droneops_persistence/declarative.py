"""Declarative base: naming conventions that make a downgrade a real downgrade.

Alembic emits ``ALTER TABLE ... DROP CONSTRAINT`` by name on the way back. Anonymous
constraints get a Postgres-generated name, which a downgrade in a different connection
cannot reference, so every constraint in this schema is named by convention and every
check constraint carries an explicit name.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import MetaData, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    # Exclusions keep their literal name: `11-simulation-routing-engine.md` section 6
    # names the constraint `no_committed_overlap`, and the migration issues that DDL
    # verbatim, so the ORM metadata must not silently rename it.
    "ex": "%(constraint_name)s",
}

metadata = MetaData(naming_convention=convention)

# Conventions section 8: every instant is ``timestamptz(6)``. The generic ``DateTime``
# would render an unpadded ``TIMESTAMP WITH TIME ZONE``, so the dialect type is pinned
# here and every ``Mapped[datetime]`` on the base below resolves to it.
UTCTimestamptz = TIMESTAMP(timezone=True, precision=6)


class Base(DeclarativeBase):
    metadata = metadata
    type_annotation_map = {
        datetime: UTCTimestamptz,
        uuid.UUID: Uuid(as_uuid=True),
    }
