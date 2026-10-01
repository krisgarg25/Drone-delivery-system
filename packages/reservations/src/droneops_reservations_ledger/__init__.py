"""``droneops_reservations_ledger`` - the SQL half of the reservation ledger.

The sibling package ``droneops_reservations`` holds the decision logic and is forbidden
by ``.importlinter``'s ``domain-is-pure`` contract from importing SQLAlchemy, asyncpg or
``droneops_persistence``. This module is where that boundary is *honoured* rather than
worked around: it is a separate top-level module inside the same ``packages/reservations``
distribution, it imports the domain package (never the other way round), and it owns
exactly one thing - running the eleven steps of ``technical-build/01`` section 9 against
a real PostgreSQL in one transaction.

Nothing here decides whether airspace may be committed. It calls
:func:`droneops_reservations.preflight` for steps 3 to 6,
:func:`droneops_reservations.promotion` for the vote, and
:mod:`droneops_reservations.states` for what a state means; every one of those calls is a
pure function of values it hands over.
"""

from __future__ import annotations

from .service import (
    CancelReceipt,
    CommitReceipt,
    Declined,
    ReservationCommitLedger,
    utc_now,
)
from .store import (
    COMMITTED_STATE_LIST,
    PROTECTED_STATE_LIST,
    DecisionRow,
    EventRow,
    LedgerStore,
    MissionRow,
    OrderRow,
    ReservationRow,
    ResourceRow,
    RowCounts,
    Transaction,
    WorldRead,
)
from .validators import (
    CheckerCommitValidator,
    IntervalCommitValidator,
    PlanRecordPayload,
    identity_of,
)

__all__ = [
    "COMMITTED_STATE_LIST",
    "PROTECTED_STATE_LIST",
    "CancelReceipt",
    "CheckerCommitValidator",
    "CommitReceipt",
    "DecisionRow",
    "Declined",
    "EventRow",
    "IntervalCommitValidator",
    "LedgerStore",
    "MissionRow",
    "OrderRow",
    "PlanRecordPayload",
    "ReservationCommitLedger",
    "ReservationRow",
    "ResourceRow",
    "RowCounts",
    "Transaction",
    "WorldRead",
    "identity_of",
    "utc_now",
]
