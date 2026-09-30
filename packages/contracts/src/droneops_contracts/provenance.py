"""Shared primitives: frozen model base, UTC enforcement, provenance and hashes.

Every contract model derives from :class:`ContractModel`, which is frozen and
rejects unknown fields. The single reusable UTC validator lives here (invariant
I10) and is re-exported through annotated types so no module re-implements it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Any, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    RootModel,
    StringConstraints,
)


class ContractModel(BaseModel):
    """Immutable, closed contract record.

    Frozen because committed operational records are append-only: a correction is
    a superseding record, never an in-place edit (conventions section 5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive or non-UTC datetimes (invariant I10)."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "naive datetime rejected: every timestamp must be timezone-aware UTC (I10)"
        )
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC (I10)")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]

# A configuration-release hash is a canonical ``sha256:<64 hex>`` value; every
# plan and decision records it (conventions section 8).
ConfigReleaseSha = Annotated[
    str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
]

# A bare content/address hash (input hashes, prev/record hashes).
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Freshness(str, Enum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


class Provenance(ContractModel):
    """Where a synthetic or externally-derived object came from.

    Required on any model that "cannot be constructed without provenance".
    """

    source: str = ""
    retrieved_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    freshness: Freshness = Freshness.UNKNOWN

    def is_expired(self, now: UtcDatetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class SyntheticMixin(BaseModel):
    """Marker for synthetic objects: ``authoritative`` defaults to false and is
    hard to strip because it is part of the frozen schema.
    """

    authoritative: bool = False


class JsonValue(
    RootModel[
        Union[  # noqa: UP007 - recursive generic args must stay a concrete Union
            None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]
        ]
    ]
):
    """Recursive JSON value type so contract APIs never use ``Any``."""

    root: bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

    def to_python(self) -> Any:
        value = self.root
        if isinstance(value, list):
            return [item.to_python() if isinstance(item, JsonValue) else item for item in value]
        if isinstance(value, dict):
            return {
                k: (v.to_python() if isinstance(v, JsonValue) else v)
                for k, v in value.items()
            }
        return value


JsonValuePayload = dict[str, JsonValue]
