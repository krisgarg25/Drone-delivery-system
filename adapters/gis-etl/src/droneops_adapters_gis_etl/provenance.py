"""Provenance records attached to everything this adapter writes.

Conventions section 3: ``source``, ``retrieved_at``, ``expires_at``, ``freshness`` on
anything externally derived, and ``authoritative: false`` / ``synthetic: true`` /
``requires_survey: true`` on anything generated. The base record is the shared
contract model so a snapshot loaded into the platform carries the same shape as every
other externally-derived object in the system.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from droneops_contracts import Freshness, Provenance, ensure_utc

#: Every OSM-derived object in this adapter is licensed ODbL and must say so.
OSM_LICENSE: Final[str] = "ODbL"
OSM_ATTRIBUTION: Final[str] = "(c) OpenStreetMap contributors"

SOURCE_MODE_LIVE: Final[str] = "live"
SOURCE_MODE_CACHE: Final[str] = "cache"
SOURCE_MODE_LOCAL_FILE: Final[str] = "local_file"
SOURCE_MODE_NONE: Final[str] = "none"

SYNTHETIC_FLAGS: Final[tuple[str, ...]] = (
    "obstacle_source",
    "vertical_authority",
    "requires_survey",
    "synthetic",
    "authoritative",
)


def utc_now() -> datetime:
    """Wall-clock UTC at retrieval time.

    Deliberately isolated: the ETL is allowed to read the clock (it records when data
    was fetched), the simulation kernel is not (conventions section 4). Everything
    downstream of this module is replayed from the frozen snapshot.
    """
    return datetime.now(UTC).replace(microsecond=0)


class RetrievalProvenance(Provenance):
    """Provenance of one retrieved artefact: query, endpoint, hash and licence.

    Extra fields are additive on the frozen contract base; ``source`` and
    ``retrieved_at`` keep the contract semantics so a snapshot row validates against
    the platform schema without a translation layer.
    """

    endpoint: str = ""
    query_sha256: str = ""
    query_text: str = ""
    response_sha256: str = ""
    raw_path: str = ""
    source_mode: str = SOURCE_MODE_LIVE
    licence: str = OSM_LICENSE
    attribution: str = OSM_ATTRIBUTION
    replication_timestamp: str = ""
    degraded: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def freshness_for(retrieved_at: datetime, ttl_days: int, now: datetime) -> Freshness:
    """Age of a snapshot against its declared TTL."""
    ensure_utc(retrieved_at)
    ensure_utc(now)
    age = now - retrieved_at
    if age < timedelta(0):
        return Freshness.UNKNOWN
    if age <= timedelta(days=max(1, ttl_days // 3)):
        return Freshness.FRESH
    if age <= timedelta(days=ttl_days):
        return Freshness.AGING
    return Freshness.STALE


def synthetic_provenance(
    *, derived_from: RetrievalProvenance, model_version: str
) -> dict[str, Any]:
    """The provenance block every *generated* vertical value carries.

    These keys are not decoration and they are not optional: the lint test
    ``test_every_generated_obstacle_carries_every_flag`` fails the build if any one of
    them is absent from a generated obstacle.
    """
    return {
        # Where the horizontal footprint came from is still a real measurement.
        "source": derived_from.source,
        "retrieved_at": derived_from.retrieved_at.isoformat(),
        "source_query_sha256": derived_from.query_sha256,
        "source_response_sha256": derived_from.response_sha256,
        "source_raw_path": derived_from.raw_path,
        "source_mode": derived_from.source_mode,
        "licence": derived_from.licence,
        "attribution": derived_from.attribution,
        # The vertical value below it is an assumption, and says so in five ways.
        "obstacle_source": "synthetic",
        "vertical_authority": "none",
        "requires_survey": True,
        "synthetic": True,
        "authoritative": False,
        "vertical_model_version": model_version,
        "altitude_reference": "AGL",
        "survey_note": (
            "Height is an assumption derived from OSM tags, not a measurement. "
            "Chandigarh OSM has essentially no vertical data (see the coverage report). "
            "Site survey or approved obstacle data is required before any use of this "
            "value for clearance."
        ),
    }


def assert_synthetic_flags(properties: dict[str, Any], identifier: str = "") -> None:
    """Raise unless all five synthetic flags are present *and* correctly valued.

    Used by the generator (fail fast at build time) and by the lint test (audit the
    artefacts on disk).
    """
    expected: dict[str, Any] = {
        "obstacle_source": "synthetic",
        "vertical_authority": "none",
        "requires_survey": True,
        "synthetic": True,
        "authoritative": False,
    }
    missing = [key for key, value in expected.items() if properties.get(key) != value]
    if missing:
        from .errors import SyntheticProvenanceError

        raise SyntheticProvenanceError(
            f"generated obstacle {identifier or '<unnamed>'} is missing or has wrong "
            f"synthetic provenance: {sorted(missing)}. A synthetic height that does not "
            f"declare itself is indistinguishable from a survey, and that is the failure "
            f"mode this adapter is not allowed to have."
        )
