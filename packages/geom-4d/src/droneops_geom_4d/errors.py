"""Typed rejection vocabulary for the swept-volume kernel.

Every rejection here is a *value* problem the caller can act on, and every one
of them is raised instead of being reported as a plausible-looking number: a
protected volume that silently swallowed a bad input is worse than no volume at
all, because the next layer reads its answer as evidence of separation.

Subclasses:
    * :class:`ProfileError` - a ``RouteProfile`` is unusable: fewer than two
      waypoints, non-monotonic time, a mixed altitude reference or a declared
      speed that its own positions contradict.
    * :class:`PolicyInputError` - a protected-volume policy input is missing,
      out of range, or belongs to a schema version this kernel does not speak.
    * :class:`SweepGeometryError` - the sweep itself cannot be built or indexed:
      a non-positive discretisation step, a degenerate segment, a footprint whose
      rasterisation would exceed the caller's cell budget, invalid geometry.

These deliberately do *not* subclass one another: catching :class:`Geom4dError`
means "this input was rejected", which is different from "the answer is
uncertain" - and an uncertain answer is a ``UNPROVEN`` verdict, never an
exception.
"""

from __future__ import annotations


class Geom4dError(ValueError):
    """Base class for every geom-4d rejection."""


class ProfileError(Geom4dError):
    """A route profile cannot be interpolated or swept."""


class PolicyInputError(Geom4dError):
    """A versioned protected-volume policy input is absent or out of domain."""


class SweepGeometryError(Geom4dError):
    """A swept volume could not be constructed, sliced or indexed."""
