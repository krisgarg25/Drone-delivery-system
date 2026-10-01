"""Energy and recovery feasibility kernel: errors.

Every failure in this package is an explicit exception type derived from
:class:`EnergyError`; the kernel never raises a bare ``Exception`` and never
swallows one (conventions section 10). Rejections that are *expected outcomes*
of a feasibility question are not exceptions: they are returned as a verdict on
:class:`~droneops_energy.feasibility.MarginReport`. Exceptions below are
reserved for malformed input and for a safety-relevant refusal to compute.
"""

from __future__ import annotations


class EnergyError(Exception):
    """Base class for every energy-model failure."""


class EnergyInputError(EnergyError):
    """A parameter or profile field is missing, out of range or self-contradictory.

    Raised instead of silently defaulting: an energy number with an undocumented
    origin cannot be reviewed, and every number here is an experiment parameter
    that must be traceable to a named profile version.
    """


class UnknownProfileError(EnergyError):
    """No stored profile has that name and version.

    Profiles are addressed by ``(name, version)`` and pinned by content hash, so a
    run can never drift onto different battery constants by accident.
    """


class ProfileConflictError(EnergyError):
    """A stored profile's content hash does not match the version it claims.

    This means a profile version was edited in place instead of being published
    under a new version string - committed records are append-only.
    """


class RouteBlockedError(EnergyError):
    """A leg cannot be flown as specified, so its energy cost is unbounded.

    Carries the leg index so the rejection can name *where* the mission breaks
    rather than reporting a single opaque failure.
    """

    def __init__(self, message: str, *, leg_index: int) -> None:
        super().__init__(message)
        self.leg_index = leg_index


class TrackHoldingError(RouteBlockedError):
    """The crosswind exceeds the aircraft's airspeed: no heading holds the track."""


class NoProgressError(RouteBlockedError):
    """The headwind cancels the airspeed: the leg would never be completed."""
