"""gis-etl: Chandigarh geodata and obstacle ETL adapter.

Phase 1 data-provenance workstream. It retrieves OpenStreetMap data (Overpass API or a
local Geofabrik extract), clips it to the Chandigarh administrative boundary, measures
how little vertical data exists, and generates a synthetic obstacle model that declares
itself synthetic on every record.

Boundaries that do not move:

* this package touches the outside world and writes snapshots. It never plans, reserves,
  dispatches or commands anything (``.importlinter`` enforces that);
* no geometry is ever invented. An unreachable endpoint degrades to the raw cache and
  says so in the artefact's own provenance;
* every generated height carries ``obstacle_source: synthetic``,
  ``vertical_authority: none``, ``requires_survey: true``, ``synthetic: true`` and
  ``authoritative: false``.

Simulation only. Not authorised for flight.
"""

from __future__ import annotations

from .audit import audit_snapshot
from .cache import CacheRecord, RawCache
from .config import Bbox, GisConfig, load_config
from .coverage import CoverageReport, run_coverage
from .errors import (
    CacheError,
    CacheMissError,
    ConfigError,
    EndpointUnavailableError,
    ExtractionLimitError,
    GisEtlError,
    ImplausibleEndpointError,
    QueryError,
    SyntheticProvenanceError,
)
from .overpass import OverpassClient
from .pipeline import ChandigarhEtl
from .provenance import RetrievalProvenance, assert_synthetic_flags, synthetic_provenance
from .sources import ExtractInfo, inspect_extract
from .vertical import build_obstacles

__all__ = [
    # orchestration
    "ChandigarhEtl",
    "audit_snapshot",
    # retrieval
    "OverpassClient",
    "RawCache",
    "CacheRecord",
    "ExtractInfo",
    "inspect_extract",
    # evidence
    "CoverageReport",
    "run_coverage",
    "build_obstacles",
    # provenance
    "RetrievalProvenance",
    "synthetic_provenance",
    "assert_synthetic_flags",
    # config
    "GisConfig",
    "Bbox",
    "load_config",
    # errors
    "GisEtlError",
    "ConfigError",
    "QueryError",
    "CacheError",
    "CacheMissError",
    "EndpointUnavailableError",
    "ImplausibleEndpointError",
    "ExtractionLimitError",
    "SyntheticProvenanceError",
]
