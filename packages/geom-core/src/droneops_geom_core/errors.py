"""Typed rejection vocabulary for geom-core.

Every primitive in this package rejects invalid input by raising one of these.
They all subclass :class:`ValueError` so an unprepared caller still sees a
value problem, while a caller that must distinguish a rejection path can catch
a single narrow base class. Rejection is never silently converted into a
plausible-looking number: that is the difference between a simulator and an
authority.
"""

from __future__ import annotations


class GeomCoreError(ValueError):
    """Base class for every geom-core rejection.

    Subclasses:
        * :class:`TimeDomainError` - an integer-microsecond value left the
          representable domain.
        * :class:`IntervalError` - an interval is malformed (end before start,
          empty operand where a range is required).
        * :class:`SafeIntervalError` - a safe-interval query is inconsistent.
        * :class:`CellDomainError` - a grid cell is outside the declared cell
          lattice or its key is not canonical.
        * :class:`AltitudeReferenceError` - an altitude or band lacks a usable
          reference, or the datum needed to convert it was not supplied.
        * :class:`ResourceKeyError` - a resource key has an unknown kind, an
          empty or illegal identifier, or is not canonical.
        * :class:`GeodeticError` - geographic input is out of range.
    """


class TimeDomainError(GeomCoreError):
    """A microsecond count is not an ``int`` or is outside the int64 domain."""


class IntervalError(GeomCoreError):
    """An interval is malformed, or an operation needs a non-empty operand."""


class SafeIntervalError(GeomCoreError):
    """A safe-interval query carries inconsistent or non-physical parameters."""


class CellDomainError(GeomCoreError):
    """A cell index, level or canonical token is outside the grid lattice."""


class AltitudeReferenceError(GeomCoreError):
    """An altitude reference is unspecified, or the datum for it is missing."""


class ResourceKeyError(GeomCoreError):
    """A resource key is unknown, empty, over-long or not in canonical form."""


class GeodeticError(GeomCoreError):
    """Latitude, longitude or distance is outside the supported domain."""
