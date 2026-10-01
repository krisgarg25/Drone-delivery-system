"""The live path of the Meteostat adapter: injected transport, explicit authority.

Same structural rule as the forecast adapter: no HTTP client is imported here, so a
deterministic run cannot fetch by accident. A caller that genuinely wants a second
source passes a transport and names itself on the authorisation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from droneops_energy.errors import EnergyError

from .cross_check import StationReport

#: Pinned endpoint version: an unpinned version would let a provider redefine a field.
METEOSTAT_API_VERSION: Final = "v1"

#: Meteostat is a historical/station source: it is never an operational forecast.
SOURCE_CLASS: Final = "second-source cross-check, historical station record"


class FetchRefusedError(EnergyError):
    """A live cross-check was requested without the authority to make one."""


class Transport(Protocol):
    """Minimal injected transport: ``get(url, params) -> JSON body``.

    Structural, not an HTTP client: nothing in this package can open a socket.
    """

    def get(self, url: str, params: Mapping[str, str]) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class LiveFetchAuthorisation:
    """Who authorised a live second-source fetch, and for what."""

    authorised_by: str
    purpose: str
    for_simulation: bool = False

    def __post_init__(self) -> None:
        if self.for_simulation:
            raise FetchRefusedError(
                "a deterministic run may not authorise a live cross-check: replay the frozen "
                "bundle and, if a second source matters, capture it out of band"
            )
        if not self.authorised_by or not self.purpose:
            raise FetchRefusedError("a live cross-check needs an authoriser and a purpose")


def station_url(
    station_id: str,
    *,
    start_iso: str,
    end_iso: str,
    api_version: str = METEOSTAT_API_VERSION,
) -> str:
    """Build the request URL: a pure string, assertable without a transport."""
    if not station_id:
        raise FetchRefusedError("a station cross-check needs a station id")
    if not (start_iso and end_iso):
        raise FetchRefusedError("a station cross-check needs a bounded date range")
    return (
        f"https://meteostat.net/pub/met/{api_version}/station/{station_id}"
        f"?start={start_iso}&end={end_iso}&freq=60m"
    )


def request_report(
    transport: Transport,
    authorisation: LiveFetchAuthorisation,
    *,
    station_id: str,
    start_iso: str,
    end_iso: str,
    retrieved_at_us: int,
) -> StationReport:
    """Fetch a station record and freeze it into a cross-check report."""
    url = station_url(station_id, start_iso=start_iso, end_iso=end_iso)
    body = transport.get(url, {"station": station_id})
    hourly: object = body.get("data", body)
    if not isinstance(hourly, Mapping):
        raise FetchRefusedError("station payload has no hourly object")
    del authorisation  # recorded by the caller at the fetch boundary, not used here
    return StationReport.from_rows(
        tuple((index * 60, int(speed)) for index, speed in enumerate(_speeds(dict(hourly)))),
        station=station_id,
        source=SOURCE_CLASS,
        retrieved_at_us=retrieved_at_us,
    )


def _speeds(hourly: Mapping[str, object]) -> list[object]:
    values = hourly.get("wspd") or hourly.get("wind_speed")
    if not isinstance(values, (list, tuple)):
        raise FetchRefusedError("station payload returned no wind-speed series")
    return list(values)
