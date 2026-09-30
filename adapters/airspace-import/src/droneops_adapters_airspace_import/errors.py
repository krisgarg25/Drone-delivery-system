"""Errors for the airspace-import adapter.

Every failure is a refusal to write an observation, never a partial or implied one: a
snapshot that could not be parsed produces an error, and the caller is left with no data
at all - which ``droneops_airspace`` reads as "cannot evaluate", not as "no restrictions".
"""

from __future__ import annotations


class AirspaceImportError(RuntimeError):
    """Base class for the adapter."""


class NoAuthorisedFeedError(AirspaceImportError):
    """Raised when asked to fetch but no authorised feed has been configured.

    This is the expected Phase 0 state, not a transient fault, so it has its own type:
    a caller must not retry it, log it as degradation, or fall back to guessing.
    """


class SnapshotValidationError(AirspaceImportError):
    """A snapshot file is structurally unusable, with the offending entry named."""

    def __init__(self, message: str, *, entry: str = "") -> None:
        super().__init__(f"{entry}: {message}" if entry else message)
        self.entry = entry
