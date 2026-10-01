"""Capacity as a schedulable resource: the section 11 exposure and its rejections.

Proves that a capacity profile carries every field ``technical-build/01`` section
11 lists, that the shipped synthetic bundle is the source of the numbers, that
handover outside land-transfer cannot be expressed, and that an outage's two
halves - refuse new work, keep committed work - are separately visible.

What this does not prove: that any declared pad count, crew shift or charge power
matches a real site. The bundle is synthetic and marked non-authoritative; real
sites need survey records.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

import pytest
from facilities_support import POLICY, SECOND_US, make_claim, make_profile

from droneops_contracts.facilities import FacilityStatus, HandoverType
from droneops_facilities import (
    Accessibility,
    BundleLoadError,
    CapacityError,
    ClaimPurpose,
    HandoverTypeError,
    OutageState,
    OutageWindow,
    ResourceClaim,
    TurnaroundDurations,
    audit_claims,
    load_capacity_profiles,
    minutes_to_us,
    profile_in_outage,
    require_supported_handover,
    scenario_day,
    unit_resources,
)
from droneops_geom_core import Interval, ResourceKind

pytestmark = pytest.mark.unit


def test_capacity_view_exposes_every_section_11_field() -> None:
    view = make_profile("hub-central", pads=4, charger_bays=6, crew_slots=2).capacity_view(
        Interval.of(0, 60 * 60 * SECOND_US)
    )
    fields = {field.name for field in dataclasses.fields(view)}
    assert {
        "opening_times",
        "simultaneous_pads",
        "charger_bays",
        "crew_slots",
        "turnaround_us",
        "loading_us",
        "accessibility",
        "policy_version",
        "outage_state",
    } <= fields
    assert view.simultaneous_pads == 4 and view.charger_bays == 6
    assert view.accessibility.handover_type is HandoverType.LAND_TRANSFER
    assert view.policy_version == POLICY


def test_capacity_view_metrics_are_key_sorted_and_stable() -> None:
    view = make_profile("hub-metrics").capacity_view(Interval.of(0, 60 * 60 * SECOND_US))
    first = view.as_metrics()
    assert list(first) == sorted(first)
    assert first["turnaround_us"] == 80 * SECOND_US
    assert first["handover_type"] == "land_transfer"
    assert first["authoritative"] == 0


def test_pad_and_charger_and_crew_counts_are_independent() -> None:
    """The section 11 point: capacity without a pad, a pad without a pilot."""
    rich_no_pad = make_profile("hub-no-pad", pads=0, charger_bays=4, crew_slots=2)
    pad_no_crew = make_profile("hub-no-crew", pads=3, charger_bays=2, crew_slots=0)
    assert rich_no_pad.charger_bays == 4 and rich_no_pad.pad_count == 0
    assert pad_no_crew.pad_count == 3 and pad_no_crew.crews() == ()
    audit = audit_claims(
        (), facility_id="hub-no-crew", pad_capacity=3, charger_capacity=2, crew_capacity=0
    )
    assert audit.crew.capacity == 0 and audit.i6_holds


def test_unit_resources_are_addressed_by_canonical_keys() -> None:
    units = unit_resources(ResourceKind.PAD, "hub-central-synthetic", 3)
    windows = Interval.of(0, 80 * SECOND_US)
    keys = [unit.key(windows).canonical for unit in units]
    assert keys == [
        f"pad:pad-hub-central-synthetic-{index}:0..{80 * SECOND_US}" for index in range(3)
    ]
    assert units[0].identity_token() == "pad:pad-hub-central-synthetic-0"


def test_winch_and_parachute_handover_are_rejected() -> None:
    for banned in (HandoverType.WINCH, HandoverType.PARACHUTE):
        with pytest.raises(HandoverTypeError):
            require_supported_handover(banned)
        with pytest.raises(HandoverTypeError):
            # The ban is enforced where the capacity is declared, not at the
            # scheduler: an unsupported site cannot even be described.
            base = make_profile(f"hub-banned-{banned.value}")
            dataclasses.replace(
                base,
                accessibility=Accessibility(
                    handover_type=banned, step_free=True, ground_pickup=True, requires_crew=True
                ),
            )


def test_land_transfer_is_the_only_supported_handover() -> None:
    assert require_supported_handover(HandoverType.LAND_TRANSFER) is HandoverType.LAND_TRANSFER


def test_zero_length_turnaround_is_refused() -> None:
    with pytest.raises(CapacityError):
        TurnaroundDurations(approach_us=0, landing_us=0, handover_us=0, loading_us=0, departure_us=0)


def test_empty_claim_is_refused_because_it_reserves_nothing() -> None:
    unit = make_profile().pads()[0]
    with pytest.raises(CapacityError, match="empty"):
        ResourceClaim(
            request_id="req-empty",
            holder="aircraft-1",
            facility_id="hub-test",
            unit=unit,
            window=Interval.of(0, 0),
            purpose=ClaimPurpose.PAD,
        )


def test_a_claim_cannot_sit_on_the_wrong_kind_of_unit() -> None:
    pad_unit = make_profile().pads()[0]
    with pytest.raises(CapacityError, match="does not match unit kind"):
        ResourceClaim(
            request_id="req-mismatch",
            holder="aircraft-1",
            facility_id="hub-test",
            unit=pad_unit,
            window=Interval.of(0, 60 * SECOND_US),
            purpose=ClaimPurpose.CREW,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("pads", -1), ("charger_bays", -2), ("crew_slots", -1)],
)
def test_negative_capacity_is_a_data_error(field: str, value: int) -> None:
    with pytest.raises(CapacityError):
        make_profile("hub-negative", **{field: value})


# --- the shipped bundle ----------------------------------------------------


@pytest.fixture(scope="module")
def bundle_document() -> Mapping[str, object]:
    from facilities_support import load_bundle_document

    return load_bundle_document()


def test_bundle_loads_every_declared_site(bundle_document: Mapping[str, object]) -> None:
    profiles = load_capacity_profiles(bundle_document)
    assert len(profiles) == len(bundle_document["facilities"])  # type: ignore[arg-type]
    ids = {profile.facility_id for profile in profiles}
    assert {"hub-central-synthetic", "node-rimroad-synthetic", "node-lakeview-synthetic"} <= ids
    assert {profile.authoritative for profile in profiles} == {False}


def test_bundle_capacity_matches_the_declared_numbers(
    bundle_document: Mapping[str, object],
) -> None:
    profiles = {profile.facility_id: profile for profile in load_capacity_profiles(bundle_document)}
    central = profiles["hub-central-synthetic"]
    rows = {str(row["facility_id"]): row for row in bundle_document["facilities"]}  # type: ignore[union-attr,index]
    row = rows["hub-central-synthetic"]
    assert central.pad_count == row["pads"] == 4
    assert central.charger_bays == row["charger_bays"] == 6
    assert central.loading_us == minutes_to_us(row["loading_turnaround_min"]) == 2 * 60 * SECOND_US
    assert central.offload_us == minutes_to_us(row["offload_turnaround_min"]) == 60 * SECOND_US
    assert central.opens_us == 0 and central.closes_us == 60 * 60 * SECOND_US
    assert central.charge_power_w == 1200


def test_bundle_encodes_the_declared_hub_outage(bundle_document: Mapping[str, object]) -> None:
    profiles = {profile.facility_id: profile for profile in load_capacity_profiles(bundle_document)}
    east = profiles["hub-ridge-east-synthetic"]
    assert len(east.outages) == 1
    outage = east.outages[0]
    assert (outage.start_us, outage.end_us) == (41 * 60 * SECOND_US, 49 * 60 * SECOND_US)
    # The bundle's own token is preserved, not normalised away: this outage is
    # declared as a closure, and a schedule must be able to tell the two apart.
    assert outage.state is FacilityStatus.CLOSED
    assert east.outage_state_at(45 * 60 * SECOND_US) is OutageState.OUTAGE
    assert east.outage_state_at(50 * 60 * SECOND_US) is OutageState.OPEN
    # The outage splits the working day: nothing new may start inside it.
    usable = east.usable_windows(Interval.of(40 * 60 * SECOND_US, 50 * 60 * SECOND_US))
    assert usable == (
        Interval.of(40 * 60 * SECOND_US, 41 * 60 * SECOND_US),
        Interval.of(49 * 60 * SECOND_US, 50 * 60 * SECOND_US),
    )


def test_bundle_shift_gap_is_visible_as_a_crew_window(
    bundle_document: Mapping[str, object],
) -> None:
    profiles = {profile.facility_id: profile for profile in load_capacity_profiles(bundle_document)}
    lakeview = profiles["node-lakeview-synthetic"]
    assert lakeview.crew_shifts == (
        Interval.of(0, 25 * 60 * SECOND_US),
        Interval.of(30 * 60 * SECOND_US, 60 * 60 * SECOND_US),
    )
    day = scenario_day(tuple(profiles.values()))
    crews = lakeview.crew_windows(day)
    assert all(not window.is_empty for window in crews)
    # minute 27 sits in the staffing gap: no crewed stage may run there.
    assert not any(window.contains_instant(27 * 60 * SECOND_US) for window in crews)


def test_bundle_without_authority_marker_is_refused(bundle_document: Mapping[str, object]) -> None:
    stripped = {key: value for key, value in bundle_document.items() if key != "authoritative"}
    with pytest.raises(BundleLoadError, match="authority"):
        load_capacity_profiles(stripped)


def test_bundle_asking_for_a_winch_is_refused(bundle_document: Mapping[str, object]) -> None:
    poisoned = dict(bundle_document)
    poisoned["facilities"] = [{**dict(row), "handover_type": "winch"} for row in bundle_document["facilities"]]  # type: ignore[union-attr,index]
    with pytest.raises(BundleLoadError, match="banned"):
        load_capacity_profiles(poisoned)


def test_an_unknown_handover_token_is_a_load_error(bundle_document: Mapping[str, object]) -> None:
    poisoned = dict(bundle_document)
    poisoned["facilities"] = [
        {**dict(row), "handover_type": "midair_grapple"} for row in bundle_document["facilities"]  # type: ignore[union-attr,index]
    ]
    with pytest.raises(BundleLoadError, match="unknown handover"):
        load_capacity_profiles(poisoned)


# --- outage and policy version ---------------------------------------------


def test_outage_impact_blocks_new_and_keeps_committed() -> None:
    profile = profile_in_outage(make_profile("hub-out"), OutageWindow(40 * 60 * SECOND_US, 48 * 60 * SECOND_US))
    held = make_claim("req-held", facility_id="hub-out", index=0, start_s=41 * 60, end_s=43 * 60)
    impact = profile.impact_of_outage(42 * 60 * SECOND_US, (held,))
    assert impact.state is OutageState.OUTAGE
    assert impact.blocks_new_claims is True
    assert impact.committed_claims_preserved is True
    assert impact.requires_reconciliation is True
    assert [claim.request_id for claim in impact.affected_claims] == ["req-held"]
    assert impact.as_metrics()["committed_claims_preserved"] is True


def test_committed_claims_preserved_cannot_be_forged() -> None:
    """No constructor argument can make an outage impact drop committed work."""
    from droneops_facilities import OutageImpact

    with pytest.raises(TypeError):
        OutageImpact(
            "hub-out",
            OutageState.OUTAGE,
            blocks_new_claims=True,
            requires_reconciliation=True,
            committed_claims_preserved=False,  # type: ignore[arg-type]
        )


def test_outage_outside_the_window_changes_nothing() -> None:
    profile = make_profile("hub-quiet")
    impact = profile.impact_of_outage(60 * SECOND_US, ())
    assert impact.state is OutageState.OPEN
    assert impact.blocks_new_claims is False and impact.requires_reconciliation is False


def test_profile_in_outage_supersedes_instead_of_editing() -> None:
    original = make_profile("hub-immutable")
    changed = profile_in_outage(original, OutageWindow(10 * SECOND_US, 20 * SECOND_US))
    assert original.outages == ()
    assert original.status is FacilityStatus.ACTIVE
    assert changed.status is FacilityStatus.OUTAGE
    assert changed.outages[0].start_us == 10 * SECOND_US
