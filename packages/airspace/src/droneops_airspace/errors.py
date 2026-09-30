"""Errors raised by airspace restriction validity and route eligibility.

Every failure here is a refusal to answer "clear": an unusable restriction, an
unparseable footprint or an attempted misreading of a verdict as a clearance all
produce an error rather than a permissive default.
"""

from __future__ import annotations

from typing import NoReturn


class AirspaceError(ValueError):
    """Base class. A raised error means "no answer", never "no restriction"."""


class GeometryInputError(AirspaceError):
    """A footprint or path is malformed: too few points, a bad radius, a ring."""


class RestrictionInputError(AirspaceError):
    """A restriction record cannot be interpreted: identity, band or window defect."""


class NotAFlightAuthorisationError(AirspaceError):
    """Raised whenever a validity or eligibility result is read as a clearance."""

    def __init__(self, message: str = "") -> None:
        super().__init__(
            message
            or (
                "airspace verdicts are simulation output. They are not a flight "
                "authorisation, clearance or permission, and no field on this result "
                "may be read as one."
            )
        )


def refuse_clearance(subject: str) -> NoReturn:
    """Single chokepoint for "this is not a clearance", so the wording never drifts."""

    raise NotAFlightAuthorisationError(f"{subject} cannot be interpreted as flight authority.")
