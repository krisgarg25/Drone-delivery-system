"""Read facility capacity from a frozen scenario bundle.

The model is *loaded, not invented*: ``simulation/scenarios/<bundle>/`` already
encodes pads, charger bays, crew shifts, loading turnarounds and a hub outage,
and those numbers are the ones this package schedules against. Per
``technical-build/12`` section 6, the sites sit on synthetic vertical data
(``alt_m_agl`` with an explicit ``AGL`` reference); nothing here treats a declared
height or a declared pad count as a survey result.

Two rules keep this honest:

* **the document is injected.** Like corridor-graph, this package declares no
  parser and no filesystem dependency: the caller hands over an already-parsed
  mapping. A facility scheduler therefore cannot reach the outside world by
  itself, and a test can feed a rewritten bundle to prove the numbers are read;
* **``authoritative`` must be present.** A capacity document without provenance
  is refused rather than defaulted to "real".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from droneops_contracts.facilities import FacilityStatus, HandoverType
from droneops_geom_core import MICROS_PER_MINUTE, Interval

from .errors import BundleLoadError
from .model import Accessibility, CapacityProfile, OutageWindow, TurnaroundDurations

FACILITIES_DOCUMENT: Final[str] = "facilities.yaml"
DEFAULT_POLICY_VERSION: Final[str] = "sim-facility-policy-v1"

# Flight-deck stages the bundle does not declare. These are experiment
# parameters, not measured site data: the invariant the chain must satisfy is
# that each stage is separately timed, not that 30 seconds is Chandigarh's
# approach time.
DEFAULT_APPROACH_US: Final[int] = 30_000_000
DEFAULT_LANDING_US: Final[int] = 20_000_000
DEFAULT_DEPARTURE_US: Final[int] = 20_000_000

# A site is staffed by one crew team per declared shift pattern unless the
# bundle says otherwise. crew_slots is deliberately not inferred from pad count:
# "capacity but no pad, or a pad but no pilot" is the failure this models.
DEFAULT_CREW_SLOTS: Final[int] = 1

_STATUS_BY_TOKEN: Final[Mapping[str, FacilityStatus]] = {
    "active": FacilityStatus.ACTIVE,
    "outage": FacilityStatus.OUTAGE,
    "closed": FacilityStatus.CLOSED,
    "survey_pending": FacilityStatus.SURVEY_PENDING,
    # The bundle writes an outage state as "closed" inside an outage block.
    "scheduled": FacilityStatus.OUTAGE,
}


def minutes_to_us(minutes: int) -> int:
    """Scenario minutes to kernel microseconds - the same anchor sim-core uses."""
    return minutes * MICROS_PER_MINUTE


def load_capacity_profiles(
    document: Mapping[str, object],
    *,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> tuple[CapacityProfile, ...]:
    """Every site in one capacity document, in file order.

    Raises:
        BundleLoadError: a missing or mistyped field, or a document that
            declares itself authoritative without saying so.
    """
    authoritative = _authoritative(document)
    rows = _sequence(document.get("facilities"), "facilities")
    out: list[CapacityProfile] = []
    for position, row_value in enumerate(rows):
        row = _mapping(row_value, f"facilities[{position}]")
        out.append(_profile(row, f"facilities[{position}]", policy_version=policy_version, authoritative=authoritative))
    return tuple(out)


def scenario_day(profiles: Sequence[CapacityProfile]) -> Interval:
    """The scheduling horizon a bundle implies: first opening to last closing."""
    if not profiles:
        raise BundleLoadError("facilities", "cannot derive a horizon from an empty capacity document")
    return Interval.of(
        min(profile.opens_us for profile in profiles),
        max(profile.closes_us for profile in profiles),
    )


def _profile(
    row: Mapping[str, object],
    path: str,
    *,
    policy_version: str,
    authoritative: bool,
) -> CapacityProfile:
    facility_id = _text(row, "facility_id", path)
    open_minute = _whole(row, "open_minute", path)
    close_minute = _whole(row, "close_minute", path)
    loading_min = _whole(row, "loading_turnaround_min", path)
    offload_min = _whole(row, "offload_turnaround_min", path)
    shifts = _crew_shifts(row.get("crew_shifts"), f"{path}.crew_shifts")
    return CapacityProfile(
        facility_id=facility_id,
        kind=_text(row, "kind", path),
        name=_text(row, "name", path),
        status=_status(row, path),
        opens_us=minutes_to_us(open_minute),
        closes_us=minutes_to_us(close_minute),
        pad_count=_whole(row, "pads", path),
        charger_bays=_whole(row, "charger_bays", path),
        crew_slots=DEFAULT_CREW_SLOTS,
        turnaround=TurnaroundDurations(
            approach_us=DEFAULT_APPROACH_US,
            landing_us=DEFAULT_LANDING_US,
            handover_us=minutes_to_us(offload_min),
            loading_us=minutes_to_us(loading_min),
            departure_us=DEFAULT_DEPARTURE_US,
        ),
        accessibility=_accessibility(row, path),
        charge_power_w=_whole(row, "charge_power_w", path),
        crew_shifts=shifts,
        outages=_outages(row.get("outage"), f"{path}.outage"),
        policy_version=policy_version,
        authoritative=authoritative,
        source=FACILITIES_DOCUMENT,
    )


def _accessibility(row: Mapping[str, object], path: str) -> Accessibility:
    """Declared access and handover type. Only land transfer is accepted.

    ``kind`` drives whether a site can be walked up to: a hub is staffed and
    step-free, a roadside node is not necessarily. These are illustrative
    synthetic attributes, not a survey.
    """
    kind = _text(row, "kind", path)
    declared = row.get("handover_type")
    if declared is None:
        handover = HandoverType.LAND_TRANSFER
    else:
        token = str(declared)
        try:
            handover = HandoverType(token)
        except ValueError as error:
            raise BundleLoadError(f"{path}.handover_type", f"unknown handover type {token!r}") from error
    # Checked here as well as in Accessibility, so a bad bundle reports a load
    # error with a path rather than an unlabelled construction failure.
    if handover is not HandoverType.LAND_TRANSFER:
        raise BundleLoadError(
            f"{path}.handover_type",
            f"handover type {handover.value!r} is banned for this build: land-and-transfer only",
        )
    return require_land_transfer(
        Accessibility(
            handover_type=handover,
            step_free=kind == "hub",
            ground_pickup=True,
            requires_crew=True,
        ),
        f"{path}.handover_type",
    )


def require_land_transfer(accessibility: Accessibility, path: str) -> Accessibility:
    """Reject a bundle that asks for a winch or a parachute handover.

    The Indian government medical delivery template mandates land-and-transfer,
    so a non-conforming site is a data error, not a fallback.
    """
    if accessibility.handover_type is not HandoverType.LAND_TRANSFER:
        raise BundleLoadError(
            path, f"handover type {accessibility.handover_type.value!r} is banned for this build"
        )
    return accessibility


def _status(row: Mapping[str, object], path: str) -> FacilityStatus:
    token = str(row.get("status", "active"))
    status = _STATUS_BY_TOKEN.get(token)
    if status is None:
        raise BundleLoadError(f"{path}.status", f"unknown facility status {token!r}")
    return status


def _outages(value: object, path: str) -> tuple[OutageWindow, ...]:
    """One outage block or a list of them; absence is normal."""
    if value is None:
        return ()
    entries = list(_sequence(value, path)) if isinstance(value, (list, tuple)) else [value]
    out: list[OutageWindow] = []
    for position, raw in enumerate(entries):
        row = _mapping(raw, f"{path}[{position}]")
        state = _status_for_outage(row, f"{path}[{position}]")
        out.append(
            OutageWindow(
                start_us=minutes_to_us(_whole(row, "from_minute", f"{path}[{position}]")),
                end_us=minutes_to_us(_whole(row, "to_minute", f"{path}[{position}]")),
                state=state,
                reason=str(row.get("reason", "declared outage")),
            )
        )
    return tuple(sorted(out, key=lambda item: (item.start_us, item.end_us)))


def _status_for_outage(row: Mapping[str, object], path: str) -> FacilityStatus:
    token = str(row.get("state", "outage"))
    status = _STATUS_BY_TOKEN.get(token)
    if status is None:
        raise BundleLoadError(f"{path}.state", f"unknown outage state {token!r}")
    return status


def _crew_shifts(value: object, path: str) -> tuple[Interval, ...]:
    """Shift pairs in whole minutes, ascending. An empty list means unstaffed."""
    if value is None:
        return ()
    out: list[Interval] = []
    for position, raw in enumerate(_sequence(value, path)):
        pair = _sequence(raw, f"{path}[{position}]")
        if len(pair) != 2:
            raise BundleLoadError(f"{path}[{position}]", "a crew shift is [open_minute, close_minute]")
        low = minutes_to_us(_whole_int(pair[0], f"{path}[{position}][0]"))
        high = minutes_to_us(_whole_int(pair[1], f"{path}[{position}][1]"))
        if high < low:
            raise BundleLoadError(f"{path}[{position}]", "crew shift closes before it opens")
        out.append(Interval.of(low, high))
    return tuple(sorted(out, key=lambda item: (item.start, item.end)))


def _authoritative(document: Mapping[str, object]) -> bool:
    if "authoritative" not in document:
        raise BundleLoadError("authoritative", "a capacity document must state its authority")
    value = document["authoritative"]
    if not isinstance(value, bool):
        raise BundleLoadError("authoritative", "must be a boolean")
    return value


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise BundleLoadError(path, f"expected a mapping, got {type(value).__name__}")


def _sequence(value: object, path: str) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise BundleLoadError(path, f"expected a sequence, got {type(value).__name__}")


def _text(row: Mapping[str, object], key: str, path: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise BundleLoadError(f"{path}.{key}", f"expected a non-empty string, got {value!r}")
    return value


def _whole(row: Mapping[str, object], key: str, path: str) -> int:
    return _whole_int(row.get(key), f"{path}.{key}")


def _whole_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleLoadError(path, f"expected a whole number, got {value!r}")
    return value


__all__ = [
    "DEFAULT_CREW_SLOTS",
    "FACILITIES_DOCUMENT",
    "CapacityProfile",
    "load_capacity_profiles",
    "minutes_to_us",
    "require_land_transfer",
    "scenario_day",
]
