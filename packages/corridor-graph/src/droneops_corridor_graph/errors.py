"""Errors for the corridor lane graph.

Every failure is a *load* or *query* defect, never a flight decision: this
package cannot mark anything safe and cannot commit anything.
"""

from __future__ import annotations


class CorridorGraphError(Exception):
    """Base class for corridor-graph failures."""


class GraphLoadError(CorridorGraphError):
    """A bundle document is missing, malformed or internally inconsistent."""


class BundleIntegrityError(CorridorGraphError):
    """A document's bytes do not match the hash its manifest declares."""


class StaleSourceError(CorridorGraphError):
    """The bundle's own provenance has expired; an expired input fails closed."""


class TopologyError(CorridorGraphError):
    """Node or edge identity, direction or geometry is unusable."""


class AltitudeBandError(CorridorGraphError):
    """A lane band is reversed, unknown, or missing its altitude reference."""


class ClosureOverlayError(CorridorGraphError):
    """A closure or restriction window is malformed or names an unknown lane."""


class PathGenerationError(CorridorGraphError):
    """Candidate path generation could not satisfy its contract."""
