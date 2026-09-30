"""Projected-grid cells, hierarchical 3D volume keys and altitude bands.

Coordinate reference system assumption, stated once
    Cell indices are **not** geographic. They are indices into a metric grid
    defined in a projected CRS, and geom-core records that CRS instead of
    implying it. The default assumption for the Chandigarh synthetic world is
    ``EPSG:32643`` (WGS 84 / UTM zone 43N, metres), which covers 72E-78E and
    therefore all of the Tri-city area. A cell key carries the EPSG code, so a
    cell from zone 43N can never be compared with, or merged into, a cell from
    zone 44N: the parse fails loudly.

    Longitude/latitude never enter an index. They are converted exactly once,
    at the :mod:`~droneops_geom_core.geo` boundary, into integer millimetres.

Why a hierarchical key at all
    ``volume:<geohash3d-cell>:<time-window>`` is the reservation the ledger
    enforces with an exclusion constraint. That needs a *short, stable, string*
    key whose prefixes are meaningful: truncating the horizontal part of a key
    gives the coarser cell that contains it, so a query at a coarse level is a
    conservative superset of the same query at a fine level. That is exactly the
    pre-filter a planner runs before it does real interval intersection.

Layout and bit budget
    Horizontal indices are zig-zag encoded (so negative cells near the origin
    stay small), interleaved x-then-y, and rendered in the geohash base-32
    alphabet, most significant bits first. ``horizontal_bits`` per axis must be
    a multiple of five so that every key character is a whole number of bits and
    truncation is exact. Vertical band indices are zig-zag encoded on their own
    after a ``.`` separator. The vertical axis is *ordinal, not hierarchical*:
    a band index is only meaningful together with its :class:`AltitudeReference`.

Ordering
    Every sequence returned here is ascending in ``(band_index, y, x)`` (cells)
    or ``(y, x)`` (2D). No function iterates a set or a dict to produce output.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from .altitude import AltitudeBand, AltitudeReference, reference_from_token
from .errors import CellDomainError
from .units import Millimetre, as_millimetres, floor_div, zigzag_decode, zigzag_encode

BASE32_ALPHABET: Final[str] = "0123456789bcdefghjkmnpqrstuvwxyz"
"""Classic geohash alphabet: no ``a``, ``i``, ``l`` or ``o`` to confuse by voice."""

_BITS_PER_BASE32_CHAR: Final[int] = 5

CHANDIGARH_UTM_EPSG: Final[int] = 32643
"""WGS 84 / UTM zone 43N. The documented CRS assumption for the synthetic world."""

CELL_2D_KEY_PREFIX: Final[str] = "2d"
CELL_3D_KEY_PREFIX: Final[str] = "3d"
_KEY_SEPARATOR: Final[str] = "|"
_TOKEN_SEPARATOR: Final[str] = "."

_DEFAULT_HORIZONTAL_BITS: Final[int] = 15
_DEFAULT_VERTICAL_BITS: Final[int] = 10


def _to_base32(value: int, width: int) -> str:
    if value < 0:
        raise CellDomainError(f"base32 encoding needs a non-negative value, got {value}")
    if value >= 32**width:
        raise CellDomainError(f"value {value} does not fit in {width} base32 characters")
    chars: list[str] = []
    for index in reversed(range(width)):
        chars.append(BASE32_ALPHABET[(value >> (5 * index)) & 31])
    return "".join(chars)


def _from_base32(token: str) -> int:
    value = 0
    for char in token:
        index = BASE32_ALPHABET.find(char)
        if index < 0:
            raise CellDomainError(f"{char!r} is not in the geohash base32 alphabet")
        value = (value << 5) | index
    return value


def _checked_reference(reference: AltitudeReference) -> AltitudeReference:
    """Reject anything that is not a canonical, usable enum member.

    ``isinstance`` is deliberately not used: under ``StrEnum`` a bare string
    compares equal to a member, so only identity plus the canonical token tells a
    real reference from a look-alike.
    """
    if reference_from_token(str(reference)) is not reference:
        raise CellDomainError(f"volume cell needs an AltitudeReference member, got {reference!r}")
    if reference is AltitudeReference.UNSPECIFIED:
        raise CellDomainError("volume cell altitude reference is UNSPECIFIED (I9)")
    return reference


def _bias(bits: int) -> int:
    return 1 << (bits - 1)


def _to_unsigned(value: int, bits: int) -> int:
    """Offset a signed index by ``2 ** (bits - 1)``: two's-complement order.

    This is why the horizontal axis is *biased* rather than zig-zag encoded. A
    zig-zag code is not prefix-compatible: dropping its low bits does not equal
    aligning the original index down to a block, so a truncated key would name a
    different cell than the one containing it, and the hierarchy would be a lie
    for every negative index. A bias is divisible by every block size the level
    scheme can produce, so ``floor(x / 2**j) * 2**j`` and ``zero_low_bits(u)``
    agree everywhere, including west and south of the grid origin.
    """
    limit = _bias(bits)
    if not -limit <= value < limit:
        raise CellDomainError(f"index {value} is outside the +-{limit} domain of a {bits}-bit axis")
    return value + limit


def _from_unsigned(code: int, bits: int) -> int:
    return code - _bias(bits)


def _interleave(x: int, y: int, bits: int) -> int:
    """Morton interleave: x occupies odd bit positions, y the even ones."""
    code = 0
    for index in range(bits):
        code |= ((x >> index) & 1) << (2 * index + 1)
        code |= ((y >> index) & 1) << (2 * index)
    return code


def _deinterleave(code: int, bits: int) -> tuple[int, int]:
    x = 0
    y = 0
    for index in range(bits):
        x |= ((code >> (2 * index + 1)) & 1) << index
        y |= ((code >> (2 * index)) & 1) << index
    return x, y


@dataclass(frozen=True)
class GridSpec:
    """A projected grid: which CRS, how wide a cell, how tall a vertical band."""

    epsg: int
    cell_size_mm: Millimetre
    vertical_band_mm: Millimetre
    horizontal_bits: int = _DEFAULT_HORIZONTAL_BITS
    vertical_bits: int = _DEFAULT_VERTICAL_BITS

    def __post_init__(self) -> None:
        if self.epsg <= 0:
            raise CellDomainError(f"EPSG code must be positive, got {self.epsg}")
        if self.cell_size_mm <= 0:
            raise CellDomainError(f"cell_size_mm must be positive, got {self.cell_size_mm}")
        if self.vertical_band_mm <= 0:
            raise CellDomainError(
                f"vertical_band_mm must be positive, got {self.vertical_band_mm}"
            )
        if self.horizontal_bits % _BITS_PER_BASE32_CHAR != 0:
            raise CellDomainError(
                f"horizontal_bits must be a multiple of {_BITS_PER_BASE32_CHAR} so key "
                f"truncation is exact; got {self.horizontal_bits}"
            )
        if self.vertical_bits % _BITS_PER_BASE32_CHAR != 0:
            raise CellDomainError(
                f"vertical_bits must be a multiple of {_BITS_PER_BASE32_CHAR}; got {self.vertical_bits}"
            )

    @property
    def horizontal_code_bits(self) -> int:
        """Total interleaved bits, and therefore the width of the horizontal token."""
        return 2 * self.horizontal_bits

    @property
    def max_level(self) -> int:
        return self.horizontal_code_bits // _BITS_PER_BASE32_CHAR

    @property
    def vertical_width(self) -> int:
        return self.vertical_bits // _BITS_PER_BASE32_CHAR

    @property
    def index_limit(self) -> int:
        """Largest absolute cell index representable: ``2 ** (bits - 1)``."""
        return 1 << (self.horizontal_bits - 1)

    @property
    def band_index_limit(self) -> int:
        """Largest absolute vertical band index representable."""
        return 1 << (self.vertical_bits - 1)

    def dropped_bits(self, level: int) -> int:
        """Interleaved bits discarded at ``level``; zero at maximum precision."""
        self._require_level(level)
        return _BITS_PER_BASE32_CHAR * (self.max_level - level)

    def block_extent(self, level: int) -> tuple[int, int]:
        """Fine cells covered by one cell at ``level``, as ``(x_count, y_count)``.

        The counts differ whenever an odd number of interleaved bits was
        truncated, which is exactly how standard geohash precision alternates
        between axes. ``x`` lives in the odd bit positions and ``y`` in the even
        ones, so of ``d`` dropped bits ``y`` loses ``ceil(d / 2)`` and ``x`` loses
        ``floor(d / 2)`` - the first character dropped belongs to ``y``.
        """
        dropped = self.dropped_bits(level)
        return (2 ** (dropped // 2), 2 ** ((dropped + 1) // 2))

    def _require_level(self, level: int) -> None:
        if not 1 <= level <= self.max_level:
            raise CellDomainError(
                f"level {level} is outside 1..{self.max_level} for this grid specification"
            )

    def _require_domain(self, x: int, y: int) -> None:
        """Reject a cell index outside the axis bit budget.

        Stating the limit is the point: without it a far-out index would encode
        into a longer token and the hierarchy would silently stop being a prefix
        relation.
        """
        _to_unsigned(x, self.horizontal_bits)
        _to_unsigned(y, self.horizontal_bits)

    def key_fields(self) -> tuple[str, ...]:
        """The five canonical specification fields."""
        return (
            str(self.epsg),
            str(self.cell_size_mm),
            str(self.vertical_band_mm),
            str(self.horizontal_bits),
            str(self.vertical_bits),
        )

    @classmethod
    def from_key_fields(cls, fields: Sequence[str]) -> GridSpec:
        """Inverse of :meth:`key_fields`."""
        if len(fields) != 5:
            raise CellDomainError(f"a grid specification has five fields, got {len(fields)}")
        try:
            return cls(
                int(fields[0]),
                Millimetre(int(fields[1])),
                Millimetre(int(fields[2])),
                int(fields[3]),
                int(fields[4]),
            )
        except ValueError as error:
            raise CellDomainError(f"grid specification fields are not integers: {fields}") from error

    def cell_of(self, east_mm: int, north_mm: int) -> Cell2D:
        """The fine cell containing a projected offset. Floor division, always."""
        x = floor_div(as_millimetres(east_mm), self.cell_size_mm)
        y = floor_div(as_millimetres(north_mm), self.cell_size_mm)
        return Cell2D(self, x, y)

    def volume_cell_of(
        self,
        east_mm: int,
        north_mm: int,
        altitude_mm: int,
        *,
        level: int,
        reference: AltitudeReference,
    ) -> Cell3D:
        """The 3D volume cell containing a projected point and an altitude.

        The horizontal index is aligned down to the block that contains it at
        ``level``; the vertical band index always uses the fine vertical lattice,
        because a coarser vertical key would silently merge bands a separation
        policy cares about.
        """
        cell = self.cell_of(east_mm, north_mm)
        extent_x, extent_y = self.block_extent(level)
        band_index = floor_div(as_millimetres(altitude_mm), self.vertical_band_mm)
        return Cell3D(
            self,
            level,
            floor_div(cell.x, extent_x) * extent_x,
            floor_div(cell.y, extent_y) * extent_y,
            band_index,
            reference,
        )

    def key(self) -> str:
        """Canonical specification key, round-trippable through :meth:`from_key`."""
        return _KEY_SEPARATOR.join(("grid", *self.key_fields()))

    @classmethod
    def from_key(cls, key: str) -> GridSpec:
        parts = key.split(_KEY_SEPARATOR)
        if len(parts) != 6 or parts[0] != "grid":
            raise CellDomainError(f"not a canonical grid specification key: {key!r}")
        return cls.from_key_fields(parts[1:])


@dataclass(frozen=True)
class Cell2D:
    """A cell of the fine horizontal lattice. Order: ``(y, x)`` ascending."""

    spec: GridSpec
    x: int
    y: int

    def __post_init__(self) -> None:
        self.spec._require_domain(self.x, self.y)

    def offset_of_origin(self) -> tuple[Millimetre, Millimetre]:
        """Projected east/north in millimetres of the cell's south-west corner."""
        return (
            Millimetre(self.x * self.spec.cell_size_mm),
            Millimetre(self.y * self.spec.cell_size_mm),
        )

    def neighbours(self) -> tuple[Cell2D, ...]:
        """The eight surrounding cells plus self, ascending ``(y, x)``."""
        return self.neighbourhood(radius=1)

    def neighbourhood(self, *, radius: int = 1) -> tuple[Cell2D, ...]:
        """Cells within Chebyshev ``radius``, including self, ascending ``(y, x)``.

        The offset list is a module constant, so the enumeration order does not
        depend on set or dict iteration. Membership is symmetric by
        construction: ``b`` appears in ``a.neighbourhood(r)`` if and only if
        ``a`` appears in ``b.neighbourhood(r)``.
        """
        if radius < 0:
            raise CellDomainError(f"radius must be non-negative, got {radius}")
        offsets = [
            (dx, dy)
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)
        ]
        return tuple(
            sorted(
                (Cell2D(self.spec, self.x + dx, self.y + dy) for dx, dy in offsets),
                key=Cell2D.sort_key,
            )
        )

    def sort_key(self) -> tuple[int, int, int]:
        return (self.spec.epsg, self.y, self.x)

    def key(self) -> str:
        """Canonical verbose key: full specification plus indices, so it stands alone."""
        return _KEY_SEPARATOR.join((CELL_2D_KEY_PREFIX, *self.spec.key_fields(), str(self.x), str(self.y)))

    @classmethod
    def from_key(cls, key: str) -> Cell2D:
        parts = key.split(_KEY_SEPARATOR)
        if len(parts) != 8 or parts[0] != CELL_2D_KEY_PREFIX:
            raise CellDomainError(
                f"not a canonical 2D cell key (expected '<2d|epsg|cell|band|hb|vb|x|y>'): {key!r}"
            )
        spec = GridSpec.from_key_fields(parts[1:6])
        try:
            return cls(spec, int(parts[6]), int(parts[7]))
        except ValueError as error:
            raise CellDomainError(f"2D cell indices are not integers: {key!r}") from error

    def at_level(self, level: int, band_index: int, reference: AltitudeReference) -> Cell3D:
        """Wrap into the 3D hierarchical cell containing this one at ``level``."""
        extent_x, extent_y = self.spec.block_extent(level)
        return Cell3D(
            self.spec,
            level,
            floor_div(self.x, extent_x) * extent_x,
            floor_div(self.y, extent_y) * extent_y,
            band_index,
            reference,
        )

    def __str__(self) -> str:
        return self.key()


@dataclass(frozen=True)
class Cell3D:
    """A hierarchical volume cell: horizontal block, vertical band, reference.

    ``x`` and ``y`` are aligned to the block size of ``level``, which is checked
    on construction: an unaligned index would round-trip to a *different* key,
    and a key that is not its own canonical form is how two reservations end up
    in two rows for the same volume.
    """

    spec: GridSpec
    level: int
    x: int
    y: int
    band_index: int
    reference: AltitudeReference

    def __post_init__(self) -> None:
        self.spec._require_level(self.level)
        self.spec._require_domain(self.x, self.y)
        _checked_reference(self.reference)
        limit = self.spec.band_index_limit
        if not -limit <= self.band_index < limit:
            raise CellDomainError(
                f"band index {self.band_index} is outside the +-{limit} domain of a "
                f"{self.spec.vertical_bits}-bit vertical axis"
            )
        extent_x, extent_y = self.spec.block_extent(self.level)
        if self.x % extent_x or self.y % extent_y:
            raise CellDomainError(
                f"cell ({self.x}, {self.y}) is not aligned to the block size "
                f"({extent_x}, {extent_y}) of level {self.level}; align it with "
                "GridSpec.volume_cell_of or Cell3D.covering"
            )

    @classmethod
    def covering(
        cls,
        spec: GridSpec,
        x: int,
        y: int,
        band_index: int,
        *,
        level: int,
        reference: AltitudeReference,
    ) -> Cell3D:
        """The level-``level`` block containing the fine cell ``(x, y)``."""
        extent_x, extent_y = spec.block_extent(level)
        return cls(
            spec,
            level,
            floor_div(x, extent_x) * extent_x,
            floor_div(y, extent_y) * extent_y,
            band_index,
            reference,
        )

    @property
    def block_extent(self) -> tuple[int, int]:
        return self.spec.block_extent(self.level)

    @property
    def edge_length_mm(self) -> Millimetre:
        """Horizontal edge length of this block in millimetres.

        The block is not square at every level: base32 truncation alternates
        which axis loses bits, so :meth:`block_extent` gives the exact counts and
        this property reports the x extent, which is the number a pre-filter
        radius needs.
        """
        return Millimetre(self.spec.cell_size_mm * self.block_extent[0])

    def covers_fine_cell(self, x: int, y: int) -> bool:
        """Conservative containment test used by the pre-filter."""
        extent_x, extent_y = self.block_extent
        return (
            floor_div(x, extent_x) * extent_x == self.x
            and floor_div(y, extent_y) * extent_y == self.y
        )

    def covers_cell(self, other: Cell3D) -> bool:
        """True when ``other`` (at any level) falls inside this block."""
        if other.spec != self.spec or other.band_index != self.band_index:
            return False
        if other.level < self.level:
            return False
        return self.covers_fine_cell(other.x, other.y)

    def parent(self) -> Cell3D:
        """The containing block one coarser level; raises at the coarsest level."""
        if self.level <= 1:
            raise CellDomainError("level 1 is the coarsest block; it has no parent")
        return Cell3D.covering(
            self.spec,
            self.x,
            self.y,
            self.band_index,
            level=self.level - 1,
            reference=self.reference,
        )

    def band(self) -> AltitudeBand:
        """The altitude band this volume cell represents, reference included."""
        return AltitudeBand.covering_index(
            self.reference, self.band_index, self.spec.vertical_band_mm
        )

    def neighbourhood(self, *, radius: int = 1) -> tuple[Cell3D, ...]:
        """Same-level cells within Chebyshev ``radius``, self included.

        Order is ascending ``(band_index, y, x)``. Vertical neighbours are not
        enumerated: a volume cell's band is part of its identity, and a planner
        that wants the band above asks for it explicitly.
        """
        if radius < 0:
            raise CellDomainError(f"radius must be non-negative, got {radius}")
        extent_x, extent_y = self.block_extent
        offsets = [
            (dx, dy)
            for dy in range(-radius, radius + 1)
            for dx in range(-radius, radius + 1)
        ]
        return tuple(
            sorted(
                (
                    Cell3D(
                        self.spec, self.level, self.x + dx * extent_x, self.y + dy * extent_y,
                        self.band_index, self.reference,
                    )
                    for dx, dy in offsets
                ),
                key=Cell3D.sort_key,
            )
        )

    def ring(self, *, radius: int = 1) -> tuple[Cell3D, ...]:
        """Cells at Chebyshev distance exactly ``radius``, excluding self."""
        if radius < 1:
            raise CellDomainError(f"ring radius must be at least 1, got {radius}")
        return tuple(cell for cell in self.neighbourhood(radius=radius) if cell != self)

    def token(self) -> str:
        """The compact ``geohash3d`` token used inside a resource key.

        ``<horizontal base32 of length level>.<vertical base32>``. Truncating the
        horizontal part yields the containing coarser cell.
        """
        code = self._interleaved_code()
        full = _to_base32(code, self.spec.max_level)
        return f"{full[: self.level]}{_TOKEN_SEPARATOR}{self._vertical_token()}"

    def _interleaved_code(self) -> int:
        return _interleave(
            _to_unsigned(self.x, self.spec.horizontal_bits),
            _to_unsigned(self.y, self.spec.horizontal_bits),
            self.spec.horizontal_bits,
        )

    def _vertical_token(self) -> str:
        """Band index in zig-zag order: the datum band is ``00``, not ``h0``.

        The vertical axis is ordinal, not hierarchical, so the prefix objection
        that rules zig-zag out for ``x`` and ``y`` does not apply here.
        """
        return _to_base32(zigzag_encode(self.band_index), self.spec.vertical_width)

    @classmethod
    def parse_token(cls, spec: GridSpec, token: str, *, reference: AltitudeReference) -> Cell3D:
        """Inverse of :meth:`token`.

        The reference is an argument, not a default. A geohash3d token carries a
        vertical *ordinal* only; the frame it is measured in belongs to the plan
        and the world snapshot. Inventing ``AMSL`` here would be exactly the
        guess invariant I9 forbids.

        Raises:
            CellDomainError: wrong shape, characters outside the base32
                alphabet, a horizontal part longer than the grid allows, or a
                band index outside the vertical domain.
        """
        parts = token.split(_TOKEN_SEPARATOR)
        if len(parts) != 2:
            raise CellDomainError(f"geohash3d token {token!r} must be '<horizontal>.<vertical>'")
        horizontal, vertical = parts
        if not horizontal or len(horizontal) > spec.max_level:
            raise CellDomainError(
                f"horizontal token {horizontal!r} is empty or longer than "
                f"{spec.max_level} characters"
            )
        if len(vertical) != spec.vertical_width:
            raise CellDomainError(
                f"vertical token {vertical!r} must be exactly {spec.vertical_width} characters"
            )
        level = len(horizontal)
        code = _from_base32(horizontal) << spec.dropped_bits(level)
        unsigned_x, unsigned_y = _deinterleave(code, spec.horizontal_bits)
        band_index = zigzag_decode(_from_base32(vertical))
        return cls(
            spec,
            level,
            _from_unsigned(unsigned_x, spec.horizontal_bits),
            _from_unsigned(unsigned_y, spec.horizontal_bits),
            band_index,
            reference,
        )

    def key(self) -> str:
        """Canonical verbose key: full specification, level, reference and token."""
        return _KEY_SEPARATOR.join(
            (
                CELL_3D_KEY_PREFIX,
                *self.spec.key_fields(),
                str(self.level),
                self.reference.value,
                self.token(),
            )
        )

    @classmethod
    def from_key(cls, key: str) -> Cell3D:
        parts = key.split(_KEY_SEPARATOR)
        if len(parts) != 9 or parts[0] != CELL_3D_KEY_PREFIX:
            raise CellDomainError(
                f"not a canonical 3D volume cell key "
                f"(expected '<3d|epsg|cell|band|hb|vb|level|ref|token>'): {key!r}"
            )
        spec = GridSpec.from_key_fields(parts[1:6])
        try:
            level = int(parts[6])
        except ValueError as error:
            raise CellDomainError(f"volume cell level is not an integer: {key!r}") from error
        cell = cls.parse_token(spec, parts[8], reference=reference_from_token(parts[7]))
        if cell.level != level:
            raise CellDomainError(
                f"volume cell key states level {level} but its token carries level {cell.level}"
            )
        return cell

    def sort_key(self) -> tuple[int, int, int, int]:
        """Ascending ``(band_index, y, x, level)``."""
        return (self.band_index, self.y, self.x, self.level)

    def __str__(self) -> str:
        return self.token()


def cell_sequence_is_disjoint(cells: Iterable[Cell3D]) -> bool:
    """Self-check: no cell in the sequence properly contains another."""
    ordered = sorted(cells, key=Cell3D.sort_key)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if left != right and (left.covers_cell(right) or right.covers_cell(left)):
                return False
    return True


def merge_cells_by_token(cells: Iterable[Cell3D]) -> tuple[Cell3D, ...]:
    """Distinct volume cells, ascending by token - the set a reservation writes."""
    unique = {cell.token(): cell for cell in cells}
    return tuple(unique[token] for token in sorted(unique))
