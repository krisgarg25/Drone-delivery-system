"""The ledger's decision logic, proven without a database.

Everything in ``droneops_reservations`` is a pure function of values, which means the
rules that matter - lock order, eligibility, promotion, what a state protects - can be
proven here rather than only in the concurrency suite. The database proves atomicity;
these tests prove the *decisions* the ledger makes are the documented ones.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from droneops_core_domain import ALLOWED_TRANSITIONS
from droneops_contracts import (
    AircraftStatus,
    AltitudeReference,
    LinkQuality,
    MissionStatus,
    PriorityClass,
    ReservationState,
)
from droneops_geom_core.intervals import Interval
from droneops_geom_core.resources import ResourceKeyError, resource_key
from hypothesis import given
from hypothesis import strategies as st

from droneops_reservations import (
    AircraftCandidate,
    CandidatePlan,
    CommitCommand,
    CommitStep,
    DecisionTrace,
    EnergyVerdict,
    FinalSet,
    PreflightFailure,
    ProfileSample,
    Rejection,
    RejectionCode,
    ResourceClaim,
    STEP_SEQUENCE,
    Selection,
    Verdict,
    WorldView,
    assert_mission_can_be_accepted,
    assert_transition,
    blocks_new_commitment,
    deterministic_id,
    eligible_aircraft,
    find_self_conflict,
    identity_sort_key,
    in_lock_order,
    input_hash,
    lock_tokens,
    may_be_released,
    mission_commit_path,
    parse_stored_key,
    preflight,
    protected_states,
    promotion,
    resolve_cancel_race,
    resource_claim_identity_order,
    stored_key_form,
    validate_lock_order,
)
from droneops_reservations.commit import AllowRule, ProtectedHolding, ValidationOutcome
from droneops_reservations.errors import CommitRejected, ReservationTransitionError
from droneops_reservations.protocol import ledger_kind

NOW = datetime(2027, 3, 4, 6, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


KINDS: tuple[str, ...] = ("corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew")


def claim(kind: str, identifier: str, start: int, end: int) -> ResourceClaim:
    span = Interval.of(start, end)
    return ResourceClaim(resource_key(kind, identifier, None if kind == "corridor" else span), span)


def verdict_outcome(
    verdict: Verdict,
    reasons: tuple[str, ...] = (),
    grant: tuple[str, str] | None = None,
) -> ValidationOutcome:
    return ValidationOutcome(
        verdict=verdict,
        reasons=reasons,
        grant=None if grant is None else AllowRule(*grant),
        checker_name="unit-validator",
        checker_version="0",
    )


def make_command(**changes: object) -> CommitCommand:
    plane = AircraftCandidate(
        aircraft_id="AC-1",
        status=AircraftStatus.AVAILABLE,
        link=LinkQuality.GOOD,
        priority_class=PriorityClass.ROUTINE,
        energy=EnergyVerdict(200, 100, 30, 70, True, "model-v1"),
        lateness_us=0,
        disturbance_units=0,
        fairness_units=0,
    )
    plan = CandidatePlan(
        candidate_id="c-1",
        aircraft_id=plane.aircraft_id,
        strategy="lane-based",
        route_digest="d" * 64,
        claims=(claim("corridor", "C-01", 0, 60_000_000),),
        profile=(
            ProfileSample(NOW, 30.7, 76.7, 60.0, AltitudeReference.AGL, 10_000),
            ProfileSample(NOW + timedelta(minutes=1), 30.72, 76.72, 60.0, AltitudeReference.AGL, 10_000),
        ),
    )
    world = WorldView("policy-v1", "sha256:" + "0" * 64, "digest", 0)
    tenant = uuid.uuid4()
    command = CommitCommand(
        tenant_id=tenant,
        order_id=uuid.uuid4(),
        mission_id=uuid.uuid4(),
        plan_version_id=uuid.uuid4(),
        commitment_token="token-1",
        idempotency_key="idem-1",
        policy_version="policy-v1",
        config_release_sha="sha256:" + "0" * 64,
        actor="unit",
        world=world,
        aircraft=(plane,),
        candidates=(plan,),
    )
    for key, value in changes.items():
        command = replace(command, **{key: value})
    return command


# ------------------------------------------------------------------ the eleven steps
def test_the_transaction_is_eleven_numbered_steps_in_one_order() -> None:
    """Section 9 as a type: eleven, numbered, and in the documented sequence."""
    assert len(CommitStep) == 11
    assert len(STEP_SEQUENCE) == 11
    assert [step.number for step in CommitStep] == list(range(1, 12))
    assert [step.description for step in CommitStep] == list(STEP_SEQUENCE)
    assert CommitStep.VALIDATE_DUPLICATE_IDENTITY.next() is CommitStep.FREEZE_WORLD_SNAPSHOT
    assert "canonical resource order" in CommitStep.ACQUIRE_ROW_LOCKS.description
    assert CommitStep.ACQUIRE_ROW_LOCKS.number < CommitStep.RECHECK_COMMITTED_RESERVATIONS.number
    assert CommitStep.RECHECK_COMMITTED_RESERVATIONS.number < CommitStep.WRITE_OPERATIONAL_RECORDS.number


# ------------------------------------------------------------------ canonical order
def test_lock_order_is_independent_of_submission_order() -> None:
    """The same resource set shuffled ten ways locks in exactly one order."""
    claims = (
        claim("crew", "CR-1", 0, 10),
        claim("corridor", "C-02", 0, 10),
        claim("charger_bay", "CH-1", 0, 10),
        claim("corridor", "C-01", 0, 10),
        claim("volume_cell", "V-01", 0, 10),
        claim("pad", "P-1", 0, 10),
        claim("crossing", "X-1", 0, 10),
    )
    rng = random.Random(7)
    ordered = in_lock_order(claims)
    for _ in range(25):
        shuffled = list(claims)
        rng.shuffle(shuffled)
        assert in_lock_order(shuffled) == ordered
    kinds = [item.key.kind.name.lower() for item in ordered]
    assert kinds == ["corridor", "corridor", "volume_cell", "crossing", "pad", "charger_bay", "crew"]
    assert lock_tokens(claims) == ("corridor:C-01", "corridor:C-02", "volume_cell:V-01", "crossing:X-1",
                                   "pad:P-1", "charger_bay:CH-1", "crew:CR-1")


def test_identity_order_matches_claim_order() -> None:
    """Cancel locks by identity; commit locks by claim. They must be the same order."""
    claims = (claim("volume_cell", "V-02", 5, 9), claim("corridor", "C-01", 5, 9))
    identities = resource_claim_identity_order(item.identity for item in claims)
    assert identities == tuple(dict.fromkeys(lock_tokens(claims)))
    assert identity_sort_key("volume_cell:V-02") > identity_sort_key("corridor:C-01")


def test_the_same_resource_over_two_windows_is_one_lock_and_a_self_conflict() -> None:
    """Deduplication is for identical claims; overlapping claims are a rejection."""
    same = (claim("volume_cell", "V-01", 0, 10), claim("volume_cell", "V-01", 0, 10))
    assert len(in_lock_order(same)) == 1
    overlap = (claim("volume_cell", "V-01", 0, 10), claim("volume_cell", "V-01", 5, 15))
    rejection = find_self_conflict(overlap)
    assert rejection is not None and rejection.code is RejectionCode.CANDIDATE_SELF_OVERLAP
    assert lock_tokens(overlap) == ("volume_cell:V-01",)


def test_touching_windows_are_not_overlap() -> None:
    """Half-open semantics: ``[0,10)`` and ``[10,20)`` coexist - that is what a slot is."""
    left, right = claim("volume_cell", "V-01", 0, 10), claim("volume_cell", "V-01", 10, 20)
    assert not left.conflicts_with(right)
    assert find_self_conflict((left, right)) is None


def test_an_empty_claim_set_is_a_rejection_not_a_no_op() -> None:
    with pytest.raises(CommitRejected) as raised:
        validate_lock_order(())
    assert raised.value.rejection.code is RejectionCode.LOCK_ORDER_INVALID


def test_stored_key_form_satisfies_the_database_prefix_rule() -> None:
    """``ck_reservation_kind_matches_key_prefix`` demands ``kind:`` == enum label.

    ``geom-core`` emits the section 6 tokens (``volume:``, ``charger:``) and contracts and
    the live schema demand the enum labels (``volume_cell:``, ``charger_bay:``). The stored
    form follows the schema, and the bridge round-trips.
    """
    for kind, identifier, span in (
        ("corridor", "C-01", None),
        ("volume_cell", "V-01", Interval.of(0, 10)),
        ("charger_bay", "CH-1", Interval.of(3, 9)),
    ):
        key = resource_key(kind, identifier, span)
        stored = stored_key_form(key)
        assert ledger_kind(stored) == kind
        assert stored.split(":")[0] == kind
        assert parse_stored_key(stored) == key
    with pytest.raises(ResourceKeyError):
        parse_stored_key("corridor:C-01:0..10")


# ------------------------------------------------------------------ steps 3 to 6
def test_offline_and_maintenance_aircraft_never_appear_as_eligible() -> None:
    """I5 asserted on the query, not on a later ranking position."""
    offered = (
        plane("AC-OFF", status=AircraftStatus.OFFLINE),
        plane("AC-MNT", status=AircraftStatus.MAINTENANCE),
        plane("AC-RET", status=AircraftStatus.RETIRED),
        plane("AC-LOST", link=LinkQuality.LOST),
        plane("AC-OK"),
    )
    eligible, rejections = eligible_aircraft(offered)
    assert [item.aircraft_id for item in eligible] == ["AC-OK"]
    assert {item.aircraft_id for item in rejections} == {"AC-OFF", "AC-MNT", "AC-RET", "AC-LOST"}
    assert {item.code for item in rejections} == {RejectionCode.AIRCRAFT_NOT_OPERABLE}


def test_the_ranking_is_a_total_order_and_survives_shuffling() -> None:
    rng = random.Random(11)
    offered = tuple(plane(f"AC-{index}", lateness_us=index * 3, margin_wh=100 - index) for index in range(12))
    expected = [item.aircraft_id for item in eligible_aircraft(offered)[0]]
    for _ in range(20):
        shuffled = list(offered)
        rng.shuffle(shuffled)
        assert [item.aircraft_id for item in eligible_aircraft(shuffled)[0]] == expected


def test_a_candidate_below_the_reserve_or_without_recovery_is_rejected() -> None:
    thin = plane("AC-THIN", margin_wh=-5)
    assert thin.energy.gate() is not None
    assert thin.energy.gate().code is RejectionCode.ENERGY_MARGIN_BELOW_RESERVE
    no_recovery = plane("AC-NOREC", recovery=False)
    assert no_recovery.energy.gate().code is RejectionCode.RECOVERY_NOT_FEASIBLE
    assert plane("AC-GOOD").energy.gate() is None


def test_preflight_rejects_restricted_and_unavailable_and_records_both() -> None:
    world = WorldView(
        "policy-v1",
        "sha256:" + "0" * 64,
        "digest",
        0,
        restricted_keys=frozenset({"corridor:C-99"}),
        unavailable=(("pad:P-9", "outage"),),
    )
    restricted = candidate("c-1-restricted", "AC-1", (claim("corridor", "C-99", 0, 60_000_000),))
    outage = candidate("c-2-outage", "AC-1", (claim("pad", "P-9", 0, 60_000_000),))
    clean = candidate("c-3-clean", "AC-1", (claim("corridor", "C-01", 0, 60_000_000),))
    selection = preflight(make_command(world=world, candidates=(clean, outage, restricted)))
    assert isinstance(selection, Selection)
    assert selection.candidate.candidate_id == "c-3-clean", "tried in rank order, not submission order"
    assert {item.candidate_id for item in selection.rejected} == {"c-1-restricted", "c-2-outage"}
    assert {item.code for item in selection.rejected} == {
        RejectionCode.CANDIDATE_RESTRICTED,
        RejectionCode.RESOURCE_UNAVAILABLE,
    }


def test_preflight_reports_an_unfrozen_world_before_touching_anything() -> None:
    failure = preflight(make_command(world=WorldView("p", "sha256:" + "0" * 64, "", 0)))
    assert isinstance(failure, PreflightFailure)
    assert failure.rejection.code is RejectionCode.SNAPSHOT_NOT_FROZEN


def test_preflight_with_no_eligible_aircraft_is_a_reasoned_rejection() -> None:
    failure = preflight(make_command(aircraft=(plane("AC-X", status=AircraftStatus.MAINTENANCE),)))
    assert isinstance(failure, PreflightFailure)
    assert failure.rejection.code is RejectionCode.NO_ELIGIBLE_AIRCRAFT
    assert failure.rejected[0].code is RejectionCode.AIRCRAFT_NOT_OPERABLE
    assert failure.rejected[-1] is failure.rejection


# ------------------------------------------------------------------ promotion (step 9/10)
def test_safe_promotes_and_every_other_answer_has_its_own_code() -> None:
    command = make_command()
    assert promotion(verdict_outcome(Verdict.SAFE), command) is None
    conflict = promotion(verdict_outcome(Verdict.CONFLICT, ("measured contact",)), command)
    assert conflict is not None and conflict.code is RejectionCode.CHECKER_CONFLICT
    unproven = promotion(verdict_outcome(Verdict.UNPROVEN, ("no reference",)), command)
    assert unproven is not None and unproven.code is RejectionCode.CHECKER_UNPROVEN


def test_an_allow_rule_needs_both_sides_naming_the_same_version() -> None:
    granted = verdict_outcome(Verdict.CONFLICT, ("within envelope",), ("AR-014", "3"))
    assert promotion(granted, make_command(allow_rule_id="AR-014", allow_rule_version="3")) is None
    mismatched = promotion(granted, make_command(allow_rule_id="AR-014", allow_rule_version="2"))
    assert mismatched is not None and mismatched.code is RejectionCode.ALLOW_RULE_UNMATCHED
    unnamed = promotion(granted, make_command())
    assert unnamed is not None and unnamed.code is RejectionCode.CHECKER_CONFLICT
    half_named = promotion(granted, make_command(allow_rule_id="AR-014", allow_rule_version=None))
    assert half_named is not None and half_named.code is RejectionCode.CHECKER_CONFLICT


def test_a_named_rule_never_overrides_unproven() -> None:
    """The exit criterion read literally: an exception and the checker must *agree*, and
    an exception cannot agree with an answer the checker never gave."""
    blocked = promotion(
        verdict_outcome(Verdict.UNPROVEN, ("expired restriction data",), ("AR-014", "3")),
        make_command(allow_rule_id="AR-014", allow_rule_version="3"),
    )
    assert blocked is not None and blocked.code is RejectionCode.CHECKER_UNPROVEN


# ------------------------------------------------------------------ I3: state and uncertainty
def test_unresolved_protects_space_but_is_not_a_committed_overlap() -> None:
    """Section 19's telemetry-loss row, as a table of decisions."""
    assert blocks_new_commitment(ReservationState.UNRESOLVED) is True
    assert ReservationState.UNRESOLVED.value in protected_states()
    assert may_be_released(ReservationState.UNRESOLVED) is False
    assert may_be_released(ReservationState.ACTIVATED) is False
    assert may_be_released(ReservationState.TENTATIVE) is True
    race = resolve_cancel_race(ReservationState.UNRESOLVED)
    assert race is not None and race.code is RejectionCode.CANCEL_REFUSED_ALREADY_COMMITTED
    assert resolve_cancel_race(ReservationState.TENTATIVE) is None


def test_the_reservation_machine_refuses_authority_skips() -> None:
    assert_transition("tentative", "committed")
    assert_transition("committed", "unresolved")
    assert_transition("unresolved", "cancelled")
    for illegal in (("tentative", "activated"), ("completed", "committed"), ("cancelled", "committed")):
        with pytest.raises(ReservationTransitionError):
            assert_transition(*illegal)


def test_the_ledger_cannot_activate_a_mission_from_planning() -> None:
    """``planning -> activated`` is impossible, and the ledger's own path agrees with
    ``core_domain``'s transition table on every hop it uses."""
    with pytest.raises(ReservationTransitionError):
        assert_mission_can_be_accepted(MissionStatus.ACTIVATED)
    path = mission_commit_path()
    assert path == (
        MissionStatus.DRAFT,
        MissionStatus.ELIGIBLE,
        MissionStatus.PLANNING,
        MissionStatus.RESERVED_PENDING_COMMIT,
        MissionStatus.ACCEPTED,
    )
    for source, target in zip(path, path[1:], strict=False):
        assert target in ALLOWED_TRANSITIONS[source], f"core_domain forbids {source} -> {target}"
    assert MissionStatus.ACTIVATED not in ALLOWED_TRANSITIONS[MissionStatus.PLANNING]


# ------------------------------------------------------------------ trace evidence
def test_the_input_hash_is_canonical_and_the_trace_parses_back() -> None:
    command = make_command()
    first = input_hash(command, {"policy_version": "policy-v1"})
    assert len(first) == 64 and all(item in "0123456789abcdef" for item in first)
    assert first == input_hash(command, {"policy_version": "policy-v1"})
    assert first != input_hash(make_command(commitment_token="token-2"), {"policy_version": "policy-v1"})

    selection = preflight(command)
    assert isinstance(selection, Selection)
    trace = DecisionTrace(
        command=command,
        input_hash=first,
        reason_codes=(RejectionCode.COMMITTED,),
        steps=(),
        ranking=selection.ranking_rows(),
        rejections=selection.rejected,
        outcome=verdict_outcome(Verdict.SAFE),
        lock_order=selection.lock_order,
        reservation_version=3,
        committed=True,
    )
    eligible = json.loads(trace.eligible_json())
    rejected = json.loads(trace.rejected_json())
    assert eligible["candidates"][0]["aircraft_id"] == "AC-1"
    assert rejected["checker"]["verdict"] == "SAFE"
    assert rejected["lock_order"] == ["corridor:C-01"]
    assert json.loads(trace.event_payload([uuid.uuid4()]))["committed"] is True


def test_derived_identifiers_are_reproducible() -> None:
    """A retry re-derives the same row ids, which is what makes a duplicate a no-op."""
    assert deterministic_id("token", "mission") == deterministic_id("token", "mission")
    assert deterministic_id("token", "mission") != deterministic_id("token2", "mission")


# ------------------------------------------------------------------ properties
@given(
    st.lists(
        st.tuples(
            st.sampled_from(KINDS),
            st.text(alphabet="ABCXYZ0123456789", min_size=1, max_size=8),
            st.integers(min_value=0, max_value=10_000),
            st.integers(min_value=1, max_value=10_000),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_lock_order_is_total_idempotent_and_independent_of_input_order(items) -> None:
    claims = tuple(claim(kind, identifier, start, start + length) for kind, identifier, start, length in items)
    ordered = in_lock_order(claims)
    assert in_lock_order(ordered) == ordered
    assert in_lock_order(tuple(reversed(claims))) == ordered
    assert len(set(ordered)) == len(ordered)
    keys = [item.key.lock_token() for item in ordered]
    assert keys == sorted(keys)


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=1, max_value=5_000))
def test_overlap_is_symmetric_and_adjacency_is_not_overlap(start: int, length: int) -> None:
    left = claim("volume_cell", "V-1", start, start + length)
    right = claim("volume_cell", "V-1", start + length, start + 2 * length)
    assert not left.conflicts_with(right) and not right.conflicts_with(left)
    touching = claim("volume_cell", "V-1", start, start + length + 1)
    assert left.conflicts_with(touching) and touching.conflicts_with(left)


# ------------------------------------------------------------------ small builders
def plane(
    aircraft_id: str,
    *,
    status: AircraftStatus = AircraftStatus.AVAILABLE,
    link: LinkQuality = LinkQuality.GOOD,
    lateness_us: int = 0,
    margin_wh: int = 90,
    recovery: bool = True,
) -> AircraftCandidate:
    return AircraftCandidate(
        aircraft_id=aircraft_id,
        status=status,
        link=link,
        priority_class=PriorityClass.ROUTINE,
        energy=EnergyVerdict(200, 200 - margin_wh - 30, 30, margin_wh, recovery, "model-v1"),
        lateness_us=lateness_us,
        disturbance_units=0,
        fairness_units=0,
    )


def candidate(candidate_id: str, aircraft_id: str, claims: tuple[ResourceClaim, ...]) -> CandidatePlan:
    return CandidatePlan(
        candidate_id=candidate_id,
        aircraft_id=aircraft_id,
        strategy="direct",
        route_digest="a" * 64,
        claims=claims,
        profile=(
            ProfileSample(NOW, 30.7, 76.7, 60.0, AltitudeReference.AGL, 10_000),
            ProfileSample(NOW + timedelta(minutes=1), 30.72, 76.72, 60.0, AltitudeReference.AGL, 10_000),
        ),
    )


def holder(resource_key_value: str, state: str, start: int, end: int) -> ProtectedHolding:
    return ProtectedHolding(
        resource_key=resource_key_value,
        mission_id=uuid.uuid4(),
        state=state,
        valid_from=datetime.fromtimestamp(start / 1_000_000, UTC),
        valid_until=datetime.fromtimestamp(end / 1_000_000, UTC),
    )
