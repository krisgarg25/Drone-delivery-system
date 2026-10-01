"""The only place in this adapter that could reach a network - and it cannot act alone.

A live fetch needs two things at once: a :class:`Transport` object supplied by the
caller, and a :class:`LiveFetchAuthorisation` naming who authorised the fetch and
for what. Neither has a default, and this module imports no HTTP client at all, so
there is no code path from a simulation entry point to a socket. A run that wants
weather passes bytes it already has to :func:`freeze.snapshot_from_payload` instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Protocol

from droneops_energy import WindSnapshot
from droneops_energy.errors import EnergyError

from .freeze import snapshot_from_open_meteo

#: The endpoint version this adapter speaks. Pinned: an unpinned API version would
#: let a provider change the meaning of a field underneath a stored run.
OPEN_METEO_API_VERSION: Final = "v1"

#: Forecast variables requested. Wind only: the energy model has no other weather term.
WIND_VARIABLES: Final = ("wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")


class FetchRefusedError(EnergyError):
    """A live fetch was asked for without the authority to make one."""


class Transport(Protocol):
    """The minimum a caller must inject: ``get(url, params) -> JSON body``.

    Deliberately structural rather than an ``httpx.Client``: the adapter stays
    importable inside a sandboxed simulation where no HTTP library exists at all.
    """

    def get(self, url: str, params: Mapping[str, str]) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class LiveFetchAuthorisation:
    """Who allowed a live call, and for what purpose.

    ``for_simulation`` is refused by construction: a deterministic run has no
    authorised live fetch, so the object a simulation would need is the one this
    class cannot represent.
    """

    authorised_by: str
    purpose: str
    for_simulation: bool = False

    def __post_init__(self) -> None:
        if self.for_simulation:
            raise FetchRefusedError(
                "a deterministic simulation run may not authorise a live fetch: replay the "
                "frozen bundle instead (conventions section 4)"
            )
        if not self.authorised_by or not self.purpose:
            raise FetchRefusedError(
                "a live weather fetch needs an authoriser and a purpose on the record"
            )


def snapshot_url(
    *,
    latitude: str,
    longitude: str,
    forecast_hours: int,
    timezone: str = "Asia/Kolkata",
    api_version: str = OPEN_METEO_API_VERSION,
) -> str:
    """Build the request URL: a pure string, testable without a transport."""
    if not (latitude and longitude):
        raise FetchRefusedError("a weather request needs a coordinate pair")
    if not 1 <= forecast_hours <= 240:
        raise FetchRefusedError(f"forecast_hours {forecast_hours} is outside the pinned contract")
    query = "&".join(
        (
            f"latitude={latitude}",
            f"longitude={longitude}",
            "hourly=" + ",".join(WIND_VARIABLES),
            f"forecast_days={(forecast_hours + 23) // 24}",
            f"timezone={timezone}",
        )
    )
    return f"https://api.open-meteo.com/{api_version}/forecast?{query}"


def request_snapshot(
    transport: Transport,
    authorisation: LiveFetchAuthorisation,
    *,
    latitude: str,
    longitude: str,
    forecast_hours: int,
    station: str,
    bundle_sha256: str,
    retrieved_at_us: int,
    expires_at_us: int | None = None,
    timezone: str = "Asia/Kolkata",
) -> WindSnapshot:
    """Fetch, then freeze: the result is an ordinary snapshot, and so is every later run.

    The caller supplies both the transport and the authorisation, and records the
    body it received; after this call nothing remembers either.
    """
    if transport is None:  # pragma: no cover - defensive against a None injection
        raise FetchRefusedError("no transport, no fetch")
    url = snapshot_url(
        latitude=latitude,
        longitude=longitude,
        forecast_hours=forecast_hours,
        timezone=timezone,
    )
    body = transport.get(url, {"station": station})
    snapshot = snapshot_from_open_meteo(
        body,
        station=station,
        bundle_sha256=bundle_sha256,
        retrieved_at_us=retrieved_at_us,
        expires_at_us=expires_at_us,
    )
    # The authoriser is recorded in the snapshot's own source string, so a run that
    # carries live-derived weather can be traced back to who allowed it.
    return replace(
        snapshot,
        source=f"{snapshot.source} (live fetch authorised by {authorisation.authorised_by})",
    )
