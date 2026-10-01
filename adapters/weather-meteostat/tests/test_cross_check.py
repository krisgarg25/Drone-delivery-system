"""The Meteostat adapter cross-checks a frozen trace and decides nothing.

It is deliberately the weakest package in the energy path: it can say "the second
source disagrees with the trace you are replaying" and nothing else. These tests
pin that, plus the unit conversion both adapters must share.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from droneops_adapters_weather_meteostat import (
    CrossCheckResult,
    StationReport,
    Verdict,
    cross_check_snapshot,
    report_from_payload,
)

from droneops_energy import Freshness, snapshot_from_rows
from droneops_energy.errors import EnergyError

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "droneops_adapters_weather_meteostat"
RETRIEVED_US = 1_759_283_200_000_000


def snapshot(speed_dmps: int, minutes: int = 6):
    return snapshot_from_rows(
        [(m, speed_dmps, 90_000, speed_dmps) for m in range(minutes)],
        station="IXC-synthetic",
        source="frozen test trace",
        bundle_sha256="d" * 64,
        retrieved_at_us=RETRIEVED_US,
        expires_at_us=RETRIEVED_US + 60 * 60 * 1_000_000,
    )


def report(speeds: list[int], *, station: str = "VCG-1") -> StationReport:
    return StationReport.from_rows(
        list(enumerate(speeds)),
        station=station,
        source="meteostat test report",
        retrieved_at_us=RETRIEVED_US,
    )


def test_kilometres_per_hour_become_the_same_integer_units_as_the_forecast() -> None:
    """18 km/h freezes to 50 dm/s in both adapters, or the comparison is meaningless."""
    frozen = report_from_payload(
        {"hourly": {"wspd": [18.0, 21.6]}}, station="VCG-1", retrieved_at_us=RETRIEVED_US
    )

    assert [speed for _minute, speed in frozen.speeds_dmps] == [50, 60]
    assert frozen.speeds_dmps[0][0] == 0
    assert frozen.speeds_dmps[1][0] == 60


def test_agreement_drift_and_contradiction_are_three_visible_answers() -> None:
    agrees = cross_check_snapshot(snapshot(50), report([50, 51, 52, 50, 49, 50]), tolerance_dmps=20)
    drifts = cross_check_snapshot(snapshot(50), report([70, 71, 70, 70, 70, 70]), tolerance_dmps=20)
    contradicts = cross_check_snapshot(
        snapshot(50), report([200, 200, 200, 200, 200, 200]), tolerance_dmps=20
    )

    assert agrees.verdict is Verdict.AGREES
    assert agrees.blocks_review is False
    assert agrees.minutes_compared == 6
    assert drifts.verdict is Verdict.DRIFTS
    assert contradicts.verdict is Verdict.CONTRADICTS
    assert contradicts.max_deviation_dmps == 150
    assert contradicts.summary.startswith("contradicts")
    assert all(row.blocks_review for row in (drifts, contradicts))


def test_a_second_source_that_covers_nothing_is_reported_as_no_overlap() -> None:
    """Silence is not agreement: a gap must be visible to a reviewer."""
    empty = StationReport.from_rows(
        [(900, 50)],
        station="far-away",
        source="meteostat test report",
        retrieved_at_us=RETRIEVED_US,
    )
    result = cross_check_snapshot(snapshot(50), empty)

    assert result.verdict is Verdict.NO_OVERLAP
    assert result.minutes_compared == 0
    assert result.blocks_review is True


def test_the_cross_check_never_mutates_the_snapshot_it_checked() -> None:
    """Adapters may observe; they may not edit the frozen basis of a run."""
    original = snapshot(50)
    before = original.samples
    cross_check_snapshot(original, report([900, 900, 900, 900, 900, 900]))

    assert original.samples == before
    assert original.freshness(RETRIEVED_US) is Freshness.FRESH


def test_a_station_report_can_never_claim_authority() -> None:
    with pytest.raises(EnergyError, match="never authoritative"):
        StationReport(
            station="VCG-1",
            source="meteostat",
            speeds_dmps=((0, 50),),
            retrieved_at_us=RETRIEVED_US,
            authoritative=True,
        )
    with pytest.raises(EnergyError, match="ascending"):
        StationReport(
            station="VCG-1",
            source="meteostat",
            speeds_dmps=((5, 50), (2, 50)),
            retrieved_at_us=RETRIEVED_US,
        )
    with pytest.raises(EnergyError, match="repeats a minute"):
        StationReport(
            station="VCG-1",
            source="meteostat",
            speeds_dmps=((1, 50), (1, 60)),
            retrieved_at_us=RETRIEVED_US,
        )
    with pytest.raises(EnergyError, match="tolerance cannot be negative"):
        cross_check_snapshot(snapshot(50), report([50]), tolerance_dmps=-1)
    with pytest.raises(EnergyError, match="no wind-speed list"):
        report_from_payload({"hourly": {}}, station="VCG-1", retrieved_at_us=RETRIEVED_US)


def test_results_are_plain_frozen_values() -> None:
    result = cross_check_snapshot(snapshot(50), report([50, 50, 50, 50, 50, 50]))
    assert isinstance(result, CrossCheckResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.verdict = Verdict.AGREES  # type: ignore[misc]


NETWORK_MODULES = {"httpx", "socket", "urllib", "requests", "ssl", "aiohttp", "subprocess"}


@pytest.mark.parametrize("path", sorted(SRC_ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_the_cross_check_adapter_has_no_network_import(path: Path) -> None:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    assert not found & NETWORK_MODULES, f"{path.name} imports {found & NETWORK_MODULES}"
    assert not {m for m in found if m.startswith("droneops_energy.")} - {
        "droneops_energy.errors"
    }, f"{path.name} reaches inside the kernel: {found}"
