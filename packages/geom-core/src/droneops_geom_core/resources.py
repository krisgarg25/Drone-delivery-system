"""Canonical resource keys and the single total order used for lock acquisition.

``technical-build/01`` section 6 fixes the stored forms:

.. code-block:: text

    corridor:<segment-id>
    volume:<geohash3d-cell>:<time-window>
    pad:<facility-id>:<slot-window>
    charger:<bay-id>:<slot-window>
    crew:<crew-id>:<duty-window>

``crossing`` is the sixth kind. Section 6 does not enumerate it, but section 8
requires merge and crossing volumes to be reserved, so it follows the same
three-field shape as the other time-bounded resources:
``crossing:<crossing-id>:<time-window>``.

The six kinds are addressed by the names the planning pipeline uses
(``volume_cell``, ``charger_bay``) while the *emitted* prefix is the section 6
token (``volume``, ``charger``), because the prefix is what PostgreSQL compares
in the reservation exclusion constraint.

Why one order and one producer
    conventions.md section 5 step 7 and ``technical-build/01`` section 9 require
    row locks in a canonical resource order. Two orderings are published here:

    * :meth:`ResourceKey.sort_key` - a typed tuple, used in Python.
    * :meth:`ResourceKey.lock_token` - a fixed-width string with the *same*
      ordering, so ``ORDER BY lock_token`` in SQL acquires locks in exactly the
      order :func:`sorted_resource_keys` produces. Deadlock avoidance only works
      if both sides agree.

Identifiers are restricted to ``[A-Za-z0-9._~-]{1,64}``. ``!`` is excluded from
that alphabet and used as the pad/separator character in ``lock_token``, which
is what makes fixed-width padding order-preserving.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from .errors import ResourceKeyError
from .intervals import Interval
from .timebase import Microsecond

_LOCK_PAD: Final[str] = "!"
_FIELD_SEPARATOR: Final[str] = ":"
_WINDOW_SEPARATOR: Final[str] = ".."
_IDENTIFIER_MAX_LENGTH: Final[int] = 64
_WINDOW_DIGITS: Final[int] = 20
_WINDOW_BIAS: Final[int] = 2**63
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~-]{1,64}")


class ResourceKind(Enum):
    """The six reservable resource kinds, with their section 6 key prefixes."""

    CORRIDOR = "corridor"
    VOLUME_CELL = "volume"
    CROSSING = "crossing"
    PAD = "pad"
    CHARGER_BAY = "charger"
    CREW = "crew"

    @property
    def prefix(self) -> str:
        """The token written into the canonical key."""
        return self.value

    @property
    def takes_window(self) -> bool:
        """``corridor`` is a bare segment; every other kind is time-bounded."""
        return self is not ResourceKind.CORRIDOR

    @property
    def lock_rank(self) -> int:
        """Position in the documented acquisition order, starting at zero."""
        return RESOURCE_KIND_ORDER.index(self)


RESOURCE_KIND_ORDER: Final[tuple[ResourceKind, ...]] = (
    ResourceKind.CORRIDOR,
    ResourceKind.VOLUME_CELL,
    ResourceKind.CROSSING,
    ResourceKind.PAD,
    ResourceKind.CHARGER_BAY,
    ResourceKind.CREW,
)
"""Lock acquisition order: continuous airspace first, then discrete volumes and
crossings, then ground facilities. Corridors are held longest and released last,
so acquiring them first is what keeps a stalled pad from holding a corridor."""

_KIND_BY_PREFIX: Final[dict[str, ResourceKind]] = {kind.prefix: kind for kind in RESOURCE_KIND_ORDER}
_KIND_BY_NAME: Final[dict[str, ResourceKind]] = {kind.name.lower(): kind for kind in RESOURCE_KIND_ORDER}


def kind_from_token(token: str) -> ResourceKind:
    """Resolve a kind from its canonical prefix or its enum name.

    Both are accepted because a plan record writes the prefix while a policy
    document names the kind; the *output* is always the prefix form.

    Raises:
        ResourceKeyError: unknown kind. There is no default kind.
    """
    try:
        return _KIND_BY_PREFIX[token]
    except KeyError:
        pass
    try:
        return _KIND_BY_NAME[token.lower()]
    except KeyError as error:
        raise ResourceKeyError(f"unknown resource kind {token!r}") from error


def validate_identifier(identifier: str) -> str:
    """Check an identifier against the canonical alphabet.

    Rejects empty, whitespace, ``:``, ``|`` and anything over 64 characters. A
    colon inside an identifier would make the key ambiguous under
    ``split(":")``, which is how two different resources end up sharing a row.

    Raises:
        ResourceKeyError: identifier is empty or illegal.
    """
    if not identifier:
        raise ResourceKeyError("resource identifier must not be empty")
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ResourceKeyError(
            f"resource identifier {identifier!r} must match [A-Za-z0-9._~-]{{1,{_IDENTIFIER_MAX_LENGTH}}} "
            "with no colon, space or pipe"
        )
    return identifier


def _window_token(start: int, end: int) -> str:
    return f"{start}{_WINDOW_SEPARATOR}{end}"


def _parse_window_token(token: str) -> Interval:
    parts = token.split(_WINDOW_SEPARATOR)
    if len(parts) != 2:
        raise ResourceKeyError(f"time window {token!r} is not '<start>..<end>'")
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError as error:
        raise ResourceKeyError(f"time window {token!r} is not integer microseconds") from error
    return Interval.of(start, end)


@dataclass(frozen=True)
class ResourceKey:
    """One canonical, validated resource key."""

    kind: ResourceKind
    identifier: str
    window: Interval | None

    def __post_init__(self) -> None:
        if self.kind.takes_window:
            if self.window is None:
                raise ResourceKeyError(
                    f"{self.kind.prefix} keys are time-bounded and need a window; "
                    "only corridor keys are bare"
                )
            if self.window.is_empty:
                raise ResourceKeyError(
                    f"{self.kind.prefix} window must claim at least one instant; "
                    "an empty reservation is not a resource claim"
                )
        elif self.window is not None:
            raise ResourceKeyError(
                "corridor keys carry no window; temporal occupancy of a corridor is "
                "expressed by its volume cells (section 6)"
            )
        validate_identifier(self.identifier)

    @property
    def canonical(self) -> str:
        """The exact string stored in the reservation ledger."""
        parts = [self.kind.prefix, self.identifier]
        if self.window is not None:
            parts.append(_window_token(self.window.start, self.window.end))
        return _FIELD_SEPARATOR.join(parts)

    @property
    def lock_rank(self) -> int:
        return self.kind.lock_rank

    def sort_key(self) -> tuple[int, str, Microsecond, Microsecond]:
        """Typed total order: kind rank, identifier, then window start and end."""
        start, end = (0, 0) if self.window is None else (self.window.start, self.window.end)
        return (self.kind.lock_rank, self.identifier, Microsecond(start), Microsecond(end))

    def lock_token(self) -> str:
        """Fixed-width string form of :meth:`sort_key`, code-point ordered.

        ``ORDER BY lock_token`` in SQL reproduces :func:`sorted_resource_keys`
        exactly. Every field is fixed width and ``!`` sorts below every
        character an identifier may contain, so padding preserves order rather
        than merely filling space.
        """
        start, end = (0, 0) if self.window is None else (self.window.start, self.window.end)
        padded = self.identifier.ljust(_IDENTIFIER_MAX_LENGTH, _LOCK_PAD)
        return (
            f"{self.kind.lock_rank:02d}{_LOCK_PAD}{padded}{_LOCK_PAD}"
            f"{start + _WINDOW_BIAS:0{_WINDOW_DIGITS}d}{_LOCK_PAD}{end + _WINDOW_BIAS:0{_WINDOW_DIGITS}d}"
        )

    def with_window(self, window: Interval) -> ResourceKey:
        """The same resource over a different window; corridors have no window."""
        if not self.kind.takes_window:
            raise ResourceKeyError("corridor keys carry no window")
        return ResourceKey(self.kind, self.identifier, window)

    def identity_token(self) -> str:
        """The key without its time window: ``<prefix>:<identifier>``.

        Useful for grouping the reservations that hold one physical object. It is
        deliberately a string and not a :class:`ResourceKey`, because for every
        kind except ``corridor`` that form is not a valid reservation key and
        must not be handed to the ledger.
        """
        return f"{self.kind.prefix}{_FIELD_SEPARATOR}{self.identifier}"

    @classmethod
    def parse(cls, canonical: str) -> ResourceKey:
        """Parse a stored key back into a typed key.

        Raises:
            ResourceKeyError: wrong field count, unknown prefix, illegal
                identifier, unparseable window, or a key that is not canonical
                (for example one whose window endpoints are reversed, which
                ``Interval`` would reject on the way in).
        """
        if not canonical:
            raise ResourceKeyError("resource key must not be empty")
        parts = canonical.split(_FIELD_SEPARATOR)
        if len(parts) not in (2, 3):
            raise ResourceKeyError(f"resource key {canonical!r} must have 2 or 3 colon-separated fields")
        kind = kind_from_token(parts[0])
        identifier = validate_identifier(parts[1])
        if kind.takes_window:
            if len(parts) != 3:
                raise ResourceKeyError(f"{kind.prefix} key {canonical!r} is missing its time window")
            return cls(kind, identifier, _parse_window_token(parts[2]))
        if len(parts) != 2:
            raise ResourceKeyError(f"{kind.prefix} key {canonical!r} must not carry a time window")
        return cls(kind, identifier, None)

    def _comparison_token(self, other: object) -> str:
        if not isinstance(other, ResourceKey):
            raise TypeError(f"cannot order a ResourceKey against {type(other).__name__}")
        return other.lock_token()

    def __lt__(self, other: object) -> bool:
        return self.lock_token() < self._comparison_token(other)

    def __le__(self, other: object) -> bool:
        return self.lock_token() <= self._comparison_token(other)

    def __gt__(self, other: object) -> bool:
        return self.lock_token() > self._comparison_token(other)

    def __ge__(self, other: object) -> bool:
        return self.lock_token() >= self._comparison_token(other)

    def __str__(self) -> str:
        return self.canonical


def resource_key(
    kind: ResourceKind | str, identifier: str, window: Interval | None = None
) -> ResourceKey:
    """The only function that produces a resource key.

    Accepts a :class:`ResourceKind` or its canonical prefix / enum name, so a
    caller reading a stored record and a caller writing a new one go through the
    same validation and the same canonicalisation.

    Raises:
        ResourceKeyError: unknown kind, empty or illegal identifier, a
            time-bounded kind without a window, or a corridor with one.
    """
    resolved = kind if isinstance(kind, ResourceKind) else kind_from_token(kind)
    return ResourceKey(resolved, identifier, window)


def sorted_resource_keys(keys: Iterable[ResourceKey]) -> tuple[ResourceKey, ...]:
    """Keys in canonical lock-acquisition order, duplicates removed.

    Deduplication is part of the contract: acquiring the same row lock twice in
    one transaction is a bug in the caller, and the ledger must not see it. The
    result is deterministic regardless of input order or of any set iteration
    that produced the input.
    """
    unique: dict[tuple[int, str, int, int], ResourceKey] = {}
    for key in keys:
        rank, identifier, start, end = key.sort_key()
        unique.setdefault((rank, identifier, start, end), key)
    return tuple(sorted(unique.values(), key=ResourceKey.lock_token))


def lock_order(keys: Iterable[ResourceKey]) -> tuple[str, ...]:
    """The canonical strings in acquisition order - what the transaction locks."""
    return tuple(key.canonical for key in sorted_resource_keys(keys))
