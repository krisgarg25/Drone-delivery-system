"""Fixed-point spatial units and deterministic rounding.

Time is integer microseconds (see :mod:`droneops_geom_core.timebase`). Space is
carried the same way: integer **millimetres**. Keeping quantisation to one
explicit, documented step means a float never leaks into a stored key, a
resource identifier or a comparison, which is what makes a replay bit-identical
across machines (conventions.md section 4, invariant I7).

Quantisation rule
    Conversion goes through :class:`decimal.Decimal` (exact for a given float
    bit pattern) and is rounded with ``ROUND_HALF_UP`` to whole millimetres.
    The error against the exact real value is bounded by half a millimetre;
    callers are given :data:`QUANTIZATION_TOLERANCE_MM` of one whole millimetre
    so a round trip through the boundary can never disagree with itself.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final, NewType

from .errors import GeomCoreError

Millimetre = NewType("Millimetre", int)
"""Integer millimetres: the only spatial quantity geom-core stores."""

MM_PER_METRE: Final[int] = 1_000
MM_PER_KILOMETRE: Final[int] = 1_000_000
MICROMETRES_PER_MM: Final[int] = 1_000

QUANTIZATION_TOLERANCE_MM: Final[int] = 1
"""Stated tolerance for any value that passed through the metre boundary.

A single quantisation contributes at most ``0.5 mm``; a conversion chain of two
quantisations stays inside ``1 mm``. This is the number tests and downstream
packages compare against, not the theoretical half-unit error.
"""


def as_millimetres(value: object) -> Millimetre:
    """Validate and coerce an arbitrary runtime value to :data:`Millimetre`.

    ``bool`` is rejected deliberately: ``True`` is an ``int`` in Python and a
    distance that silently equals one millimetre is worse than a crash.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeomCoreError(f"millimetre value must be an int, got {type(value).__name__}")
    return Millimetre(value)


def metres_to_millimetres(metres: float) -> Millimetre:
    """Quantise a metre value to integer millimetres, ``ROUND_HALF_UP``.

    Raises:
        GeomCoreError: for ``nan``, ``inf`` or a value Decimal cannot represent.
    """
    try:
        exact = Decimal(metres)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise GeomCoreError(f"metre value is not convertible: {metres!r}") from error
    if not exact.is_finite():
        raise GeomCoreError(f"metre value is not finite: {metres!r}")
    return Millimetre(int((exact * MM_PER_METRE).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def millimetres_to_metres(millimetres: int) -> float:
    """Return a millimetre value as metres.

    Display and adapter boundary only: never feed the result back into a key,
    a comparison or a stored value object.
    """
    return as_millimetres(millimetres) / MM_PER_METRE


def floor_div(numerator: int, denominator: int) -> int:
    """Floor division for lattice indices, defined for negative numerators.

    Truncating division (``int(a / b)``) rounds towards zero and would place
    ``-1 mm`` into cell ``0`` next to ``+1 mm``; a grid must not have an
    oversized cell straddling the origin. Every index computation here floors.
    """
    if denominator == 0:
        raise GeomCoreError("denominator must be non-zero")
    if denominator < 0:
        raise GeomCoreError("denominator must be positive for a lattice index")
    return numerator // denominator


def zigzag_encode(value: int) -> int:
    """Bijection from signed integers to non-negative, small-first ordering."""
    return 2 * value if value >= 0 else -2 * value - 1


def zigzag_decode(value: int) -> int:
    """Inverse of :func:`zigzag_encode`."""
    if value < 0:
        raise GeomCoreError(f"zigzag value must be non-negative, got {value}")
    return value >> 1 if value % 2 == 0 else -((value + 1) >> 1)
