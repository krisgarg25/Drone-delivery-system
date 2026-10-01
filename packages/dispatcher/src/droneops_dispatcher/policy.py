"""The versioned dispatch policy: fairness bound, ranking weights, solver settings.

Two numbers in here are load-bearing enough to be named constants and refused at
construction rather than defaulted silently:

* ``starvation_bound_us`` - the published queue-fairness cap. A routine mission may
  be overtaken by higher-priority work, but never for longer than this bound; at
  the bound it is entitled to the head of its own band and the round records a
  starvation event. The bound is *named*, configurable and version-pinned so a
  replay of an old decision says which bound applied.
* the solver settings. :class:`SolverSettings` accepts exactly one search worker
  and a non-negative fixed seed, and the reason is in ``Determinism`` below.

Determinism
    CP-SAT's answer depends on the worker count and the random seed. With one
    worker the search is a single deterministic thread, so the same model produces
    the same objective value and the same assignment on every run and every
    machine; with ``num_search_workers > 1`` the portfolio races and the winner
    depends on thread timing. The simulation's whole replay guarantee
    (``conventions.md`` section 4, invariant I7) rests on that being reproducible,
    so :class:`SolverSettings` rejects any other worker count. Raising it to buy
    throughput is not a performance change - it is a silent change to which
    aircraft the operator sees, and the determinism test in
    ``tests/test_solver_determinism.py`` will fail across ``PYTHONHASHSEED`` values.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from .errors import PolicyVersionError, SolverDeterminismError

MICROS_PER_SECOND: Final[int] = 1_000_000
MICROS_PER_MINUTE: Final[int] = 60 * MICROS_PER_SECOND
MICROS_PER_HOUR: Final[int] = 60 * MICROS_PER_MINUTE

DISPATCH_POLICY_VERSION: Final[str] = "dispatch-fairness-v1"
FAIRNESS_POLICY_VERSION: Final[str] = "edf-with-aging-v1"
RANKING_POLICY_VERSION: Final[str] = "dispatch-ranking-v1"

STARVATION_BOUND_NAME: Final[str] = "starvation_bound_us"
"""The published name of the fairness cap, quoted in every explanation."""

DEFAULT_STARVATION_BOUND_US: Final[int] = 15 * MICROS_PER_MINUTE
DEFAULT_AGING_STEP_US: Final[int] = 3 * MICROS_PER_MINUTE
ONE_SOLVER_WORKER: Final[int] = 1

SIM_ONLY_BANNER: Final[str] = "Simulation only. Not authorised for flight."

DETERMINISM_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("num_search_workers", "1 (portfolio parallelism breaks replay; see policy.py)"),
    ("random_seed", "fixed per DispatchPolicy, never derived from a clock"),
    ("kernel_time", "integer microseconds supplied by the caller; no wall clock read"),
    ("hash_order", "every collection is iterated sorted by identifier"),
    ("float_use", "none in ranking: integer units, basis points and watt-hours"),
)


def digest_of(rows: Sequence[tuple[str, object]]) -> str:
    """Canonical sha256 over ordered ``(key, value)`` rows.

    Sorted by key so the digest cannot depend on dict insertion order, and stringified
    with ``repr``-free ``str`` so a bool and an int never collide.
    """
    text = "\n".join(f"{key}={value}" for key, value in sorted(rows))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class KernelClock(Protocol):
    """The dispatcher is handed time; it never asks a clock for it."""

    def now_us(self) -> int: ...


@dataclass(frozen=True)
class SolverSettings:
    """The OR-Tools configuration a replay needs in order to reproduce an answer."""

    num_search_workers: int = ONE_SOLVER_WORKER
    random_seed: int = 2026_0930
    max_time_in_seconds: int = 5
    log_search_progress: bool = False

    def __post_init__(self) -> None:
        if self.num_search_workers != ONE_SOLVER_WORKER:
            raise SolverDeterminismError(
                f"num_search_workers must be exactly {ONE_SOLVER_WORKER}; a portfolio "
                "solver returns a timing-dependent answer and breaks deterministic replay "
                "(conventions.md section 4, invariant I7)"
            )
        if self.random_seed < 0:
            raise SolverDeterminismError("the solver seed must be fixed and non-negative")
        if self.max_time_in_seconds <= 0:
            raise SolverDeterminismError("a solver with no time budget cannot answer at all")

    def rows(self) -> tuple[tuple[str, object], ...]:
        return (
            ("num_search_workers", self.num_search_workers),
            ("random_seed", self.random_seed),
            ("max_time_in_seconds", self.max_time_in_seconds),
            ("log_search_progress", self.log_search_progress),
        )

    def digest(self) -> str:
        return digest_of(list(self.rows()))


@dataclass(frozen=True)
class DispatchPolicy:
    """Every number a dispatch round may depend on, in one versioned record.

    Ranking weights are integer *units* on the same scale as the planner's cost
    profile, so a weight can be compared with a lateness or an energy margin without
    a float anywhere in the total order.
    """

    policy_version: str = DISPATCH_POLICY_VERSION
    fairness_policy_version: str = FAIRNESS_POLICY_VERSION
    ranking_policy_version: str = RANKING_POLICY_VERSION
    config_release_sha: str = "sha256:unreleased"
    starvation_bound_us: int = DEFAULT_STARVATION_BOUND_US
    aging_step_us: int = DEFAULT_AGING_STEP_US
    max_sla_rank_promotions: int = 1
    lateness_penalty_units_per_minute: int = 500
    energy_margin_credit_units_per_wh: int = 8
    pad_wait_units_per_minute: int = 120
    crew_wait_units_per_minute: int = 120
    charger_wait_units_per_minute: int = 40
    repositioning_energy_units_per_wh: int = 25
    repositioning_time_units_per_minute: int = 80
    repositioning_pad_occupancy_units: int = 200
    unserved_penalty_units: int = 10_000
    max_observation_age_us: int = 30 * MICROS_PER_SECOND
    solver: SolverSettings = field(default_factory=SolverSettings)

    def __post_init__(self) -> None:
        for name in ("policy_version", "fairness_policy_version", "ranking_policy_version"):
            if not getattr(self, name).strip():
                raise PolicyVersionError(f"{name} must be a non-empty version string")
        if self.starvation_bound_us <= 0:
            raise PolicyVersionError(
                f"{STARVATION_BOUND_NAME} must be a positive bound: an unbounded queue is "
                "a queue in which routine work may never run"
            )
        if self.aging_step_us <= 0:
            raise PolicyVersionError("the aging step must be positive")
        if self.starvation_bound_us < self.aging_step_us:
            raise PolicyVersionError("the aging step cannot exceed the starvation bound")
        if self.max_sla_rank_promotions < 0:
            raise PolicyVersionError("a payment tier cannot buy negative or unbounded promotion")
        for name in (
            "lateness_penalty_units_per_minute",
            "energy_margin_credit_units_per_wh",
            "pad_wait_units_per_minute",
            "crew_wait_units_per_minute",
            "charger_wait_units_per_minute",
            "repositioning_energy_units_per_wh",
            "repositioning_time_units_per_minute",
            "unserved_penalty_units",
        ):
            if getattr(self, name) < 0:
                raise PolicyVersionError(f"{name} cannot be negative")
        if self.max_observation_age_us <= 0:
            raise PolicyVersionError("an observation age must be positive")

    @property
    def starvation_bound_name(self) -> str:
        return STARVATION_BOUND_NAME

    def aging_steps(self, waited_us: int) -> int:
        """How many fairness promotions ``waited_us`` of queue age has earned."""
        if waited_us <= 0:
            return 0
        return waited_us // self.aging_step_us

    def rows(self) -> tuple[tuple[str, object], ...]:
        return (
            ("policy_version", self.policy_version),
            ("fairness_policy_version", self.fairness_policy_version),
            ("ranking_policy_version", self.ranking_policy_version),
            ("config_release_sha", self.config_release_sha),
            (STARVATION_BOUND_NAME, self.starvation_bound_us),
            ("aging_step_us", self.aging_step_us),
            ("max_sla_rank_promotions", self.max_sla_rank_promotions),
            ("lateness_penalty_units_per_minute", self.lateness_penalty_units_per_minute),
            ("energy_margin_credit_units_per_wh", self.energy_margin_credit_units_per_wh),
            ("pad_wait_units_per_minute", self.pad_wait_units_per_minute),
            ("crew_wait_units_per_minute", self.crew_wait_units_per_minute),
            ("charger_wait_units_per_minute", self.charger_wait_units_per_minute),
            ("repositioning_energy_units_per_wh", self.repositioning_energy_units_per_wh),
            ("repositioning_time_units_per_minute", self.repositioning_time_units_per_minute),
            ("repositioning_pad_occupancy_units", self.repositioning_pad_occupancy_units),
            ("unserved_penalty_units", self.unserved_penalty_units),
            ("max_observation_age_us", self.max_observation_age_us),
            *[(f"solver_{key}", value) for key, value in self.solver.rows()],
        )

    def digest(self) -> str:
        return digest_of(list(self.rows()))

    def determinism(self) -> tuple[tuple[str, str], ...]:
        """What a reviewer must know to reproduce this round."""
        return DETERMINISM_ROWS

    @property
    def banner(self) -> str:
        return SIM_ONLY_BANNER


__all__ = [
    "DEFAULT_AGING_STEP_US",
    "DEFAULT_STARVATION_BOUND_US",
    "DETERMINISM_ROWS",
    "DISPATCH_POLICY_VERSION",
    "FAIRNESS_POLICY_VERSION",
    "MICROS_PER_HOUR",
    "MICROS_PER_MINUTE",
    "MICROS_PER_SECOND",
    "ONE_SOLVER_WORKER",
    "RANKING_POLICY_VERSION",
    "SIM_ONLY_BANNER",
    "STARVATION_BOUND_NAME",
    "DispatchPolicy",
    "KernelClock",
    "SolverSettings",
    "digest_of",
]
