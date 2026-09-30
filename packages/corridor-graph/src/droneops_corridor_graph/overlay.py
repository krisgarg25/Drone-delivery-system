"""Closure and restriction overlays: a lane's rules as a function of time.

An overlay never rewrites the graph. It answers two questions the planner needs
separately:

* *can this lane be entered at all in this window* - a closure that spans the whole
  plan window removes candidates outright;
* *which sub-windows of the plan is it usable in* - a restriction that **expires
  mid-plan** must stay representable, because a lane closed from minute 30 to
  minute 38 of a 60 minute plan is usable twice, before and after, and a planner
  that only knows "closed / open" has to reject a feasible trip.

Windows are half-open ``[start, end)`` in kernel microseconds, matching
``geom-core`` interval algebra, so a closure that ends exactly when a plan starts
does not block it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from droneops_geom_core import Interval, gaps_within

from .errors import ClosureOverlayError
from .model import canonical_digest, minutes_to_us


class RestrictionKind(StrEnum):
    """How a restriction binds a lane."""

    CLOSED = "closed"
    SPEED_CAP = "speed_cap"
    WIND_CAP = "wind_cap"
    BAND_CAP = "band_cap"


@dataclass(frozen=True)
class Restriction:
    """One time-bounded binding on one lane, with the reason it exists.

    ``human_reason`` and ``technical_detail`` are both mandatory: a rejection that
    can only say ``CLOSED`` cannot be explained to an operator, and a rejection that
    can only say "crane survey" cannot be audited against a bundle version.
    """

    restriction_id: str
    edge_id: str
    kind: RestrictionKind
    start_us: int
    end_us: int
    human_reason: str
    technical_detail: str
    value_mm: int | None = None
    source: str = "bundle"

    def __post_init__(self) -> None:
        if self.end_us <= self.start_us:
            raise ClosureOverlayError(
                f"restriction {self.restriction_id} ends at {self.end_us} which is not "
                f"after its start {self.start_us}"
            )
        if not self.human_reason:
            raise ClosureOverlayError(f"restriction {self.restriction_id} has no human reason")

    @property
    def window(self) -> Interval:
        return Interval.of(self.start_us, self.end_us)

    def blocks_window(self, window: Interval) -> bool:
        """True when this closure covers the whole of ``window``."""
        return self.kind is RestrictionKind.CLOSED and self.window.contains(window)

    def overlaps(self, window: Interval) -> bool:
        return self.window.overlaps(window)

    def expires_within(self, window: Interval) -> bool:
        """True when the binding lifts inside ``window``: a mid-plan expiry.

        The window it produces is what a static graph cannot express - a lane that
        is closed at minute 30 and open again at 38 of a 60 minute plan.
        """
        return self.window.overlaps(window) and window.contains_instant(self.end_us)

    def sort_key(self) -> tuple[str, int, int, str]:
        return (self.edge_id, self.start_us, self.end_us, self.restriction_id)


@dataclass(frozen=True)
class ClosureOverlay:
    """All restrictions for one graph, indexed by lane, time-ordered."""

    restrictions: tuple[Restriction, ...]

    def __post_init__(self) -> None:
        ordered = sorted(self.restrictions, key=Restriction.sort_key)
        object.__setattr__(self, "restrictions", tuple(ordered))
        seen: set[str] = set()
        for item in ordered:
            if item.restriction_id in seen:
                raise ClosureOverlayError(f"duplicate restriction id {item.restriction_id!r}")
            seen.add(item.restriction_id)

    @staticmethod
    def empty() -> "ClosureOverlay":
        return ClosureOverlay(())

    def for_edge(self, edge_id: str) -> tuple[Restriction, ...]:
        return tuple(item for item in self.restrictions if item.edge_id == edge_id)

    def is_closed_throughout(self, edge_id: str, window: Interval) -> bool:
        """True when no entry in ``window`` is legal on this lane."""
        return any(
            item.blocks_window(window) for item in self.for_edge(edge_id)
            if item.kind is RestrictionKind.CLOSED
        )

    def availability_intervals(self, edge_id: str, window: Interval) -> tuple[Interval, ...]:
        """Sub-windows of ``window`` in which the lane may be entered, ascending.

        Closures subtract; a lane with no closure raises nothing and stays fully
        available. Caps are not consulted here - a capped lane is still enterable,
        just slower, and that is priced by the cost model instead.
        """
        closed = [
            item.window
            for item in self.for_edge(edge_id)
            if item.kind is RestrictionKind.CLOSED and item.overlaps(window)
        ]
        return gaps_within(window, closed)

    def blocking_restriction(self, edge_id: str, window: Interval) -> Restriction | None:
        """The closure that forbids ``window`` entirely, or ``None``.

        Chosen by earliest start then id, so two overlapping closures always name
        the same one in the decision trace.
        """
        candidates = [
            item
            for item in self.for_edge(edge_id)
            if item.kind is RestrictionKind.CLOSED and item.blocks_window(window)
        ]
        return min(candidates, key=Restriction.sort_key) if candidates else None

    def binding_cap(
        self, edge_id: str, kind: RestrictionKind, window: Interval
    ) -> int | None:
        """The tightest declared cap in effect for the whole of ``window``."""
        active = [
            item
            for item in self.for_edge(edge_id)
            if item.kind is kind
            and item.value_mm is not None
            and item.window.contains(window)
        ]
        if not active:
            return None
        values = [item.value_mm for item in active if item.value_mm is not None]
        return min(values)

    def edges_closed_throughout(self, window: Interval) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.edge_id
                    for item in self.restrictions
                    if item.kind is RestrictionKind.CLOSED and item.blocks_window(window)
                }
            )
        )

    def mid_plan_expiries(self, window: Interval) -> tuple[Restriction, ...]:
        """Restrictions that lift inside ``window`` - the cases a static graph loses."""
        return tuple(
            sorted(
                (item for item in self.restrictions if item.expires_within(window)),
                key=Restriction.sort_key,
            )
        )

    def overlay_digest(self) -> str:
        return canonical_digest(self.restrictions)


def overlay_from_document(
    document: object,
    *,
    epoch_us: int,
    known_edges: tuple[str, ...],
) -> ClosureOverlay:
    """Build the overlay from the bundle ``closures`` / ``restrictions`` lists.

    A closure naming an unknown lane is a bundle defect and raises: silently
    dropping it would mean an unenforced restriction.
    """
    known = set(known_edges)
    items: list[Restriction] = []
    document_map = document if isinstance(document, dict) else {}
    entries = document_map.get("closures", [])
    if isinstance(entries, list):
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise ClosureOverlayError(f"closure entry {index} is not a mapping")
            items.append(_closure_from(raw, epoch_us=epoch_us, index=index, known=known))
    restrictions = document_map.get("restrictions", [])
    if isinstance(restrictions, list):
        for index, raw in enumerate(restrictions):
            if not isinstance(raw, dict):
                raise ClosureOverlayError(f"restriction entry {index} is not a mapping")
            items.append(_restriction_from(raw, epoch_us=epoch_us, index=index, known=known))
    return ClosureOverlay(tuple(items))


def _closure_from(
    raw: dict[str, object], *, epoch_us: int, index: int, known: set[str]
) -> Restriction:
    edge_id = _text(raw, "corridor_id")
    if edge_id not in known:
        raise ClosureOverlayError(
            f"closure {index} names lane {edge_id!r} which the bundle does not declare"
        )
    start = minutes_to_us(_whole(raw, "from_minute"), epoch_us)
    end = minutes_to_us(_whole(raw, "to_minute"), epoch_us)
    reason = str(raw.get("reason", "unspecified closure"))
    return Restriction(
        restriction_id=f"closure-{index:03d}-{edge_id}",
        edge_id=edge_id,
        kind=RestrictionKind.CLOSED,
        start_us=start,
        end_us=end,
        human_reason=reason,
        technical_detail=(
            f"lane {edge_id} closed for minutes {raw.get('from_minute')}.."
            f"{raw.get('to_minute')} of the bundle epoch (window [{start}, {end}))"
        ),
    )


def _restriction_from(
    raw: dict[str, object], *, epoch_us: int, index: int, known: set[str]
) -> Restriction:
    edge_id = _text(raw, "corridor_id")
    if edge_id not in known:
        raise ClosureOverlayError(
            f"restriction {index} names lane {edge_id!r} which the bundle does not declare"
        )
    kind = RestrictionKind(str(raw.get("kind", RestrictionKind.CLOSED.value)))
    start = minutes_to_us(_whole(raw, "from_minute"), epoch_us)
    end = minutes_to_us(_whole(raw, "to_minute"), epoch_us)
    value = raw.get("value_mm")
    return Restriction(
        restriction_id=f"restriction-{index:03d}-{edge_id}",
        edge_id=edge_id,
        kind=kind,
        start_us=start,
        end_us=end,
        human_reason=str(raw.get("reason", "unspecified restriction")),
        technical_detail=f"lane {edge_id} {kind.value} binding over [{start}, {end})",
        value_mm=_millimetres_or_none(value, edge_id),
    )


def _millimetres_or_none(value: object, edge_id: str) -> int | None:
    """A declared cap in whole millimetres, or ``None`` when the binding states none.

    A fractional cap is a bundle defect: rounding it down would make a restriction
    looser than the operator wrote it.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClosureOverlayError(
            f"restriction on lane {edge_id} declares cap {value!r}; "
            "millimetre values must be whole"
        )
    return value


def _text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ClosureOverlayError(f"restriction document field {key!r} must be a string")
    return value


def _whole(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClosureOverlayError(f"restriction document field {key!r} must be whole minutes")
    return value


__all__ = [
    "ClosureOverlay",
    "Restriction",
    "RestrictionKind",
    "overlay_from_document",
]
