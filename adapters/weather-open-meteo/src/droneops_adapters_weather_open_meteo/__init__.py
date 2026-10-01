"""Open-Meteo weather adapter: build a request, freeze a response, replay it.

This is an *adapter*: the only code in the energy path that talks to an outside
service, and it has no authority over any decision. Its job ends at producing a
``droneops_energy.WindSnapshot`` - a frozen artefact with
provenance - which is then passed to the energy kernel.

The structural rule this adapter exists to enforce is that a deterministic run can
never make a network call:

* ``freeze`` and ``scenario`` are pure functions of bytes already on disk. They
  import no HTTP client and no socket.
* ``live.request_snapshot`` needs a transport object *injected by the caller* and a
  ``LiveFetchAuthorisation`` that carries who authorised it. There is no default
  transport in this module, so a simulation entry point cannot reach the network by
  forgetting to configure something - it would have to write the client itself.
* ``apps/sim_runner`` and ``droneops_sim_core`` are forbidden from importing this
  package at all, by the ``sim-core-has-no-network-or-database`` import-linter
  contract, and the energy kernel is forbidden from importing any adapter.

Temperatures and pressure are carried through as provenance only: the Phase 3
energy model has no density-altitude term, and inventing one without data would be
the kind of unreviewed constant the conventions forbid.
"""

from __future__ import annotations

from .freeze import (
    OPEN_METEO_SCHEMA_VERSION,
    snapshot_from_open_meteo,
    snapshot_from_payload,
)
from .live import (
    LiveFetchAuthorisation,
    Transport,
    request_snapshot,
    snapshot_url,
)
from .scenario import snapshot_from_scenario_trace

__all__ = [
    "OPEN_METEO_SCHEMA_VERSION",
    "LiveFetchAuthorisation",
    "Transport",
    "request_snapshot",
    "snapshot_from_open_meteo",
    "snapshot_from_payload",
    "snapshot_from_scenario_trace",
    "snapshot_url",
]
