"""The airspace-import adapter: records observations, invents nothing, decides nothing.

The interesting assertions are the negative ones: no endpoint exists to call, a missing
feed is an explicit state rather than an empty list, an undeclared freshness is preserved
as ``unknown`` (so the validity layer fails closed) instead of being filled in as ``fresh``,
the fetch time recorded is this run's own rather than one copied from the file, and a file
cannot promote itself to an authoritative feed.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from droneops_adapters_airspace_import import (
    NO_FEED_MESSAGE,
    AirspaceImportError,
    LocalFileRestrictionSource,
    NoAuthorisedFeedConfigured,
    NoAuthorisedFeedError,
    SnapshotValidationError,
    default_source,
    import_from_file,
    import_snapshot,
)

from droneops_airspace import (
    EligibilityVerdict,
    RestrictionKind,
    RouteProfile,
    RouteWaypoint,
    evaluate_route,
)
from droneops_contracts.geometry import AltitudeReference
from droneops_contracts.provenance import Freshness

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
EXAMPLE = Path(__file__).parent / "data" / "manual-restrictions.yaml"
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "droneops_adapters_airspace_import"

BASE_ENTRY: dict[str, object] = {
    "restriction_id": "only-one",
    "kind": "red",
    "shape": "polygon",
    "points": [[30.73, 76.78], [30.73, 76.8], [30.75, 76.8], [30.75, 76.78]],
    "floor_m": 0,
    "ceiling_m": 120,
    "altitude_reference": "AGL",
    "effective_from_utc": "2026-10-01T10:00:00+00:00",
    "effective_until_utc": "2026-10-02T10:00:00+00:00",
    "provenance": {
        "source": "operator-manual-entry",
        "retrieved_at_utc": "2026-10-01T09:00:00+00:00",
        "expires_at_utc": "2026-10-02T09:00:00+00:00",
        "freshness": "fresh",
    },
}


def document(**overrides: object) -> str:
    """One-entry restriction document with the named fields replaced or deleted."""

    entry = dict(BASE_ENTRY)
    payload: dict[str, object] = {
        "schema_version": 1,
        "source": "operator file",
        "version": "v1",
        "availability": "manual_operator_file",
        "restrictions": [entry],
    }
    for key, value in overrides.items():
        if key in payload:
            payload[key] = value
        elif value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return json.dumps(payload)


def write(tmp_path: Path, text: str, name: str = "restrictions.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def crossing_route() -> RouteProfile:
    """A three-waypoint path that crosses the example red sector twice."""

    return RouteProfile(
        plan_id="plan-1",
        planned_start_utc=NOW,
        waypoints=tuple(
            RouteWaypoint(
                lat=lat,
                lon=lon,
                altitude_m=80.0,
                altitude_reference=AltitudeReference.AGL,
                t_s=index * 300,
            )
            for index, (lat, lon) in enumerate(((30.72, 76.77), (30.74, 76.79), (30.76, 76.81)))
        ),
    )


def test_the_default_source_says_there_is_no_feed() -> None:
    assert isinstance(default_source(), NoAuthorisedFeedConfigured)
    with pytest.raises(NoAuthorisedFeedError, match="No authorised"):
        default_source().read()


def test_importing_the_default_yields_an_explicit_no_feed_snapshot() -> None:
    result = import_snapshot(default_source(), now=NOW)
    assert result.snapshot.restrictions == ()
    assert result.snapshot.availability.value == "no_feed_configured"
    assert "ignorance, not clear airspace" in result.snapshot.note
    assert result.snapshot.assess(NOW).data_usable is False
    assert result.warnings == (result.snapshot.note,)


def test_a_no_feed_snapshot_blocks_a_route_rather_than_clearing_it() -> None:
    result = import_snapshot(None, now=NOW)
    verdict = evaluate_route(crossing_route(), result.snapshot, now=NOW)
    assert verdict.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA
    assert verdict.launch_permitted is False


def test_a_missing_file_is_not_reported_as_an_empty_sky(tmp_path: Path) -> None:
    with pytest.raises(NoAuthorisedFeedError, match="does not exist"):
        import_snapshot(LocalFileRestrictionSource(tmp_path / "absent.json"), now=NOW)


def test_a_local_file_becomes_a_normalised_snapshot(tmp_path: Path) -> None:
    path = write(tmp_path, document())
    result = import_from_file(path, now=NOW)
    snapshot = result.snapshot
    assert len(snapshot.restrictions) == 1
    (one,) = snapshot.restrictions
    assert one.kind is RestrictionKind.RED
    assert one.authoritative is False
    assert one.provenance.source == "operator-manual-entry"
    assert one.footprint.points[0] == one.footprint.points[-1], "open ring closed"
    assert snapshot.source == "operator file"
    assert snapshot.feed_version == "v1"
    assert snapshot.content_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot.snapshot_id == f"airspace-{snapshot.content_sha256[:12]}"
    assert snapshot.assess(NOW).data_usable is True


def test_fetch_time_is_this_run_not_a_timestamp_copied_from_the_file(tmp_path: Path) -> None:
    result = import_from_file(write(tmp_path, document()), now=NOW)
    assert result.snapshot.fetched_at == NOW
    (one,) = result.snapshot.restrictions
    assert one.provenance.retrieved_at == datetime(2026, 10, 1, 9, 0, tzinfo=UTC), (
        "the entry keeps the source's own retrieval time"
    )


def test_a_fetch_that_has_aged_out_is_visible_as_stale(tmp_path: Path) -> None:
    result = import_from_file(write(tmp_path, document()), now=NOW)
    assert result.snapshot.assess(NOW).data_usable is True
    assert result.snapshot.assess(NOW + timedelta(days=2)).data_usable is False


def test_the_example_file_imports_and_exercises_every_kind() -> None:
    result = import_from_file(EXAMPLE, now=NOW)
    assert len(result.snapshot.restrictions) == 3
    kinds = {item.kind.value for item in result.snapshot.restrictions}
    assert {"red", "airport_vicinity", "temporary_closure"} <= kinds
    assert "closed automatically" in " ".join(result.warnings)
    stale = next(
        item
        for item in result.snapshot.restrictions
        if item.restriction_id == "example-stale-closure"
    )
    assert stale.provenance.freshness is Freshness.STALE
    assert stale.authoritative is False


def test_the_imported_file_blocks_a_route_and_a_stale_copy_fails_closed() -> None:
    result = import_from_file(EXAMPLE, now=NOW)
    verdict = evaluate_route(crossing_route(), result.snapshot, now=NOW)
    assert verdict.verdict is EligibilityVerdict.BLOCKED_NOW
    stale = evaluate_route(
        crossing_route(),
        result.snapshot.model_copy(update={"fetched_at": NOW - timedelta(days=4)}),
        now=NOW,
    )
    assert stale.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA


def test_an_entry_without_declared_freshness_keeps_its_gap(tmp_path: Path) -> None:
    text = document(provenance=None)
    result = import_from_file(write(tmp_path, text), now=NOW)
    (one,) = result.snapshot.restrictions
    assert one.provenance.freshness is Freshness.UNKNOWN
    assert one.provenance.source == "file-entry:only-one"
    assert "freshness unknown" in " ".join(result.warnings)
    verdict = evaluate_route(crossing_route(), result.snapshot, now=NOW)
    assert verdict.verdict is EligibilityVerdict.NO_LAUNCH_INSUFFICIENT_DATA


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (json.dumps({"schema_version": 1, "source": "x"}), "restrictions' list"),
        (json.dumps({"schema_version": 9, "restrictions": []}), "not understood"),
        ("just a sentence", "mapping"),
    ],
)
def test_unusable_documents_are_refused(tmp_path: Path, payload: str, message: str) -> None:
    with pytest.raises(SnapshotValidationError, match=message):
        import_from_file(write(tmp_path, payload), now=NOW)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kind": "no-such-zone"}, r"kind must be one of"),
        ({"altitude_reference": "UNSPECIFIED"}, r"I9"),
        ({"altitude_reference": None}, r"missing required key"),
        ({"effective_from_utc": "2026-10-01T10:00:00"}, r"I10"),
        ({"shape": "metaball"}, r"polygon, circle or buffered_line"),
        ({"restriction_id": "x"}, r"at least 3 characters"),
        ({"points": "not a list"}, r"non-empty list"),
        ({"shape": "circle", "points": [[30.73, 76.78], [30.74, 76.79]]}, r"one centre point"),
    ],
)
def test_bad_entries_are_named_not_silently_dropped(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(Exception, match=message):
        import_from_file(write(tmp_path, document(**overrides)), now=NOW)


def test_a_file_cannot_promote_itself_to_an_authoritative_feed(tmp_path: Path) -> None:
    with pytest.raises(SnapshotValidationError, match="contradicts"):
        import_from_file(write(tmp_path, document(availability="authoritative_feed")), now=NOW)


def test_an_authoritative_source_label_cannot_be_satisfied_in_phase_zero(tmp_path: Path) -> None:
    path = write(tmp_path, document(availability="authoritative_feed"))
    with pytest.raises(Exception, match="authoritative"):
        import_snapshot(
            LocalFileRestrictionSource(path, availability_label="authoritative_feed"), now=NOW
        )


def test_the_adapter_does_not_touch_a_reservation_or_the_network() -> None:
    forbidden = (
        "droneops_reservations",
        "droneops_planner",
        "droneops_dispatcher",
        "httpx",
        "requests",
        "socket",
        "urllib",
    )
    offenders: list[str] = []
    for source in sorted(SRC_ROOT.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            offenders += [
                f"{source.name}: {name}"
                for name in modules
                for forbidden_name in forbidden
                if name.startswith(forbidden_name)
            ]
    assert offenders == []


def test_no_live_digitalsky_endpoint_is_hardcoded_anywhere() -> None:
    text = " ".join(path.read_text(encoding="utf-8") for path in sorted(SRC_ROOT.glob("*.py")))
    assert "digitalsky.gov.in" not in text.lower()
    assert "https://" not in text, "a URL in this package would be an invented integration"
    assert "no authorised" in NO_FEED_MESSAGE.lower()


def test_every_import_error_derives_from_one_base() -> None:
    assert issubclass(NoAuthorisedFeedError, AirspaceImportError)
    assert issubclass(SnapshotValidationError, AirspaceImportError)
