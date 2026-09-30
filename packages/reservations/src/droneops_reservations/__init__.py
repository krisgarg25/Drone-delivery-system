from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from droneops_contracts import ReservationState

VALID_STATES = set(ReservationState)


def canonical_resource_key(kind: str, identifier: str) -> str:
    kind = kind.strip().lower()
    identifier = identifier.strip()
    if kind not in {"corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew"}:
        raise ValueError(f"Unsupported reservation resource kind: {kind}")
    if not identifier:
        raise ValueError("Reservation identifier must not be empty")
    return f"{kind}:{identifier}"


@dataclass
class Reservation:
    id: uuid.UUID
    mission_id: uuid.UUID
    resource_key: str
    valid_during: tuple[datetime, datetime]
    state: ReservationState = ReservationState.TENTATIVE

    def __post_init__(self) -> None:
        start, end = self.valid_during
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Reservation windows must be timezone-aware")
        if end < start:
            raise ValueError("Reservation end must not precede start")


def can_overlap_committed(a: Reservation, b: Reservation) -> bool:
    """Committed overlap is not allowed until a versioned policy explicitly supports exceptions."""
    return False


def validate_commit_set(reservations: list[Reservation], checker=None) -> None:
    committed = [
        reservation for reservation in reservations
        if reservation.state in {ReservationState.COMMITTED, ReservationState.ACTIVATED}
    ]
    if checker is not None:
        for reservation in committed:
            for other in committed:
                if reservation.id != other.id and reservation.resource_key == other.resource_key:
                    verdict = checker(reservation.valid_during, other.valid_during)
                    if str(verdict).upper().startswith("CONFLICT") and not can_overlap_committed(
                        reservation, other
                    ):
                        raise ValueError("Independent checker rejected a committed reservation set")
                    if str(verdict).upper().startswith("UNPROVEN"):
                        raise ValueError("Independent checker could not prove a committed reservation set")
