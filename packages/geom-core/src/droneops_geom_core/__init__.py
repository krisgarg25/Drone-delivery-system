"""geom-core: the pure spatial-temporal primitive kernel of DroneOps.

Everything above the kernel that has to *compute* something about time and space
starts here: integer-microsecond time, half-open interval algebra, safe intervals
against committed occupancy, projected grid cells and hierarchical volume keys,
altitude bands that always carry their reference, and canonical resource keys with
a single total order for lock acquisition.

The rules the package is built to (conventions.md sections 3, 4 and 7):

* **stdlib only.** No database, HTTP, aircraft SDK or simulator dependency; the
  test suite scans the source for forbidden imports and fails the build if one
  appears.
* **Integer units.** Time is ``int`` microseconds, space is ``int`` millimetres.
  Floats survive only inside geodetic formulas and are quantised once, at a
  documented boundary, with a stated tolerance.
* **No wall clock.** Nothing reads a clock, the environment, the filesystem or
  the network, so the same inputs give the same answers on every replay.
* **Explicit ordering.** Every sequence-returning function documents its total
  order, and none of them iterates an unsorted set or dict to produce output.
* **Rejection is a result.** Unknown resource kinds, empty identifiers, reversed
  bands, naive datetimes and ``UNSPECIFIED`` altitudes raise. A safe-interval
  query reports *which* commitment blocked it and *why* a gap was too small.

``geom-core`` owns intervals, safe intervals, cells and references. It does not
own swept 4D volumes (that is ``geom-4d``) and must never own persistence. The
independent conflict checker deliberately keeps its own simple interval logic:
two implementations that agree are evidence, one implementation is an assertion.
"""

from __future__ import annotations

from .altitude import (
    ALTITUDE_REFERENCE_ORDER,
    Altitude,
    AltitudeBand,
    AltitudeReference,
    AltitudeReferenceError,
    VerticalDatums,
    band_sequence_is_disjoint,
    merge_bands,
    reference_from_token,
    subtract_band,
)
from .cells import (
    CHANDIGARH_UTM_EPSG,
    BASE32_ALPHABET,
    Cell2D,
    Cell3D,
    GridSpec,
    cell_sequence_is_disjoint,
    merge_cells_by_token,
)
from .errors import (
    CellDomainError,
    GeomCoreError,
    GeodeticError,
    IntervalError,
    ResourceKeyError,
    SafeIntervalError,
    TimeDomainError,
)
from .geo import (
    EARTH_RADIUS_M,
    GEODESIC_RELATIVE_TOLERANCE,
    MAX_PLANAR_EXTENT_M,
    GeoPoint,
    LocalPlane,
    PlanarOffset,
    Waypoint,
    destination_point,
    final_bearing_deg,
    geodesic_distance_mm,
    haversine_distance_m,
    initial_bearing_deg,
    normalize_deg,
    traversal_microseconds,
    wrap_longitude,
)
from .intervals import (
    INVERSE_RELATION,
    RangeRelation,
    gaps_within,
    intersect_all,
    merge,
    overlaps_any,
    shift_all,
    sort_key,
    subtract,
    total_measure,
)
from .intervals import Interval
from .resources import (
    RESOURCE_KIND_ORDER,
    ResourceKey,
    ResourceKind,
    kind_from_token,
    lock_order,
    resource_key,
    sorted_resource_keys,
    validate_identifier,
)
from .safe_intervals import (
    EntryDecision,
    EntryStatus,
    GapAudit,
    GapReason,
    OccupiedWindow,
    SafeIntervalPadding,
    SafeIntervalQuery,
    SafeIntervalResult,
    SafeIntervalStatus,
    SafeWindow,
    classify_entry,
    compute_safe_intervals,
    describe_result,
    earliest_entry_on_or_after,
)
from .timebase import (
    DATETIME_REPRESENTABLE_MAX_US,
    DATETIME_REPRESENTABLE_MIN_US,
    EPOCH,
    INT64_MICROSECOND_MAX,
    INT64_MICROSECOND_MIN,
    MICROS_PER_DAY,
    MICROS_PER_HOUR,
    MICROS_PER_MILLISECOND,
    MICROS_PER_MINUTE,
    MICROS_PER_SECOND,
    UTC,
    Microsecond,
    as_duration_us,
    as_microseconds,
    checked_add,
    datetimes_to_microseconds,
    describe_span,
    from_microseconds,
    saturating_add,
    to_microseconds,
)
from .units import (
    MM_PER_METRE,
    QUANTIZATION_TOLERANCE_MM,
    Millimetre,
    as_millimetres,
    floor_div,
    metres_to_millimetres,
    millimetres_to_metres,
    zigzag_decode,
    zigzag_encode,
)

__all__ = [
    'ALTITUDE_REFERENCE_ORDER',
    'BASE32_ALPHABET',
    'CHANDIGARH_UTM_EPSG',
    'DATETIME_REPRESENTABLE_MAX_US',
    'DATETIME_REPRESENTABLE_MIN_US',
    'EARTH_RADIUS_M',
    'EPOCH',
    'GEODESIC_RELATIVE_TOLERANCE',
    'INT64_MICROSECOND_MAX',
    'INT64_MICROSECOND_MIN',
    'INVERSE_RELATION',
    'MAX_PLANAR_EXTENT_M',
    'MICROS_PER_DAY',
    'MICROS_PER_HOUR',
    'MICROS_PER_MILLISECOND',
    'MICROS_PER_MINUTE',
    'MICROS_PER_SECOND',
    'MM_PER_METRE',
    'QUANTIZATION_TOLERANCE_MM',
    'RESOURCE_KIND_ORDER',
    'UTC',
    'Altitude',
    'AltitudeBand',
    'AltitudeReference',
    'AltitudeReferenceError',
    'Cell2D',
    'Cell3D',
    'CellDomainError',
    'EntryDecision',
    'EntryStatus',
    'GapAudit',
    'GapReason',
    'GeoPoint',
    'GeodeticError',
    'GeomCoreError',
    'GridSpec',
    'Interval',
    'IntervalError',
    'LocalPlane',
    'Microsecond',
    'Millimetre',
    'OccupiedWindow',
    'PlanarOffset',
    'RangeRelation',
    'ResourceKey',
    'ResourceKeyError',
    'ResourceKind',
    'SafeIntervalError',
    'SafeIntervalPadding',
    'SafeIntervalQuery',
    'SafeIntervalResult',
    'SafeIntervalStatus',
    'SafeWindow',
    'TimeDomainError',
    'VerticalDatums',
    'Waypoint',
    'as_duration_us',
    'as_microseconds',
    'as_millimetres',
    'band_sequence_is_disjoint',
    'cell_sequence_is_disjoint',
    'checked_add',
    'classify_entry',
    'compute_safe_intervals',
    'datetimes_to_microseconds',
    'describe_result',
    'describe_span',
    'destination_point',
    'earliest_entry_on_or_after',
    'final_bearing_deg',
    'floor_div',
    'from_microseconds',
    'gaps_within',
    'geodesic_distance_mm',
    'haversine_distance_m',
    'initial_bearing_deg',
    'intersect_all',
    'kind_from_token',
    'lock_order',
    'merge',
    'merge_bands',
    'merge_cells_by_token',
    'metres_to_millimetres',
    'millimetres_to_metres',
    'normalize_deg',
    'overlaps_any',
    'reference_from_token',
    'resource_key',
    'saturating_add',
    'shift_all',
    'sort_key',
    'sorted_resource_keys',
    'subtract',
    'subtract_band',
    'to_microseconds',
    'total_measure',
    'traversal_microseconds',
    'validate_identifier',
    'wrap_longitude',
    'zigzag_decode',
    'zigzag_encode',
]
