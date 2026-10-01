"""The Open-Meteo adapter must produce a frozen snapshot and be unable to fetch.

The deterministic-run guarantee is checked three ways: a source scan for network
imports, a live-socket refusal wrapped around a full energy evaluation driven by
the committed scenario trace, and the structural fact that the live path needs an
injected transport plus a named authorisation that a simulation cannot create.
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest
import yaml
from droneops_adapters_weather_open_meteo import (
    LiveFetchAuthorisation,
    request_snapshot,
    snapshot_from_open_meteo,
    snapshot_from_payload,
    snapshot_from_scenario_trace,
    snapshot_url,
)

from droneops_energy import Freshness, assess_mission
from droneops_energy.errors import EnergyError

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_WEATHER = (
    REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1" / "weather.yaml"
)
SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "droneops_adapters_weather_open_meteo"

#: A payload in Open-Meteo's own shape: hourly km/h, degrees "from", gusts.
PAYLOAD = {
    "hourly": {
        "time": ["2026-10-01T00:00", "2026-10-01T01:00", "2026-10-01T02:00"],
        "wind_speed_10m": [18.0, 21.6, 36.0],
        "wind_direction_10m": [270.0, 270.0, 90.0],
        "wind_gusts_10m": [27.0, 32.4, 45.0],
    }
}
BUNDLE = "b" * 64
RETRIEVED_US = 1_759_283_200_000_000


def _frozen(**kwargs: object):
    return snapshot_from_open_meteo(
        PAYLOAD,
        station="IXC-synthetic",
        bundle_sha256=BUNDLE,
        retrieved_at_us=RETRIEVED_US,
        expires_at_us=RETRIEVED_US + 3 * 60 * 60 * 1_000_000,
        **kwargs,
    )


# ------------------------------------------------------------------- freezing
def test_payload_units_and_convention_are_fixed_once() -> None:
    """18 km/h is 5 m/s; a wind from due west blows due east into the triangle."""
    snapshot = _frozen()

    first = snapshot.samples[0]
    assert first.speed_dmps == 50
    assert first.gust_dmps == 75
    assert first.bearing_mdeg == 90_000, "from 270 deg becomes towards 090 deg"
    assert snapshot.samples[2].bearing_mdeg == 270_000
    assert snapshot.samples[0].minute == 0
    assert snapshot.samples[2].minute == 120
    assert snapshot.authoritative is False
    assert "open-meteo" in snapshot.source


def test_the_same_payload_bytes_always_freeze_to_the_same_snapshot() -> None:
    body = json.dumps(PAYLOAD)
    a = snapshot_from_payload(
        body, station="IXC", bundle_sha256=BUNDLE, retrieved_at_us=RETRIEVED_US
    )
    b = snapshot_from_payload(
        body.encode("utf-8"), station="IXC", bundle_sha256=BUNDLE, retrieved_at_us=RETRIEVED_US
    )

    assert a.samples == b.samples
    assert a.span_minutes == b.span_minutes


def test_a_ragged_or_missing_payload_is_refused() -> None:
    broken = {"hourly": {**PAYLOAD["hourly"], "wind_gusts_10m": [1.0]}}
    with pytest.raises(EnergyError, match="ragged"):
        snapshot_from_open_meteo(
            broken, station="IXC", bundle_sha256=BUNDLE, retrieved_at_us=RETRIEVED_US
        )
    with pytest.raises(EnergyError, match="no 'hourly'"):
        snapshot_from_open_meteo(
            {"hourly": None}, station="IXC", bundle_sha256=BUNDLE, retrieved_at_us=RETRIEVED_US
        )
    with pytest.raises(EnergyError, match="not JSON"):
        snapshot_from_payload(
            "not json", station="IXC", bundle_sha256=BUNDLE, retrieved_at_us=RETRIEVED_US
        )


# ------------------------------------------------- the committed scenario trace
def _scenario_rows() -> tuple[list[dict[str, int]], dict[str, object]]:
    document = yaml.safe_load(SCENARIO_WEATHER.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    trace = document["trace"]
    assert isinstance(trace, list)
    return trace, document


def test_the_committed_frozen_trace_replays_into_a_snapshot() -> None:
    """The scenario's own weather.yaml is the input a deterministic run uses."""
    trace, document = _scenario_rows()
    snapshot = snapshot_from_scenario_trace(
        trace,
        station=str(document["station"]),
        source=str(document["source"]),
        bundle_sha256="e" * 64,
        retrieved_at_us=RETRIEVED_US,
        expires_at_us=RETRIEVED_US + 60 * 60 * 1_000_000,
        reference_course_mdeg=90_000,
    )

    assert snapshot.samples[0].speed_dmps == 60, "the trace's minute 0 is 6 m/s"
    assert snapshot.freshness(RETRIEVED_US) is Freshness.FRESH
    assert document["authoritative"] is False
    # A 12 % headwind fraction cannot point the wind straight down the track.
    assert snapshot.samples[0].bearing_mdeg != 270_000
    assert snapshot.samples[0].gust_dmps == snapshot.samples[0].speed_dmps


def test_the_replayed_trace_drives_a_real_energy_decision() -> None:
    """End to end, no network: the trace the scenario ships with, priced by the kernel."""
    from droneops_energy import AIRFRAME_X_SIM, MissionEnergyRequest, Route, SpeedProfile

    trace, _document = _scenario_rows()
    snapshot = snapshot_from_scenario_trace(
        trace,
        station="IXC-synthetic",
        source="frozen scenario trace",
        bundle_sha256="e" * 64,
        retrieved_at_us=RETRIEVED_US,
        expires_at_us=RETRIEVED_US + 60 * 60 * 1_000_000,
    )

    def build(weather):
        return MissionEnergyRequest(
            mission_id="ord-scenario",
            payload_g=1000,
            outbound=Route.straight("outbound", 8_000_000, course_mdeg=90_000),
            handover=Route(label="handover", legs=()),
            recovery=Route.straight("recovery", 8_000_000, course_mdeg=270_000),
            weather=weather,
            profile=AIRFRAME_X_SIM,
            clock_us=RETRIEVED_US,
            speed=SpeedProfile(),
        )

    as_frozen = assess_mission(build(snapshot))
    reversed_trace = assess_mission(build(snapshot.reversed()))

    assert as_frozen.outbound.legs
    assert as_frozen.recovery is not None
    assert as_frozen.profile_sha256 == AIRFRAME_X_SIM.parameters_sha256
    assert isinstance(as_frozen.margin_wh, int)
    assert as_frozen.margin_wh != reversed_trace.margin_wh
    # The scenario trace carries no gust column, so no reserve uplift is invented.
    assert as_frozen.terms[-1].wh == 0


# ---------------------------------------------------------------- no live call
class _RefusingTransport:
    """A transport that proves it was never used by raising if it is."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, _params: dict[str, str]) -> dict[str, object]:
        self.calls += 1
        raise AssertionError(f"unexpected live fetch: {url}")


def test_the_live_path_needs_both_an_injected_transport_and_a_named_authorisation() -> None:
    """A simulation cannot build the authorisation object the live path requires."""
    with pytest.raises(EnergyError, match="may not authorise a live fetch"):
        LiveFetchAuthorisation(authorised_by="sim", purpose="replay", for_simulation=True)
    with pytest.raises(EnergyError, match="authoriser"):
        LiveFetchAuthorisation(authorised_by="", purpose="")

    real = LiveFetchAuthorisation(authorised_by="kris", purpose="bundle capture out of band")
    transport = _RefusingTransport()
    assert isinstance(transport, object)
    with pytest.raises(AssertionError, match="unexpected live fetch"):
        request_snapshot(
            transport,
            real,
            latitude="30.738",
            longitude="76.794",
            forecast_hours=3,
            station="IXC-synthetic",
            bundle_sha256=BUNDLE,
            retrieved_at_us=RETRIEVED_US,
        )
    assert transport.calls == 1, "the fetch is explicit, not implicit"


def test_a_frozen_run_finishes_with_every_socket_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the socket family and replay: if anything tried to fetch, it fails here."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the weather path opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    trace, _document = _scenario_rows()
    snapshot = snapshot_from_scenario_trace(
        trace,
        station="IXC-synthetic",
        source="frozen scenario trace",
        bundle_sha256="e" * 64,
        retrieved_at_us=RETRIEVED_US,
        expires_at_us=RETRIEVED_US + 60 * 60 * 1_000_000,
    )
    assert snapshot.samples


def test_the_url_builder_is_pure_and_pinned() -> None:
    url = snapshot_url(latitude="30.738", longitude="76.794", forecast_hours=3)

    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "wind_gusts_10m" in url
    assert "timezone=Asia/Kolkata" in url
    with pytest.raises(EnergyError, match="pinned contract"):
        snapshot_url(latitude="30.738", longitude="76.794", forecast_hours=0)


# --------------------------------------------------------------- source purity
NETWORK_MODULES = {"httpx", "socket", "urllib", "requests", "ssl", "aiohttp", "subprocess"}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("path", sorted(SRC_ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_no_freeze_or_scenario_path_can_reach_a_socket(path: Path) -> None:
    """``freeze`` and ``scenario`` are the functions a deterministic run may call."""
    if path.name == "live.py":
        pytest.skip("the explicit live path is isolated by name and by injected transport")
    offenders = _imports(path) & NETWORK_MODULES
    assert not offenders, f"{path.name} imports {offenders}"


def test_the_adapter_never_decides_a_reservation() -> None:
    """Contract-checked in .importlinter too; this is the cheap local version."""
    for path in sorted(SRC_ROOT.rglob("*.py")):
        reached = {
            module
            for module in _imports(path)
            if module.startswith(
                ("droneops_reservations", "droneops_planner", "droneops_dispatcher")
            )
        }
        assert not reached, f"{path.name} reaches a decision package: {reached}"
