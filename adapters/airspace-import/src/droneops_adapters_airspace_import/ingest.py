"""Turn one restriction document into a normalised snapshot. Observations only.

The adapter's whole job is the recording half of section 16: keep the source, the fetch
time and a version, and hand ``droneops_airspace`` data shaped so *it* can decide what the
data is worth. Concretely:

* ``fetched_at`` is this run's own observation moment, never a timestamp copied from the
  file, so a stale file cannot make the snapshot look fresh;
* a restriction that does not declare ``freshness`` is recorded as ``unknown``, which the
  validity layer reads as stale and fails closed on - the adapter never launders a gap in
  the data into an affirmative value;
* ``content_sha256`` covers the exact bytes read, so a decision record can prove which file
  was in front of the operator;
* nothing is cached, decided, committed, ranked, priced or scheduled here. There is no
  import of ``droneops_reservations``, ``droneops_planner`` or ``droneops_dispatcher``, and
  ``.importlinter`` keeps it that way.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from droneops_airspace import (
    AltitudeLimit,
    FootprintShape,
    FootprintSpec,
    GeoCoord,
    Restriction,
    RestrictionKind,
    RestrictionSnapshot,
    SnapshotAvailability,
)
from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import Freshness, Provenance

from .errors import AirspaceImportError, SnapshotValidationError
from .source import (
    LocalFileRestrictionSource,
    NoAuthorisedFeedConfigured,
    RestrictionSource,
)

SUPPORTED_SCHEMA_VERSION = 1

_REQUIRED_ENTRY_KEYS = (
    "restriction_id",
    "kind",
    "shape",
    "points",
    "altitude_reference",
    "effective_from_utc",
)

_AVAILABILITY_BY_LABEL = {
    "manual_operator_file": SnapshotAvailability.MANUAL_OPERATOR_FILE,
    "authoritative_feed": SnapshotAvailability.AUTHORITATIVE_FEED,
    "no_feed_configured": SnapshotAvailability.NO_FEED_CONFIGURED,
}


@dataclass(frozen=True)
class ImportResult:
    """A snapshot plus the caveats collected on the way in. Both must travel together."""

    snapshot: RestrictionSnapshot
    warnings: tuple[str, ...] = ()
    source_label: str = ""
    content_sha256: str = ""

    @property
    def restriction_count(self) -> int:
        return len(self.snapshot.restrictions)


def import_from_file(path: Path | str, *, now: datetime | None = None) -> ImportResult:
    """Convenience for the only source that exists today: a local file."""

    return import_snapshot(LocalFileRestrictionSource(Path(path)), now=now)


def import_snapshot(
    source: RestrictionSource | None = None, *, now: datetime | None = None
) -> ImportResult:
    """Read whatever the source offers and normalise it, or record that there is nothing."""

    active_source: RestrictionSource = source or NoAuthorisedFeedConfigured()
    moment = _aware_utc(now or datetime.now(UTC))
    if isinstance(active_source, NoAuthorisedFeedConfigured):
        empty = RestrictionSnapshot.no_feed(moment)
        return ImportResult(
            snapshot=empty,
            warnings=(empty.note,),
            source_label=active_source.availability_label,
            content_sha256="",
        )
    document = active_source.read()
    digest = hashlib.sha256(document.raw_bytes).hexdigest()
    payload = _load(document.text, origin=document.name)
    restrictions, warnings = _restrictions_from_payload(
        payload, fetched_at=moment, origin=document.name
    )
    availability = _AVAILABILITY_BY_LABEL.get(active_source.availability_label)
    if availability is None:
        raise SnapshotValidationError(
            f"unknown availability label {active_source.availability_label!r}; a source must "
            "state whether it is a manual file or an authorised feed"
        )
    claimed = payload.get("availability")
    if claimed is not None and str(claimed) != active_source.availability_label:
        raise SnapshotValidationError(
            f"the document declares availability {claimed!r}, which contradicts the configured "
            "source label "
            f"{active_source.availability_label!r}: authority comes from where data was "
            "obtained, not from what the file claims about itself",
            entry=document.name,
        )
    snapshot = RestrictionSnapshot(
        snapshot_id=f"airspace-{digest[:12]}",
        schema_version=int(payload.get("schema_version", SUPPORTED_SCHEMA_VERSION)),
        source=str(payload.get("source") or document.name),
        fetched_at=moment,
        feed_version=str(payload.get("version") or f"sha256:{digest}"),
        availability=availability,
        restrictions=restrictions,
        content_sha256=digest,
    )
    return ImportResult(
        snapshot=snapshot,
        warnings=tuple(warnings),
        source_label=document.name,
        content_sha256=digest,
    )


def parse_document(
    text: str, *, fetched_at: datetime, origin: str = "document"
) -> tuple[tuple[Restriction, ...], list[str]]:
    """Entries only: no snapshot identity, no fetch time, no claim about the feed."""

    return _restrictions_from_payload(
        _load(text, origin=origin), fetched_at=fetched_at, origin=origin
    )


def _restrictions_from_payload(
    payload: dict[str, Any], *, fetched_at: datetime, origin: str
) -> tuple[tuple[Restriction, ...], list[str]]:
    raw = payload.get("restrictions")
    if not isinstance(raw, list):
        raise SnapshotValidationError(
            "a restriction document must carry a 'restrictions' list; an absent list is not "
            "a statement that airspace is clear",
            entry=origin,
        )
    warnings: list[str] = []
    restrictions = tuple(
        _entry_to_restriction(item, index=index, fetched_at=fetched_at, warnings=warnings)
        for index, item in enumerate(raw)
    )
    return restrictions, warnings


def _load(text: str, *, origin: str) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SnapshotValidationError(f"unparseable document: {error}", entry=origin) from error
    if not isinstance(payload, dict):
        raise SnapshotValidationError("the document root must be a mapping", entry=origin)
    version = payload.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    if int(version) != SUPPORTED_SCHEMA_VERSION:
        raise SnapshotValidationError(
            f"schema_version {version} is not understood; this build reads "
            f"version {SUPPORTED_SCHEMA_VERSION} only",
            entry=origin,
        )
    return payload


def _entry_to_restriction(
    item: object, *, index: int, fetched_at: datetime, warnings: list[str]
) -> Restriction:
    if not isinstance(item, dict):
        raise SnapshotValidationError(
            "every restriction entry must be a mapping", entry=f"#{index}"
        )
    entry_id = str(item.get("restriction_id") or f"entry-{index}")
    for key in _REQUIRED_ENTRY_KEYS:
        if key not in item:
            raise SnapshotValidationError(f"missing required key {key!r}", entry=entry_id)
    provenance, note = _provenance_of(item, entry_id=entry_id, fetched_at=fetched_at)
    if note:
        warnings.append(note)
    footprint, shape_note = _footprint_of(item, entry_id=entry_id)
    if shape_note:
        warnings.append(shape_note)
    return Restriction(
        restriction_id=entry_id,
        kind=_kind(item["kind"], entry_id=entry_id),
        footprint=footprint,
        altitude=_altitude(item, entry_id=entry_id),
        effective_from=_moment(
            item["effective_from_utc"], field="effective_from_utc", entry_id=entry_id
        ),
        effective_until=_optional_moment(item.get("effective_until_utc"), entry_id=entry_id),
        provenance=provenance,
        label=str(item.get("label") or ""),
    )


def _provenance_of(
    item: dict[str, Any], *, entry_id: str, fetched_at: datetime
) -> tuple[Provenance, str]:
    block = item.get("provenance")
    if not isinstance(block, dict):
        block = {}
    source = str(block.get("source") or f"file-entry:{entry_id}")
    declared = block.get("retrieved_at_utc")
    warning = ""
    if declared is None:
        retrieved = fetched_at
        warning = (
            f"{entry_id}: retrieved_at_utc undeclared, recorded as this fetch with freshness "
            "unknown - the validity layer will fail closed on it, which is intended"
        )
    else:
        retrieved = _moment(declared, field="provenance.retrieved_at_utc", entry_id=entry_id)
    expires = block.get("expires_at_utc")
    return (
        Provenance(
            source=source,
            retrieved_at=retrieved,
            expires_at=(
                None
                if expires is None
                else _moment(expires, field="provenance.expires_at_utc", entry_id=entry_id)
            ),
            freshness=_freshness(block.get("freshness"), entry_id=entry_id),
        ),
        warning,
    )


def _freshness(value: object, *, entry_id: str) -> Freshness:
    if value is None:
        return Freshness.UNKNOWN
    try:
        return Freshness(str(value).lower())
    except ValueError as error:
        allowed = sorted(item.value for item in Freshness)
        raise SnapshotValidationError(
            f"freshness must be one of {allowed}; {value!r} is not, and is not quietly "
            "treated as fresh",
            entry=entry_id,
        ) from error


def _kind(value: object, *, entry_id: str) -> RestrictionKind:
    try:
        return RestrictionKind(str(value).lower())
    except ValueError as error:
        allowed = sorted(item.value for item in RestrictionKind)
        raise SnapshotValidationError(f"kind must be one of {allowed}", entry=entry_id) from error


def _footprint_of(item: dict[str, Any], *, entry_id: str) -> tuple[FootprintSpec, str]:
    points = item.get("points")
    if not isinstance(points, list) or not points:
        raise SnapshotValidationError("'points' must be a non-empty list", entry=entry_id)
    coords: list[GeoCoord] = []
    for pair in points:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise SnapshotValidationError("each point must be [lat, lon]", entry=entry_id)
        coords.append(GeoCoord(lat=float(pair[0]), lon=float(pair[1])))
    shape = str(item.get("shape") or "").lower()
    ring = tuple(coords)
    note = ""
    if shape == "polygon":
        if ring[0] != ring[-1]:
            ring = (*ring, ring[0])
            note = f"{entry_id}: open polygon ring closed automatically"
    elif shape == "circle" and len(ring) != 1:
        raise SnapshotValidationError("a circle takes exactly one centre point", entry=entry_id)
    elif shape == "buffered_line" and len(ring) < 2:
        raise SnapshotValidationError("a buffered line takes at least two points", entry=entry_id)
    elif shape not in {"polygon", "circle", "buffered_line"}:
        raise SnapshotValidationError(
            f"shape {item.get('shape')!r} must be polygon, circle or buffered_line",
            entry=entry_id,
        )
    radius = item.get("radius_m")
    width = item.get("width_m")
    return (
        FootprintSpec(
            shape=FootprintShape(shape),
            points=ring,
            radius_m=None if radius is None else int(radius),
            width_m=None if width is None else int(width),
        ),
        note,
    )


def _altitude(item: dict[str, Any], *, entry_id: str) -> AltitudeLimit:
    try:
        reference = AltitudeReference(str(item["altitude_reference"]).upper())
    except (KeyError, ValueError) as error:
        raise SnapshotValidationError(
            "altitude_reference is required and must be explicit (I9)", entry=entry_id
        ) from error
    floor = item.get("floor_m", 0.0)
    ceiling = item.get("ceiling_m")
    return AltitudeLimit(
        floor_m=float(floor),
        ceiling_m=None if ceiling is None else float(ceiling),
        altitude_reference=reference,
    )


def _optional_moment(value: object, *, entry_id: str) -> datetime | None:
    if value is None or value == "":
        return None
    return _moment(value, field="effective_until_utc", entry_id=entry_id)


def _moment(value: object, *, field: str, entry_id: str) -> datetime:
    if isinstance(value, datetime):
        return _aware_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise SnapshotValidationError(
            f"{field} is not an ISO 8601 timestamp: {value!r}", entry=entry_id
        ) from error
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SnapshotValidationError(
            f"{field} must carry an offset: a naive time is ambiguous (I10)", entry=entry_id
        )
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


__all__ = [
    "AirspaceImportError",
    "ImportResult",
    "import_from_file",
    "import_snapshot",
    "parse_document",
]
