"""Meteostat station cross-check: an observation of the weather, never a decision.

This adapter answers a different question from Open-Meteo: not "what is the wind"
but "does the second source agree with the trace this run is replaying". Its output
is a :class:`CrossCheckResult` - a record a reviewer or a ``requires_review``
verdict can consume. It never modifies a snapshot, never blocks a reservation and
never feeds the energy kernel a number of its own: ``droneops_energy`` does not
import it, and no adapter may import the reservation ledger.

Like its sibling adapter, it holds no HTTP client: a caller injects a transport and
the live path is explicit.
"""

from __future__ import annotations

from .cross_check import (
    CrossCheckResult,
    StationReport,
    Verdict,
    cross_check_snapshot,
    report_from_payload,
)
from .live import LiveFetchAuthorisation, Transport, request_report

__all__ = [
    "CrossCheckResult",
    "LiveFetchAuthorisation",
    "StationReport",
    "Transport",
    "Verdict",
    "cross_check_snapshot",
    "report_from_payload",
    "request_report",
]
