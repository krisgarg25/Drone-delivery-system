"""airspace-import: restriction snapshots into DroneOps, from a file.

The only channel through which restriction data may enter the system. It records source,
fetch time and version; it never evaluates, ranks, reserves, dispatches or decides.
``droneops_airspace`` consumes the snapshot this produces, and ``.importlinter`` keeps the
adapter from importing ``droneops_reservations``, ``droneops_planner`` or
``droneops_dispatcher``.

NO AUTHORISED FEED IS CONFIGURED. There is no DigitalSky or UTM client in this package and
no endpoint has been invented for one: the credentials, agreement and licence terms that
would make a live fetch lawful here do not exist yet. The honest Phase 0 behaviour is one
local file path supplied by an operator who already has rights to it, and an explicit
``NoAuthorisedFeedConfigured`` default that raises rather than returning an empty set that
somebody could mistake for clear airspace.

Simulation only. Not authorised for flight, and not a statement about real Indian airspace.
"""

from __future__ import annotations

from .errors import AirspaceImportError, NoAuthorisedFeedError, SnapshotValidationError
from .ingest import ImportResult, import_from_file, import_snapshot, parse_document
from .source import (
    NO_FEED_MESSAGE,
    FileRestrictionDocument,
    LocalFileRestrictionSource,
    NoAuthorisedFeedConfigured,
    RawRestrictionDocument,
    RestrictionSource,
    default_source,
)

__all__ = [
    "NO_FEED_MESSAGE",
    "AirspaceImportError",
    "NoAuthorisedFeedError",
    "SnapshotValidationError",
    "ImportResult",
    "import_from_file",
    "import_snapshot",
    "parse_document",
    "FileRestrictionDocument",
    "LocalFileRestrictionSource",
    "NoAuthorisedFeedConfigured",
    "RawRestrictionDocument",
    "RestrictionSource",
    "default_source",
]
