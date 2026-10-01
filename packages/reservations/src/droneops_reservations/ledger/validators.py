"""Two independent validators, both wired to the same ledger code path.

:class:`IntervalCommitValidator`
    Deterministic and dependency-free: it re-derives occupancy from half-open integer
    microsecond windows on a resource identity and answers SAFE / CONFLICT / UNPROVEN.
    The stress, rollback and fuzz proofs use it, because a 100-way test must not have its
    slowest component be the validator.

:class:`CheckerCommitValidator`
    The real ``droneops_conflict_checker``, driven through ``check_set`` over the *final*
    set, re-deriving swept geometry from the raw plan points each record carries. This is
    the validator that makes the Phase 2 exit criterion true rather than described: the
    ledger promotes only when a second implementation, with its own construction and its
    own arithmetic, agrees.

Both are **synchronous, read-only functions of a value**. They receive a
:class:`~droneops_reservations.commit.FinalSet` - data, not a session - so a validator
cannot be steered by the same uncommitted transaction it is judging, and the checker's own
rule ("reads only committed records") survives being called from inside one.

A record either validator cannot reason about is never dropped: it becomes ``UNPROVEN``,
which blocks. Dropping it would turn "we did not check this" into "this is clear".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from droneops_conflict_checker import (
    AllowRuleSet,
    CheckerPolicy,
    CommittedPlanRecord,
    ConflictReport,
    EvaluationContext,
    PlannerClaim,
    PlanPoint,
    check_set,
)
from droneops_contracts import AltitudeReference
from droneops_geom_core.intervals import Interval

from droneops_reservations.commit import AllowRule, FinalSet, ProtectedHolding, ValidationOutcome
from droneops_reservations.intake import ProfileSample, Verdict
from droneops_reservations.states import first_protecting


def _values(value: object) -> tuple[object, ...]:
    return tuple(value) if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()


def _rows(value: object) -> tuple[tuple[object, ...], ...]:
    """A JSON array of arrays, as indexable rows, with anything else dropped."""
    return tuple(
        tuple(row)
        for row in _values(value)
        if isinstance(row, Sequence) and not isinstance(row, str | bytes)
    )


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _text(value: object, field: str) -> str:
    """A JSON string, refused rather than coerced if it is not one.

    A payload whose altitude reference is a number is a corrupt record, and
    ``AltitudeReference(str(3))`` would turn that corruption into a different,
    confidently wrong error several layers away.
    """
    if not isinstance(value, str):
        raise ValueError(f"profile payload field {field} is {type(value).__name__}, not text")
    return value


def _measurement(value: object, field: str) -> float:
    """A JSON number as a float, with the type narrowed where it is read.

    ``float(object)`` is what mypy --strict refuses, and the refusal is the point: a
    payload row is untyped JSON, so the check has to happen at this boundary rather than
    as a cast that promises a type nobody verified. A numeric *string* is accepted because
    ``jsonb`` round-trips can hand one back; a bool is not, because ``True`` as a
    coordinate is a defect and not a position.
    """
    if isinstance(value, bool):
        raise ValueError(f"profile payload field {field} is a boolean, not a measurement")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"profile payload field {field} is {type(value).__name__}, not a measurement")


def _whole(value: object, field: str) -> int:
    """A JSON number as an integer, refusing a value with a fractional part.

    Declared speed is in whole millimetres per second; ``int(11_999.7)`` would silently
    restate a corrupt payload as a legal one.
    """
    if isinstance(value, bool):
        raise ValueError(f"profile payload field {field} is a boolean, not a count")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"profile payload field {field} is {value}, which is not whole")
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"profile payload field {field} is {type(value).__name__}, not a count")


@dataclass(frozen=True)
class PlanRecordPayload:
    """The JSON one ``plan_version.profile_json`` row carries.

    Written by the ledger and read back by both validators, so the committed record - not
    the planner's memory - is what the checker evaluates. That is invariant I1 made
    concrete: a re-plan cannot retro-fit the evidence, and a validator written next quarter
    can re-derive the protected volumes from rows that already exist.
    """

    strategy: str
    aircraft_id: str
    planner_verdict: str
    policy_version: str
    config_release_sha: str
    points: tuple[ProfileSample, ...]
    claims: tuple[str, ...]

    def as_json(self) -> str:
        return json.dumps(
            {
                "strategy": self.strategy,
                "aircraft_id": self.aircraft_id,
                "planner_verdict": self.planner_verdict,
                "policy_version": self.policy_version,
                "config_release_sha": self.config_release_sha,
                "points": [list(point.as_row()) for point in self.points],
                "claims": list(self.claims),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> PlanRecordPayload:
        payload: Mapping[str, object] = json.loads(text)
        return cls(
            strategy=str(payload.get("strategy", "")),
            aircraft_id=str(payload.get("aircraft_id", "")),
            planner_verdict=str(payload.get("planner_verdict", "SAFE")),
            policy_version=str(payload.get("policy_version", "")),
            config_release_sha=str(payload.get("config_release_sha", "")),
            points=tuple(
                ProfileSample(
                    at=_instant(row[0]),
                    lat_deg=_measurement(row[1], "lat_deg"),
                    lon_deg=_measurement(row[2], "lon_deg"),
                    altitude_m=_measurement(row[3], "altitude_m"),
                    altitude_reference=AltitudeReference(_text(row[4], "altitude_reference")),
                    speed_mm_per_s=_whole(row[5], "speed_mm_per_s"),
                )
                for row in _rows(payload.get("points", ()))
                if len(row) == 6
            ),
            claims=tuple(str(item) for item in _values(payload.get("claims", ()))),
        )


def identity_of(stored_key: str) -> str:
    """``<kind>:<identifier>`` of a stored key - the physical resource, windowless.

    This is the comparison the database's exclusion constraint cannot make: its predicate
    is ``resource_key WITH =``, so the same cell over a different window is a *different*
    string and never collides in the index. Identity overlap is therefore the ledger's
    job, and this function is its single definition.
    """
    parts = stored_key.split(":")
    if len(parts) < 2 or not parts[1]:
        raise ValueError(f"stored resource key {stored_key!r} has no identifier")
    return f"{parts[0]}:{parts[1]}"


def records_from_pool_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    body_radius_mm: int = 1200,
    body_half_height_mm: int = 750,
) -> tuple[CommittedPlanRecord, ...]:
    """Group stored reservation rows into one checker record per (plan, mission).

    The ledger's holder read returns one row per resource window; the checker reasons
    about *plans*, each of which claims several windows. Reconstructing the grouping here
    is what stops a mission's second resource from being presented as an unrelated
    conflict, and it is the same shape :class:`CheckerCommitValidator` builds from its own
    ``final.holders``.

    A row whose plan carries no profile is skipped rather than guessed at: without stored
    points there is no volume to re-derive, and that absence is the ledger's holder read
    (``left join``) telling the truth about a mission that reserved airspace before plan
    evidence existed. Such a mission still blocks by identity at step 8; it simply cannot
    be judged by geometry.
    """
    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    payloads: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        plan_version = str(row.get("route_hash") or "")
        profile_json = row.get("profile_json")
        if not plan_version or not profile_json:
            continue
        mission_id = str(row.get("mission_id"))
        key = (plan_version, mission_id)
        grouped.setdefault(key, []).append((str(row.get("resource_key")), str(row.get("state"))))
        payloads[key] = (str(profile_json), mission_id)
    built: list[CommittedPlanRecord] = []
    for (plan_version, mission_id), keys in sorted(grouped.items()):
        payload = PlanRecordPayload.from_json(payloads[(plan_version, mission_id)][0])
        built.append(
            CommittedPlanRecord(
                plan_version=plan_version,
                mission_id=mission_id,
                status=sorted({state for _, state in keys})[0],
                points=tuple(
                    PlanPoint(
                        at=sample.at,
                        lat_deg=sample.lat_deg,
                        lon_deg=sample.lon_deg,
                        altitude_m=sample.altitude_m,
                        altitude_reference=sample.altitude_reference.value,
                        speed_mm_per_s=sample.speed_mm_per_s,
                    )
                    for sample in payload.points
                ),
                resource_keys=tuple(sorted({key for key, _ in keys})),
                body_radius_mm=body_radius_mm,
                body_half_height_mm=body_half_height_mm,
                policy_release=payload.policy_version,
                source="ledger",
                planner_claim=PlannerClaim(
                    verdict=payload.planner_verdict, policy_version=payload.policy_version
                ),
            )
        )
    return tuple(built)


class IntervalCommitValidator:
    """Overlap-by-identity validation over half-open microsecond windows.

    Adjacency is not overlap: two windows that touch at one instant are SAFE, which is the
    ``geom-core`` half-open rule applied consistently rather than a local opinion.
    """

    name = "interval-commit-validator"
    version = "1"

    def validate(self, final: FinalSet) -> ValidationOutcome:
        requested = final.requested
        holders = final.holders
        findings: list[tuple[str, str]] = []
        unproven: list[str] = []
        pairs = 0

        for claim in requested:
            for holder in holders:
                if identity_of(holder.resource_key) != claim.identity:
                    continue
                pairs += 1
                if not holder.valid_from < holder.valid_until:
                    unproven.append(f"{holder.resource_key} has an inverted stored window")
                    continue
                if Interval.from_datetimes(holder.valid_from, holder.valid_until).overlaps(claim.window):
                    findings.append((holder.resource_key, holder.state))

        for index, left in enumerate(requested):
            for right in requested[index + 1 :]:
                pairs += 1
                if left.conflicts_with(right):
                    findings.append((left.stored_key, "requested"))

        conflict = first_protecting(findings)
        verdict = Verdict.SAFE if conflict is None else Verdict.CONFLICT
        if conflict is None and unproven:
            verdict = Verdict.UNPROVEN
        return ValidationOutcome(
            verdict=verdict,
            reasons=(conflict.detail,) if conflict is not None else tuple(sorted(unproven)),
            checker_name=self.name,
            checker_version=self.version,
            records_checked=len(requested) + len(holders),
            pairs_checked=pairs,
        )


class CheckerCommitValidator:
    """The independent checker, used as the promotion vote.

    The candidate about to be committed is evaluated in the state it *would* hold,
    alongside every existing protected holder rebuilt from its own stored plan points, and
    the whole set goes to ``check_set`` together.

    ``committed_records`` is the checker's wider world: a read-only source of *committed*
    records the caller knows exist but did not put in the final set, because the set was
    built by resource identity and those rows hold a different key. Which of them join the
    examination is decided inside the checker - same key, adjacent lane, adjacent cell, or
    protected volumes that reach - so a pair sharing no resource key can still be convicted
    here. It is a port, not a database handle: this package still holds no session, and the
    source must return only rows the database has committed.
    """

    name = "droneops_conflict_checker"
    version = "2"

    def __init__(
        self,
        policy: CheckerPolicy,
        evaluated_at: datetime,
        *,
        body_radius_mm: int = 1200,
        body_half_height_mm: int = 750,
        adjacency: Mapping[str, Sequence[str]] | None = None,
        rules: AllowRuleSet | None = None,
        committed_records: Callable[[FinalSet], Sequence[CommittedPlanRecord]] | None = None,
    ) -> None:
        self._policy = policy
        self._context = EvaluationContext(
            policy=policy, evaluated_at=evaluated_at, adjacency=dict(adjacency or {})
        )
        self._body_radius_mm = body_radius_mm
        self._body_half_height_mm = body_half_height_mm
        self._rules = rules
        self._committed_records = committed_records

    @property
    def policy_version(self) -> str:
        """The release this validator speaks for, so a caller can refuse a mismatch."""
        return self._policy.policy_version

    def validate(self, final: FinalSet) -> ValidationOutcome:
        records = [
            self._record_from_candidate(final),
            *(self._record_from_holder(holder, final) for holder in final.holders),
        ]
        pool = () if self._committed_records is None else tuple(self._committed_records(final))
        report = check_set(records, self._context, rules=self._rules, pool=pool)
        blocked = [item for item in report.violations if item.blocked]
        if not report.blocks_release:
            verdict = Verdict.SAFE
        elif blocked or report.disagreements:
            # A proven contact, or a planner/checker disagreement, is a conflict. The
            # disagreement is never reconciled here: that is the checker's own rule, and
            # folding it into a verdict would make the second opinion meaningless.
            verdict = Verdict.CONFLICT
        else:
            verdict = Verdict.UNPROVEN
        return ValidationOutcome(
            verdict=verdict,
            reasons=report.release_blockers(),
            grant=self._grant_of(report),
            policy_key=report.policy_key,
            checker_name=self.name,
            checker_version=f"{self._policy.policy_version}/{report.basis}",
            records_checked=report.records_checked,
            pairs_checked=report.pairs_checked,
            planner_disagreement=bool(report.disagreements),
        )

    @staticmethod
    def _grant_of(report: ConflictReport) -> AllowRule | None:
        """The exception the checker itself applied, if it applied one.

        Only an *unblocked* violation carries a grant the ledger may rely on: an allow
        rule excuses a measured contact, it never excuses an unknown.
        """
        for violation in report.violations:
            if violation.grant is not None and not violation.blocked:
                return AllowRule(violation.grant.rule_id, violation.grant.rule_version)
        return None

    def _record_from_candidate(self, final: FinalSet) -> CommittedPlanRecord:
        plan = final.candidate
        return CommittedPlanRecord(
            plan_version=plan.route_digest,
            mission_id=plan.candidate_id,
            # The candidate is evaluated in the state it is about to hold, because the
            # question is whether the final committed set is valid - not whether a
            # tentative row exists. ``holds_airspace`` is what puts it into the pairs.
            status="committed",
            points=tuple(self._point(sample) for sample in plan.profile),
            resource_keys=tuple(claim.stored_key for claim in plan.ordered_claims()),
            body_radius_mm=self._body_radius_mm,
            body_half_height_mm=self._body_half_height_mm,
            policy_release=final.policy_version,
            source="ledger",
            planner_claim=PlannerClaim(
                verdict=plan.planner_verdict.value,
                policy_version=final.policy_version,
                allow_rule_id=plan.allow_rule_id,
                allow_rule_version=plan.allow_rule_version,
            ),
        )

    def _record_from_holder(self, holder: ProtectedHolding, final: FinalSet) -> CommittedPlanRecord:
        return CommittedPlanRecord(
            plan_version=holder.plan_version,
            mission_id=str(holder.mission_id),
            status=holder.state,
            points=tuple(self._point(sample) for sample in holder.points),
            resource_keys=(holder.resource_key,),
            body_radius_mm=self._body_radius_mm,
            body_half_height_mm=self._body_half_height_mm,
            policy_release=holder.policy_version or final.policy_version,
            source="ledger",
            telemetry_last_fix=holder.telemetry_last_fix,
            planner_claim=None
            if holder.planner_verdict is None
            else PlannerClaim(
                verdict=holder.planner_verdict, policy_version=holder.policy_version or final.policy_version
            ),
        )

    def _point(self, sample: ProfileSample) -> PlanPoint:
        return PlanPoint(
            at=sample.at,
            lat_deg=sample.lat_deg,
            lon_deg=sample.lon_deg,
            altitude_m=sample.altitude_m,
            altitude_reference=sample.altitude_reference.value,
            speed_mm_per_s=sample.speed_mm_per_s,
        )


__all__ = [
    "CheckerCommitValidator",
    "IntervalCommitValidator",
    "PlanRecordPayload",
    "identity_of",
    "records_from_pool_rows",
]
