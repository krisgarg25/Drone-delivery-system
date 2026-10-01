"""Failure modes for facility capacity scheduling.

Rejection is a *result* in this package, not an exception: a full pad returns a
reason-coded :class:`~droneops_facilities.model.Rejection`, never a raise. The
exceptions below are reserved for caller mistakes and data that cannot be
interpreted at all, because those states must stop a run rather than be
reported as "no capacity".
"""

from __future__ import annotations


class FacilitiesError(Exception):
    """Base class for every facility failure."""


class CapacityError(FacilitiesError):
    """A capacity profile is unusable: negative bays, zero-length turnaround."""


class HandoverTypeError(FacilitiesError):
    """A handover type outside the permitted set was requested.

    The permit set has exactly one member (``LAND_TRANSFER``); see
    :func:`droneops_facilities.model.require_supported_handover`.
    """


class ProposalError(FacilitiesError):
    """A proposal is malformed: unsorted claims, a stage outside its resource."""


class CommitConflictError(FacilitiesError):
    """A proposal overlaps an already-committed claim on one resource.

    The scheduler is supposed to prevent this by construction. Raising here is
    the backstop that makes invariant I6 observable at the commit boundary
    instead of only in a post-run audit.
    """


class OutageError(FacilitiesError):
    """An outage window cannot be interpreted against the horizon."""


class BundleLoadError(FacilitiesError):
    """A scenario capacity document is missing a field or has a wrong type."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


__all__ = [
    "BundleLoadError",
    "CapacityError",
    "CommitConflictError",
    "FacilitiesError",
    "HandoverTypeError",
    "OutageError",
    "ProposalError",
]
