"""Altitude references, vertical datums and altitude bands.

conventions.md section 3 and invariant I9 are absolute: no altitude value is
accepted or returned without its reference. That is why there is no bare
altitude number in this package. :class:`Altitude` and :class:`AltitudeBand`
both *require* an :class:`AltitudeReference`, and a value whose reference is
``UNSPECIFIED`` cannot be constructed at all: the rejection happens on the way
in, so an uncertain altitude can never be converted, compared or reserved by
guessing which datum someone meant.

Reference semantics, all of them offsets below AMSL
    ``AMSL``        height above mean sea level. No datum needed.
    ``AGL``         height above the terrain directly beneath the aircraft.
                    Needs ``terrain_amsl_mm``.
    ``GROUND``      height above a surveyed **site datum** plane (a fixed
                    benchmark at the facility), not above varying terrain. Needs
                    ``site_datum_amsl_mm``. It differs from ``AGL`` precisely
                    where the terrain is not flat.
    ``PAD``         height above a specific pad deck. Needs ``pad_deck_amsl_mm``;
                    if the deck elevation was never surveyed the conversion
                    raises instead of assuming the deck is at ground level.

Units are integer millimetres, so band arithmetic and key round-trips are exact.
The float-to-integer boundary is :func:`~droneops_geom_core.units.metres_to_millimetres`
and carries a stated tolerance of one millimetre.

Ordering
    :func:`merge_bands` returns ascending ``(reference, lower, upper)`` and the
    canonical form is non-overlapping and non-touching *within one reference*.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .errors import AltitudeReferenceError
from .units import (
    Millimetre,
    as_millimetres,
    floor_div,
    metres_to_millimetres,
    millimetres_to_metres,
)

_KEY_SEPARATOR: Final[str] = "|"


class AltitudeReference(StrEnum):
    """The five reference frames, exactly as the contracts enumerate them."""

    AGL = "AGL"
    AMSL = "AMSL"
    GROUND = "GROUND"
    PAD = "PAD"
    UNSPECIFIED = "UNSPECIFIED"


ALTITUDE_REFERENCE_ORDER: Final[tuple[AltitudeReference, ...]] = (
    AltitudeReference.AMSL,
    AltitudeReference.GROUND,
    AltitudeReference.AGL,
    AltitudeReference.PAD,
    AltitudeReference.UNSPECIFIED,
)
"""Documented total order for bands and for any sequence of altitudes.

AMSL first because it is the absolute frame every other one is derived from;
``UNSPECIFIED`` last because it is never a valid value, only a rejection.
"""

_CONSTRUCTIBLE_REFS: Final[frozenset[AltitudeReference]] = frozenset(
    ALTITUDE_REFERENCE_ORDER[:4]
)

BAND_KEY_PREFIX: Final[str] = "band"
ALTITUDE_KEY_PREFIX: Final[str] = "alt"


def reference_from_token(token: str) -> AltitudeReference:
    """Parse a reference name case-sensitively; unknown names raise.

    Deliberately not forgiving about case: ``"agl"`` in a stored record means
    somebody wrote it from a different vocabulary, and silently accepting it
    hides that.
    """
    for candidate in ALTITUDE_REFERENCE_ORDER:
        if candidate.value == token:
            return candidate
    raise AltitudeReferenceError(f"unknown altitude reference {token!r}")


def _require_constructible(reference: AltitudeReference) -> AltitudeReference:
    if reference not in _CONSTRUCTIBLE_REFS:
        raise AltitudeReferenceError(
            f"{reference.value} cannot carry a height value; supply the datum and "
            "convert to AGL, AMSL, GROUND or PAD (invariant I9)"
        )
    return reference


@dataclass(frozen=True)
class VerticalDatums:
    """Elevations, in millimetres AMSL, that make relative references meaningful.

    This is data, not a guess: a real deployment sources it from a surveyed
    terrain model and a site record. Synthetic worlds must state it explicitly.
    """

    terrain_amsl_mm: Millimetre
    site_datum_amsl_mm: Millimetre
    pad_deck_amsl_mm: Millimetre | None

    def __post_init__(self) -> None:
        as_millimetres(self.terrain_amsl_mm)
        as_millimetres(self.site_datum_amsl_mm)
        if self.pad_deck_amsl_mm is not None:
            as_millimetres(self.pad_deck_amsl_mm)

    @classmethod
    def at_site(
        cls,
        terrain_amsl_m: float,
        site_datum_amsl_m: float | None = None,
        pad_deck_amsl_m: float | None = None,
    ) -> VerticalDatums:
        """Boundary constructor taking metres; each argument is quantised once."""
        terrain = metres_to_millimetres(terrain_amsl_m)
        datum = (
            terrain if site_datum_amsl_m is None else metres_to_millimetres(site_datum_amsl_m)
        )
        deck = None if pad_deck_amsl_m is None else metres_to_millimetres(pad_deck_amsl_m)
        return cls(terrain, datum, deck)

    def flat(self) -> VerticalDatums:
        """A copy where terrain, site datum and pad deck coincide.

        Explicit, so a caller has to say "this site is flat" rather than getting
        it by accident from a missing field.
        """
        return VerticalDatums(self.terrain_amsl_mm, self.terrain_amsl_mm, self.terrain_amsl_mm)

    def offset_for(self, reference: AltitudeReference) -> Millimetre:
        """The AMSL elevation of the zero plane of ``reference``.

        Raises:
            AltitudeReferenceError: ``UNSPECIFIED`` (no plane exists), or ``PAD``
                with no surveyed deck. Missing data is a rejection, never a
                default.
        """
        if reference is AltitudeReference.AMSL:
            return Millimetre(0)
        if reference is AltitudeReference.AGL:
            return self.terrain_amsl_mm
        if reference is AltitudeReference.GROUND:
            return self.site_datum_amsl_mm
        if reference is AltitudeReference.PAD:
            if self.pad_deck_amsl_mm is None:
                raise AltitudeReferenceError(
                    "PAD reference needs a surveyed pad_deck_amsl_mm; refusing to assume "
                    "the deck sits at ground level"
                )
            return self.pad_deck_amsl_mm
        raise AltitudeReferenceError(f"{reference.value} is not a usable altitude reference (I9)")


@dataclass(frozen=True)
class Altitude:
    """A height together with the reference it is measured against. I9 by shape."""

    reference: AltitudeReference
    value_mm: Millimetre

    def __post_init__(self) -> None:
        _require_constructible(self.reference)
        as_millimetres(self.value_mm)

    @classmethod
    def in_reference(cls, value_m: float, reference: AltitudeReference) -> Altitude:
        """Boundary constructor from metres (quantised once, tolerance 1 mm)."""
        return cls(reference, metres_to_millimetres(value_m))

    @property
    def value_m(self) -> float:
        """Metres for rendering only; never compare or store this."""
        return millimetres_to_metres(self.value_mm)

    def as_reference(
        self, reference: AltitudeReference, datums: VerticalDatums
    ) -> Altitude:
        """Convert to another frame. The result always carries its reference.

        Raises:
            AltitudeReferenceError: target is ``UNSPECIFIED``, or the datum the
                target frame needs was not supplied.
        """
        _require_constructible(reference)
        absolute = self.value_mm + datums.offset_for(self.reference)
        return Altitude(reference, Millimetre(absolute - datums.offset_for(reference)))

    def as_amsl(self, datums: VerticalDatums) -> Altitude:
        """Conversion to the absolute frame; the common first step of any test."""
        return self.as_reference(AltitudeReference.AMSL, datums)

    def separation_from(self, other: Altitude, datums: VerticalDatums) -> Millimetre:
        """Signed height difference ``self - other`` in a single absolute frame.

        Comparing an AGL and a PAD height without converting first is the bug
        this method exists to prevent.
        """
        left = self.as_amsl(datums).value_mm
        right = other.as_amsl(datums).value_mm
        return Millimetre(left - right)

    def key(self) -> str:
        """Canonical, round-trippable string form."""
        return f"{ALTITUDE_KEY_PREFIX}{_KEY_SEPARATOR}{self.reference.value}{_KEY_SEPARATOR}{self.value_mm}"

    @classmethod
    def from_key(cls, key: str) -> Altitude:
        """Inverse of :meth:`key`.

        Raises:
            AltitudeReferenceError: not three fields, unknown prefix, non-integer
                height, or a reference that cannot carry a value.
        """
        parts = key.split(_KEY_SEPARATOR)
        if len(parts) != 3 or parts[0] != ALTITUDE_KEY_PREFIX:
            raise AltitudeReferenceError(f"not a canonical altitude key: {key!r}")
        try:
            value = int(parts[2])
        except ValueError as error:
            raise AltitudeReferenceError(f"altitude value is not an integer: {key!r}") from error
        return cls(reference_from_token(parts[1]), Millimetre(value))

    def __str__(self) -> str:
        return self.key()


@dataclass(frozen=True)
class AltitudeBand:
    """Half-open vertical range ``[lower_mm, upper_mm)`` with a required reference.

    Half-open for the same reason intervals are: two bands that share a
    boundary do not overlap, so stacking bands to cover a corridor adds up
    exactly with no double-counted ceiling.
    """

    reference: AltitudeReference
    lower_mm: Millimetre
    upper_mm: Millimetre

    def __post_init__(self) -> None:
        _require_constructible(self.reference)
        lower = as_millimetres(self.lower_mm)
        upper = as_millimetres(self.upper_mm)
        if upper < lower:
            raise AltitudeReferenceError(
                f"altitude band is reversed: upper {upper} mm below lower {lower} mm"
            )

    @classmethod
    def from_metres(
        cls, lower_m: float, upper_m: float, reference: AltitudeReference
    ) -> AltitudeBand:
        """Boundary constructor from metres."""
        return cls(reference, metres_to_millimetres(lower_m), metres_to_millimetres(upper_m))

    @classmethod
    def from_altitudes(cls, lower: Altitude, upper: Altitude) -> AltitudeBand:
        """Build a band from two altitudes that must already share one reference.

        Mixing references here would silently invent a datum difference.
        """
        if lower.reference is not upper.reference:
            raise AltitudeReferenceError(
                f"band endpoints must share a reference, got {lower.reference.value} and "
                f"{upper.reference.value}"
            )
        return cls(lower.reference, lower.value_mm, upper.value_mm)

    @classmethod
    def closed_at_origin(cls, reference: AltitudeReference, height_mm: int) -> AltitudeBand:
        """The band ``[0, height)`` in ``reference`` - the usual corridor ceiling."""
        return cls(reference, Millimetre(0), as_millimetres(height_mm))

    @property
    def measure(self) -> Millimetre:
        return Millimetre(self.upper_mm - self.lower_mm)

    @property
    def is_empty(self) -> bool:
        return self.lower_mm == self.upper_mm

    def contains_altitude(self, altitude: Altitude, datums: VerticalDatums) -> bool:
        """True when ``altitude`` lies inside the band, after reference conversion."""
        converted = altitude.as_reference(self.reference, datums)
        return self.lower_mm <= converted.value_mm < self.upper_mm

    def contains(self, other: AltitudeBand) -> bool:
        if other.reference is not self.reference:
            raise AltitudeReferenceError("band containment requires one reference")
        return self.lower_mm <= other.lower_mm and other.upper_mm <= self.upper_mm

    def intersects(self, other: AltitudeBand) -> bool:
        if other.reference is not self.reference:
            raise AltitudeReferenceError("band intersection requires one reference")
        if self.is_empty or other.is_empty:
            return False
        return self.lower_mm < other.upper_mm and other.lower_mm < self.upper_mm

    def intersection(self, other: AltitudeBand) -> tuple[AltitudeBand, ...]:
        if not self.intersects(other):
            return ()
        return (
            AltitudeBand(
                self.reference,
                Millimetre(max(self.lower_mm, other.lower_mm)),
                Millimetre(min(self.upper_mm, other.upper_mm)),
            ),
        )

    def union(self, other: AltitudeBand) -> tuple[AltitudeBand, ...]:
        return merge_bands((self, other))

    def difference(self, other: AltitudeBand) -> tuple[AltitudeBand, ...]:
        return subtract_band(self, (other,))

    def subtract(self, others: Iterable[AltitudeBand]) -> tuple[AltitudeBand, ...]:
        return subtract_band(self, others)

    def shifted_by(self, delta_mm: int) -> AltitudeBand:
        """Translate the band. The reference does not change: a shifted AGL band
        is still measured from the terrain."""
        step = as_millimetres(delta_mm)
        return AltitudeBand(
            self.reference, Millimetre(self.lower_mm + step), Millimetre(self.upper_mm + step)
        )

    def grow_by(self, margin_mm: int) -> AltitudeBand:
        """Expand the band by ``margin_mm`` on each side (a vertical margin).

        A zero margin returns an equal band; a negative margin is a rejection,
        because "shrink the protected volume by 10 m" is how an unsafe corridor
        gets created by an arithmetic sign error.
        """
        margin = as_millimetres(margin_mm)
        if margin < 0:
            raise AltitudeReferenceError(f"margin must be non-negative, got {margin} mm")
        return AltitudeBand(
            self.reference,
            Millimetre(self.lower_mm - margin),
            Millimetre(self.upper_mm + margin),
        )

    def clamp_to(self, other: AltitudeBand) -> AltitudeBand:
        """Clip into ``other``, or become empty at ``other``'s lower edge."""
        if other.reference is not self.reference:
            raise AltitudeReferenceError("band clamping requires one reference")
        lower = max(self.lower_mm, other.lower_mm)
        upper = min(self.upper_mm, other.upper_mm)
        if lower > upper:
            return AltitudeBand(self.reference, other.lower_mm, other.lower_mm)
        return AltitudeBand(self.reference, Millimetre(lower), Millimetre(upper))

    def as_reference(self, reference: AltitudeReference, datums: VerticalDatums) -> AltitudeBand:
        """Convert both edges, preserving the reference on the result.

        A band is a *vertical range at one place*. Converting an AGL band whose
        terrain varies along the corridor is only valid for the datum supplied:
        pass the lowest terrain for the floor and the highest for the ceiling,
        or split the band. This function will not average them for you.
        """
        lower = Altitude(self.reference, self.lower_mm).as_reference(reference, datums)
        upper = Altitude(self.reference, self.upper_mm).as_reference(reference, datums)
        return AltitudeBand(reference, lower.value_mm, upper.value_mm)

    def band_index(self, vertical_band_mm: int) -> int:
        """Ordinal index of the band's floor in a uniform vertical lattice.

        Floor division, so a band below the datum gets a negative index instead
        of being folded into index 0.
        """
        return floor_div(self.lower_mm, as_millimetres(vertical_band_mm))

    @classmethod
    def covering_index(
        cls, reference: AltitudeReference, band_index: int, vertical_band_mm: int
    ) -> AltitudeBand:
        """The uniform-lattice band with the given ordinal index."""
        size = as_millimetres(vertical_band_mm)
        lower = band_index * size
        return cls(reference, Millimetre(lower), Millimetre(lower + size))

    def sort_key(self) -> tuple[str, int, int]:
        """Ascending ``(reference name, lower, upper)``."""
        return (self.reference.value, self.lower_mm, self.upper_mm)

    def key(self) -> str:
        """Canonical, round-trippable string form."""
        return (
            f"{BAND_KEY_PREFIX}{_KEY_SEPARATOR}{self.reference.value}"
            f"{_KEY_SEPARATOR}{self.lower_mm}{_KEY_SEPARATOR}{self.upper_mm}"
        )

    @classmethod
    def from_key(cls, key: str) -> AltitudeBand:
        parts = key.split(_KEY_SEPARATOR)
        if len(parts) != 4 or parts[0] != BAND_KEY_PREFIX:
            raise AltitudeReferenceError(f"not a canonical altitude band key: {key!r}")
        try:
            lower = int(parts[2])
            upper = int(parts[3])
        except ValueError as error:
            raise AltitudeReferenceError(f"band edges are not integers: {key!r}") from error
        return cls(reference_from_token(parts[1]), Millimetre(lower), Millimetre(upper))

    def __str__(self) -> str:
        return self.key()


def merge_bands(bands: Iterable[AltitudeBand]) -> tuple[AltitudeBand, ...]:
    """Canonical ascending cover.

    Raises:
        AltitudeReferenceError: the input mixes references. Merging an AGL band
            with an AMSL band would produce a band that is neither.
    """
    ordered = sorted(bands, key=AltitudeBand.sort_key)
    if not ordered:
        return ()
    reference = ordered[0].reference
    for band in ordered:
        if band.reference is not reference:
            raise AltitudeReferenceError(
                f"merge_bands is per-reference; {reference.value} and {band.reference.value} "
                "must be converted into one frame first"
            )
    merged: list[AltitudeBand] = []
    for band in ordered:
        if band.is_empty:
            continue
        if merged and band.lower_mm <= merged[-1].upper_mm:
            previous = merged[-1]
            if band.upper_mm > previous.upper_mm:
                merged[-1] = AltitudeBand(reference, previous.lower_mm, band.upper_mm)
            continue
        merged.append(band)
    return tuple(merged)


def subtract_band(base: AltitudeBand, others: Iterable[AltitudeBand]) -> tuple[AltitudeBand, ...]:
    """``base`` minus every band in ``others``, ascending and canonical."""
    if base.is_empty:
        return ()
    cleaned = merge_bands(others)
    if cleaned and cleaned[0].reference is not base.reference:
        raise AltitudeReferenceError("band subtraction requires one reference")
    cursor = base.lower_mm
    pieces: list[AltitudeBand] = []
    for other in cleaned:
        if other.upper_mm <= base.lower_mm:
            continue
        if other.lower_mm >= base.upper_mm:
            break
        if other.lower_mm > cursor:
            pieces.append(AltitudeBand(base.reference, cursor, other.lower_mm))
        cursor = other.upper_mm
        if cursor >= base.upper_mm:
            return tuple(pieces)
    if cursor < base.upper_mm:
        pieces.append(AltitudeBand(base.reference, cursor, base.upper_mm))
    return tuple(pieces)


def band_sequence_is_disjoint(bands: Sequence[AltitudeBand]) -> bool:
    """Self-check used by tests and by a planner's post-condition assertion."""
    merged = merge_bands(bands)
    return sum((band.measure for band in merged), 0) == sum(
        (band.measure for band in bands if not band.is_empty), 0
    )


__all__ = [
    "ALTITUDE_KEY_PREFIX",
    "ALTITUDE_REFERENCE_ORDER",
    "BAND_KEY_PREFIX",
    "Altitude",
    "AltitudeBand",
    "AltitudeReference",
    "AltitudeReferenceError",
    "Millimetre",
    "VerticalDatums",
    "band_sequence_is_disjoint",
    "merge_bands",
    "reference_from_token",
    "subtract_band",
]
