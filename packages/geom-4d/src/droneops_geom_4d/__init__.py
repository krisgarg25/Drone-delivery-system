"""geom-4d: the swept 4D protected-volume kernel.

A plan is a *function of time*, not a list of places, and the airspace a moving
aircraft protects is the sweep of that function. This package builds that sweep,
intersects it with somebody else's, and returns one of three typed answers.

What is here
    * :class:`RouteProfile` - time, position, speed, altitude and altitude
      reference per waypoint, interpolable to any instant, monotonic time enforced.
    * :func:`sweep_profile` - the exact continuous swept prism per leg, plus
      :func:`discretised_sweep` for a conservative discretisation and
      :func:`frame_snapshots` for the naive per-frame view (rendering and
      contrast only - see :func:`frame_conflict_verdict`).
    * :func:`climb_descent_envelopes` / :func:`layer_occupancies` - manoeuvre
      volumes with the policy's transition buffer, and the altitude layers a climb
      passes through with the times it is in them.
    * :func:`merge_and_crossing_volumes` - the volume an interaction occupies.
    * :func:`conflict_verdict`, :func:`pair_verdict`, :func:`volume_pair_verdict` -
      typed ``SAFE | CONFLICT | UNPROVEN`` results, plus a cell-and-time-window
      pre-filter built on ``geom_core`` cells (:func:`index_volumes`).
    * :class:`ProtectedVolumePolicy` - horizontal margin, vertical margin,
      position uncertainty and timing uncertainty as four separate, versioned
      inputs read from ``contracts/configuration/protected_volume_policy.yaml``.
      Every one of those numbers is an experiment parameter, and that file is
      where the citation lives: no margin, radius, buffer, tolerance or threshold
      is hard-coded anywhere in this package.

Why the answers are worth something
    Two aircraft 40 m apart closing at 40 m/s are half a second from contact. A
    sampler at 1 Hz reports clearance at frame *n*, clearance at frame *n+1*, and
    is wrong. The contact test here works over every integer microsecond of the
    shared window using exact rational arithmetic, so a verdict cannot depend on a
    step size, an epsilon, the order two lists arrived in, or a projected frame.

Deliberately a different geometry path from anything a checker would build
    This is the fast, indexed path: swept prisms rasterised onto ``geom_core``
    cells, a closed-form prism-pair decision procedure for the pairs that survive
    the filter, margins applied from the versioned policy object. The independent
    conflict checker must not import this package (an ``.importlinter`` contract
    says so, and conventions section 6 explains why). A *deliberately different*
    implementation should not copy these internals - it should sweep continuous
    absolute-time movement on shapely geometry, carrying the time on each vertex,
    intersect ``LineString`` against ``LineString`` in space and absolute interval
    against absolute interval in time, recompute protected volumes from raw plan
    points rather than from a policy object, and use no cell or bucket pre-filter
    at all. Slower and independent is the point: two implementations that agree
    are evidence, one implementation is an assertion, and a scenario where they
    disagree is a release blocker rather than a warning.

This package owns swept volume and intersection only. It never owns planning
(conventions section 7), it never reads a clock, and it cannot reach the planner.
"""

from __future__ import annotations

from . import shapes
from .envelope import (
    LayerOccupancy,
    Manoeuvre,
    TransitionEnvelope,
    climb_descent_envelopes,
    describe_envelope,
    layer_occupancies,
)
from .errors import Geom4dError, PolicyInputError, ProfileError, SweepGeometryError
from .index import (
    IndexedVolume,
    VolumeIndex,
    index_volumes,
    prefiltered_pair_verdicts,
    volume_cells,
)
from .interaction import InteractionKind, InteractionVolume, merge_and_crossing_volumes
from .intersect import (
    ContactEvidence,
    IntersectionResult,
    ProtectedPrism,
    ProtectedSlice,
    SweepBasis,
    Verdict,
    VerdictCause,
    merge_windows,
    pair_verdict,
    profile_verdict,
    volume_pair_verdict,
)
from .policy import (
    DEFAULT_POLICY_RELPATH,
    SUPPORTED_SCHEMA_VERSION,
    ProtectedVolumePolicy,
    load_policy_file,
    policy_from_mapping,
    policy_from_yaml_text,
)
from .profile import (
    MAX_POINTS,
    MIN_POINTS,
    ProfilePoint,
    ProfileSegment,
    ProfileState,
    RouteProfile,
    exact_axis,
    quantised_axis,
)
from .sweep import (
    ProtectedBody,
    SweptPrism,
    conflict_verdict,
    discretised_conflict_verdict,
    discretised_sweep,
    frame_conflict_verdict,
    frame_snapshots,
    protected_radius_mm,
    protected_vertical_half_mm,
    static_volume,
    sweep_profile,
    volumes_in_order,
)

__all__ = [
    "DEFAULT_POLICY_RELPATH",
    "MAX_POINTS",
    "MIN_POINTS",
    "SUPPORTED_SCHEMA_VERSION",
    "ContactEvidence",
    "Geom4dError",
    "IndexedVolume",
    "InteractionKind",
    "InteractionVolume",
    "IntersectionResult",
    "LayerOccupancy",
    "Manoeuvre",
    "PolicyInputError",
    "ProfileError",
    "ProfilePoint",
    "ProfileSegment",
    "ProfileState",
    "ProtectedBody",
    "ProtectedPrism",
    "ProtectedSlice",
    "ProtectedVolumePolicy",
    "RouteProfile",
    "SweepBasis",
    "SweepGeometryError",
    "SweptPrism",
    "TransitionEnvelope",
    "Verdict",
    "VerdictCause",
    "VolumeIndex",
    "climb_descent_envelopes",
    "conflict_verdict",
    "describe_envelope",
    "discretised_conflict_verdict",
    "discretised_sweep",
    "exact_axis",
    "frame_conflict_verdict",
    "frame_snapshots",
    "index_volumes",
    "layer_occupancies",
    "load_policy_file",
    "merge_and_crossing_volumes",
    "merge_windows",
    "pair_verdict",
    "policy_from_mapping",
    "policy_from_yaml_text",
    "prefiltered_pair_verdicts",
    "profile_verdict",
    "protected_radius_mm",
    "protected_vertical_half_mm",
    "quantised_axis",
    "shapes",
    "static_volume",
    "sweep_profile",
    "volume_cells",
    "volume_pair_verdict",
    "volumes_in_order",
]
