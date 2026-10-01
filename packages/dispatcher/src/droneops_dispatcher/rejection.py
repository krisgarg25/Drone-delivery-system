"""Rejection explanations and the append-only decision record.

Phase 4's console does not ask the dispatcher anything; it reads records. So every
outcome - assigned, queued or refused - is written as a
:class:`DispatchDecision` carrying the ranked candidates, the components of each
rank, and for every refusal a :class:`RejectionExplanation` with both halves the
convention asks for: a sentence an operator can read, and the technical detail
that produced it. "Rejection" is never a status code alone.

The log is append-only in the strongest sense the package can express:
:class:`AppendOnlyDecisionLog` has no update method, no delete method, and refuses
a second append under an existing ``decision_id`` unless the payload is
byte-identical - the same identity carrying different content is exactly the
correction an auditor must not be able to make silently
(``conventions.md`` section 5: a correction is a superseding record).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from droneops_contracts import PriorityClass
from droneops_platform.ports import AppendOnlyViolation

from .eligibility import ExcludedAircraft
from .errors import DispatchInputError
from .policy import SIM_ONLY_BANNER, DispatchPolicy, digest_of
from .priority import ResequenceRefusal
from .queue import FairnessRank
from .ranking import EnergyScreen

DISPATCHER_ACTOR = "dispatcher:synthetic"


class ReasonCode(StrEnum):
    """Stable rejection codes. Renaming one is a contract change."""

    OFFLINE = "aircraft_offline"
    MAINTENANCE = "aircraft_maintenance"
    AIRCRAFT_BUSY = "aircraft_busy_this_round"
    AIRBORNE = "aircraft_airborne"
    LINK_UNCERTAIN = "aircraft_link_uncertain"
    OBSERVATION_STALE = "aircraft_observation_stale"
    PAYLOAD_EXCEEDED = "aircraft_payload_exceeded"
    CHARGER_UNREADY = "aircraft_charger_unready"
    NO_ELIGIBLE_AIRCRAFT = "no_eligible_aircraft"
    ENERGY_RESERVE_SHORT = "energy_margin_below_reserve"
    RECOVERY_INFEASIBLE = "recovery_not_feasible"
    RECOVERY_ROUTE_MISSING = "recovery_route_missing"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    STALE_WEATHER = "stale_weather"
    PROFILE_UNKNOWN = "profile_unknown"
    PAD_CAPACITY_QUEUE = "pad_capacity_queue"
    CHARGER_QUEUE = "charger_queue"
    CREW_UNAVAILABLE = "crew_unavailable"
    FACILITY_OUTAGE = "facility_outage"
    OUTSIDE_OPENING_HOURS = "outside_opening_hours"
    HORIZON_EXCEEDED = "horizon_exceeded"
    AIRBORNE_COMMITMENT_IMMUTABLE = "airborne_commitment_immutable"
    SLA_NO_EMERGENCY_RIGHT = "sla_has_no_emergency_right"
    TIME_WINDOW_INFEASIBLE = "time_window_infeasible"
    SOLVER_INFEASIBLE = "solver_infeasible"
    FAIRNESS_STARVATION = "starvation_bound_reached"
    INPUT_INVALID = "dispatch_input_invalid"


#: The exclusion reason the eligibility filter gives, mapped to a record code.
_REASON_BY_EXCLUSION: dict[str, ReasonCode] = {
    "aircraft_offline": ReasonCode.OFFLINE,
    "aircraft_maintenance": ReasonCode.MAINTENANCE,
    "aircraft_retired": ReasonCode.MAINTENANCE,
    "aircraft_contingency": ReasonCode.AIRBORNE,
    "aircraft_busy_this_round": ReasonCode.AIRCRAFT_BUSY,
    "aircraft_airborne": ReasonCode.AIRBORNE,
    "aircraft_link_uncertain": ReasonCode.LINK_UNCERTAIN,
    "aircraft_observation_stale": ReasonCode.OBSERVATION_STALE,
    "aircraft_payload_exceeded": ReasonCode.PAYLOAD_EXCEEDED,
    "aircraft_charger_unready": ReasonCode.CHARGER_UNREADY,
    "aircraft_status_unknown": ReasonCode.INPUT_INVALID,
}

_ENERGY_CODES: dict[str, ReasonCode] = {
    "energy_margin_below_reserve": ReasonCode.ENERGY_RESERVE_SHORT,
    "reserve_infeasible": ReasonCode.ENERGY_RESERVE_SHORT,
    "recovery_infeasible": ReasonCode.RECOVERY_INFEASIBLE,
    "recovery_route_missing": ReasonCode.RECOVERY_ROUTE_MISSING,
    "outbound_infeasible": ReasonCode.ENERGY_RESERVE_SHORT,
    "handover_infeasible": ReasonCode.ENERGY_RESERVE_SHORT,
    "gust_uplift_infeasible": ReasonCode.ENERGY_RESERVE_SHORT,
    "route_blocked_by_wind": ReasonCode.STALE_WEATHER,
    "stale_weather": ReasonCode.STALE_WEATHER,
    "deadline_exceeded": ReasonCode.DEADLINE_EXCEEDED,
    "sustained_wind_above_airframe_limit": ReasonCode.STALE_WEATHER,
    "battery_models_disagree": ReasonCode.INPUT_INVALID,
    "energy_model_refused": ReasonCode.INPUT_INVALID,
    "profile_unknown": ReasonCode.PROFILE_UNKNOWN,
}


@dataclass(frozen=True)
class RejectionExplanation:
    """A refusal in two registers, both persisted."""

    code: ReasonCode
    subject_kind: str
    subject_id: str
    mission_id: str
    human_reason: str
    technical: tuple[tuple[str, str], ...]
    policy_version: str
    fairness_policy_version: str
    config_release_sha: str
    banner: str = SIM_ONLY_BANNER
    persisted: bool = True

    def detail_of(self, key: str) -> str:
        for name, value in self.technical:
            if name == key:
                return value
        return ""

    def as_row(self) -> tuple[str, str, str, str]:
        return (self.subject_id, self.code.value, self.human_reason, self.detail_of("rule"))

    def digest(self) -> str:
        rows: list[tuple[str, object]] = [
            ("code", self.code.value),
            ("subject", f"{self.subject_kind}:{self.subject_id}"),
            ("mission_id", self.mission_id),
            ("human_reason", self.human_reason),
            ("policy_version", self.policy_version),
            ("config_release_sha", self.config_release_sha),
        ]
        rows.extend((f"technical_{key}", value) for key, value in self.technical)
        return digest_of(rows)

    def as_document(self) -> dict[str, object]:
        """What Phase 4 renders: the sentence, then the drawer."""
        return {
            "code": self.code.value,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "mission_id": self.mission_id,
            "human_reason": self.human_reason,
            "technical_detail": dict(self.technical),
            "policy_version": self.policy_version,
            "fairness_policy_version": self.fairness_policy_version,
            "config_release_sha": self.config_release_sha,
            "banner": self.banner,
        }


def explain(
    *,
    policy: DispatchPolicy,
    code: ReasonCode,
    subject_kind: str,
    subject_id: str,
    mission_id: str,
    human_reason: str,
    technical: Sequence[tuple[str, object]] = (),
) -> RejectionExplanation:
    """One construction point, so no explanation can be missing a reason."""
    if not human_reason.strip():
        raise DispatchInputError(f"{code.value}: a refusal must say why in one sentence")
    rows = tuple((key, str(value)) for key, value in technical)
    return RejectionExplanation(
        code=code,
        subject_kind=subject_kind,
        subject_id=subject_id,
        mission_id=mission_id,
        human_reason=human_reason,
        technical=rows,
        policy_version=policy.policy_version,
        fairness_policy_version=policy.fairness_policy_version,
        config_release_sha=policy.config_release_sha,
    )


def explain_exclusion(
    row: ExcludedAircraft, mission_id: str, policy: DispatchPolicy
) -> RejectionExplanation:
    code = _REASON_BY_EXCLUSION.get(row.reason.value, ReasonCode.INPUT_INVALID)
    return explain(
        policy=policy,
        code=code,
        subject_kind="aircraft",
        subject_id=row.aircraft_id,
        mission_id=mission_id,
        human_reason=f"{row.aircraft_id} ({row.status}) was not offered: {row.detail}",
        technical=(
            ("status", row.status),
            ("rule", "invariant I5: the candidate query excludes, it does not demote"),
            ("exclusion_reason", row.reason.value),
        ),
    )


def explain_screen(
    screen: EnergyScreen, mission_id: str, policy: DispatchPolicy
) -> RejectionExplanation:
    codes = sorted(
        {_ENERGY_CODES.get(item, ReasonCode.INPUT_INVALID) for item in screen.reason_codes}
    )
    code = codes[0] if codes else ReasonCode.ENERGY_RESERVE_SHORT
    return explain(
        policy=policy,
        code=code,
        subject_kind="aircraft",
        subject_id=screen.aircraft_id,
        mission_id=mission_id,
        human_reason=(
            f"{screen.aircraft_id} cannot complete this mission with reserve: "
            f"margin {screen.margin_wh} Wh, broken term {screen.breaking_term}"
        ),
        technical=(
            ("margin_wh", screen.margin_wh),
            ("consumption_wh", screen.consumption_wh),
            ("reserve_required_wh", screen.reserve_required_wh),
            ("breaking_term", screen.breaking_term),
            ("reject_reasons", ",".join(screen.reason_codes) or "none_reported"),
            ("profile", f"{screen.profile_id}@{screen.profile_version}"),
            ("parameters_sha256", screen.parameters_sha256),
            ("rule", "invariant I4: outbound + handover + recovery + reserve, or nothing"),
            ("basis", screen.disclaimer),
            ("note", screen.note),
        ),
    )


def explain_priority(refusal: ResequenceRefusal, policy: DispatchPolicy) -> RejectionExplanation:
    code = (
        ReasonCode.AIRBORNE_COMMITMENT_IMMUTABLE
        if refusal.code == "airborne_commitment_immutable"
        else ReasonCode.SLA_NO_EMERGENCY_RIGHT
    )
    return explain(
        policy=policy,
        code=code,
        subject_kind="mission",
        subject_id=refusal.target_mission_id or refusal.requesting_mission_id,
        mission_id=refusal.requesting_mission_id,
        human_reason=refusal.reason,
        technical=(
            ("code", refusal.code),
            ("target_protected", refusal.target_protected),
            ("rule", "technical-build/01 section 12: an airborne change is a proposal"),
        ),
    )


def explain_queue(
    *, mission_id: str, aircraft_id: str, reason: str, detail: str, policy: DispatchPolicy
) -> RejectionExplanation:
    code = {
        "pad_wait": ReasonCode.PAD_CAPACITY_QUEUE,
        "charger_wait": ReasonCode.CHARGER_QUEUE,
        "crew_unavailable": ReasonCode.CREW_UNAVAILABLE,
        "facility_outage": ReasonCode.FACILITY_OUTAGE,
        "outside_opening_hours": ReasonCode.OUTSIDE_OPENING_HOURS,
        "horizon_exceeded": ReasonCode.HORIZON_EXCEEDED,
    }.get(reason, ReasonCode.PAD_CAPACITY_QUEUE)
    return explain(
        policy=policy,
        code=code,
        subject_kind="aircraft",
        subject_id=aircraft_id,
        mission_id=mission_id,
        human_reason=(
            f"{aircraft_id} is queued rather than assigned: {detail} - the pad, charger "
            "and crew counts are physical, so the work waits instead of overbooking them"
        ),
        technical=(
            ("wait_reason", reason),
            ("detail", detail),
            ("rule", "invariant I6: capacity is never overbooked"),
        ),
    )


def explain_solver(
    mission_id: str, aircraft_id: str, detail: str, policy: DispatchPolicy
) -> RejectionExplanation:
    return explain(
        policy=policy,
        code=ReasonCode.SOLVER_INFEASIBLE,
        subject_kind="aircraft",
        subject_id=aircraft_id or mission_id,
        mission_id=mission_id,
        human_reason=f"no assignment exists for {mission_id}: {detail}",
        technical=(
            ("detail", detail),
            ("rule", "the solver answers infeasible; it never relaxes a window"),
        ),
    )


class DecisionOutcome(StrEnum):
    ASSIGNED = "assigned"
    QUEUED = "queued"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DispatchDecision:
    """Everything one dispatch round decided about one mission, in one record."""

    decision_id: str
    mission_id: str
    outcome: DecisionOutcome
    as_of_us: int
    aircraft_id: str | None
    ranked: tuple[tuple[str, str, int, int, int], ...]
    rejections: tuple[RejectionExplanation, ...]
    fairness: FairnessRank | None
    queue_state: str
    wait_reasons: tuple[tuple[str, int], ...]
    repositioning: tuple[tuple[str, object], ...]
    policy_version: str
    fairness_policy_version: str
    ranking_policy_version: str
    config_release_sha: str
    policy_digest: str
    solver_digest: str
    determinism: tuple[tuple[str, str], ...]
    proposal_digest: str
    priority: str = "routine"
    banner: str = SIM_ONLY_BANNER
    authoritative: bool = False
    safety_claim: bool = False

    def digest(self) -> str:
        rows: list[tuple[str, object]] = [
            ("decision_id", self.decision_id),
            ("mission_id", self.mission_id),
            ("outcome", self.outcome.value),
            ("as_of_us", self.as_of_us),
            ("aircraft_id", self.aircraft_id or ""),
            ("ranked", ";".join(",".join(str(part) for part in row) for row in self.ranked)),
            ("rejections", ";".join(row.digest() for row in self.rejections)),
            ("queue_state", self.queue_state),
            ("policy_digest", self.policy_digest),
            ("solver_digest", self.solver_digest),
            ("proposal_digest", self.proposal_digest),
        ]
        return digest_of(rows)

    def human_summary(self) -> tuple[str, ...]:
        lines = [
            f"{self.mission_id}: {self.outcome.value}"
            + (f" -> {self.aircraft_id}" if self.aircraft_id else ""),
            f"policy {self.policy_version} / fairness {self.fairness_policy_version} "
            f"/ config {self.config_release_sha}",
        ]
        if self.fairness is not None:
            lines.append(self.fairness.explanation(PriorityClass(self.priority)))
        lines.extend(row.human_reason for row in self.rejections)
        lines.append(self.banner)
        return tuple(lines)

    def as_document(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "outcome": self.outcome.value,
            "priority": self.priority,
            "as_of_us": self.as_of_us,
            "aircraft_id": self.aircraft_id,
            "ranked_candidates": [list(row) for row in self.ranked],
            "rejections": [row.as_document() for row in self.rejections],
            "fairness": None if self.fairness is None else list(self.fairness.as_row()),
            "queue_state": self.queue_state,
            "wait_reasons": dict(self.wait_reasons),
            "repositioning": dict(self.repositioning),
            "policy_version": self.policy_version,
            "fairness_policy_version": self.fairness_policy_version,
            "ranking_policy_version": self.ranking_policy_version,
            "config_release_sha": self.config_release_sha,
            "policy_digest": self.policy_digest,
            "solver_digest": self.solver_digest,
            "determinism": dict(self.determinism),
            "proposal_digest": self.proposal_digest,
            "banner": self.banner,
            "authoritative": False,
            "safety_claim": False,
        }


class AppendOnlyDecisionLog:
    """The dispatcher's own record of what it decided. Append, read, never edit."""

    def __init__(self) -> None:
        self._records: list[DispatchDecision] = []
        self._by_id: dict[str, DispatchDecision] = {}

    def append(self, decision: DispatchDecision) -> DispatchDecision:
        existing = self._by_id.get(decision.decision_id)
        if existing is not None:
            if existing.digest() != decision.digest():
                raise AppendOnlyViolation(
                    f"decision {decision.decision_id} already recorded with different "
                    "content: a correction must be a new superseding record"
                )
            return existing
        self._records.append(decision)
        self._by_id[decision.decision_id] = decision
        return decision

    def all(self) -> tuple[DispatchDecision, ...]:
        return tuple(self._records)

    def for_mission(self, mission_id: str) -> tuple[DispatchDecision, ...]:
        return tuple(row for row in self._records if row.mission_id == mission_id)

    def latest(self, mission_id: str) -> DispatchDecision | None:
        rows = self.for_mission(mission_id)
        return rows[-1] if rows else None

    def rejections_for(self, mission_id: str) -> tuple[RejectionExplanation, ...]:
        return tuple(
            row for decision in self.for_mission(mission_id) for row in decision.rejections
        )

    def __len__(self) -> int:
        return len(self._records)


__all__ = [
    "DISPATCHER_ACTOR",
    "AppendOnlyDecisionLog",
    "DecisionOutcome",
    "DispatchDecision",
    "ReasonCode",
    "RejectionExplanation",
    "explain",
    "explain_exclusion",
    "explain_priority",
    "explain_queue",
    "explain_screen",
    "explain_solver",
]
