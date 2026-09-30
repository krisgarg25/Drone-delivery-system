"""Merge and crossing volume generation.

Two aircraft whose protected volumes touch are in conflict; two aircraft that
are *about* to share a corridor need a different artefact: the volume that the
interaction itself occupies, so it can be reserved, priced and explained. This
module produces those volumes from a pair of swept prism sets.

The classification is derived, not tuned. There is no "merge angle" in the
protected-volume policy, and inventing one here would be exactly the kind of
hard-coded aviation number the conventions forbid. So:

* **CROSSING** - the two legs' horizontal velocity vectors are not parallel. The
  sign of the 2D cross product of integer/rational velocities is an exact
  arithmetic fact; a crossing does not need a tolerance, it needs both headings
  to differ.
* **MERGE** - the legs are parallel and the bodies are inside each other's
  protection: joining a lane, flying in company, or overtaking. Same arithmetic,
  zero cross product.

Each interaction window is widened by the policy's transition buffer on both
sides, because a merge is a manoeuvre and a manoeuvre is what runs late. The
shared footprint and shared altitude band come from the two prisms' own extents,
so the volume that gets reserved is the volume that was actually contested.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from droneops_geom_core import AltitudeBand, Interval, Microsecond

from . import shapes
from .errors import SweepGeometryError
from .intersect import ProtectedPrism, centre_at, scan_prism_pair, velocity_of
from .policy import ProtectedVolumePolicy


class InteractionKind(StrEnum):
    MERGE = "MERGE"
    CROSSING = "CROSSING"


@dataclass(frozen=True)
class InteractionVolume:
    """The contested volume of one interaction, over one window."""

    volume_id: str
    kind: InteractionKind
    members: tuple[str, str]
    window: Interval
    footprint_wkt: str
    band: AltitudeBand
    transition_buffer_us: Microsecond
    policy_key: str
    authoritative: bool

    @property
    def is_pairwise(self) -> bool:
        return len(set(self.members)) == 2


def classify_interaction(
    left_velocity: tuple[Fraction, Fraction, Fraction],
    right_velocity: tuple[Fraction, Fraction, Fraction],
) -> InteractionKind:
    """Exact heading relationship: cross product sign, never an angle threshold."""
    cross = left_velocity[0] * right_velocity[1] - left_velocity[1] * right_velocity[0]
    return InteractionKind.CROSSING if cross != 0 else InteractionKind.MERGE


def _shared_band(left: ProtectedPrism, right: ProtectedPrism) -> AltitudeBand:
    """The altitude both bodies contest. A contact proves this is non-empty."""
    pieces = left.band().intersection(right.band())
    if not pieces:
        raise SweepGeometryError(
            f"{left.volume_id} and {right.volume_id} touched but share no altitude band"
        )
    lower = min(piece.lower_mm for piece in pieces)
    upper = max(piece.upper_mm for piece in pieces)
    return AltitudeBand(left.reference, lower, upper)


def _shared_footprint(left: ProtectedPrism, right: ProtectedPrism, at_us: int) -> str:
    """Horizontal region both protections cover, falling back to the connecting capsule."""
    shared = shapes.overlap_wkt(left.footprint_wkt(), right.footprint_wkt())
    if shapes.is_usable(shared):
        return shared
    ax, ay, _ = centre_at(left, at_us)
    bx, by, _ = centre_at(right, at_us)
    reach = max(left.radius_mm, right.radius_mm)
    return shapes.capsule_wkt(
        [(math.floor(ax), math.floor(ay)), (math.floor(bx), math.floor(by))], reach
    )


def merge_and_crossing_volumes(
    left: Sequence[ProtectedPrism],
    right: Sequence[ProtectedPrism],
    policy: ProtectedVolumePolicy,
) -> tuple[InteractionVolume, ...]:
    """Every merge and crossing volume between two swept prism sets.

    Pairs whose altitudes live in different reference frames are skipped rather
    than guessed at; the caller's verdict on that pair is ``UNPROVEN`` from
    :func:`droneops_geom_4d.intersect.pair_verdict`, and an interaction volume
    built on a fabricated datum would be worse than no volume at all.

    Returns runs in window order, then identifier order, so the sequence is a
    function of the inputs alone.
    """
    buffer_us = policy.transition_buffer_us
    found: list[InteractionVolume] = []
    for _left_index, first in enumerate(left):
        for _right_index, second in enumerate(right):
            if first.reference is not second.reference:
                continue
            scan = scan_prism_pair(first, second)
            for lo, hi in scan.windows:
                kind = classify_interaction(velocity_of(first), velocity_of(second))
                found.append(
                    InteractionVolume(
                        volume_id=(
                            f"interaction:{kind.value.lower()}:{first.volume_id}+{second.volume_id}"
                        ),
                        kind=kind,
                        members=(first.volume_id, second.volume_id),
                        window=Interval.of(lo - buffer_us, hi + buffer_us + 1),
                        footprint_wkt=_shared_footprint(first, second, lo),
                        band=_shared_band(first, second),
                        transition_buffer_us=Microsecond(buffer_us),
                        policy_key=policy.key(),
                        authoritative=first.authoritative and second.authoritative,
                    )
                )
    return tuple(sorted(found, key=lambda item: (item.window.start, item.volume_id)))
