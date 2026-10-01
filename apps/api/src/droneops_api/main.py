"""Process entry point for the control plane.

``create_app`` in :mod:`droneops_api.composition` is the composition root: it injects the
ports the routers call. This module instantiates it for ``uvicorn`` and keeps the Phase 0
``/v1/simulated-orders`` endpoint alive until the Phase 4 ``/v1/orders`` surface fully
replaces it. :mod:`scripts.openapi_gate` lists that route as legacy: command-free,
reviewed, and not allowed to gain new responsibilities.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, Request, Response

from droneops_api.composition import create_app
from droneops_contracts import (
    AltitudeReference,
    MissionStatus,
    SimulatedOrderRequest,
    SimulatedOrderResponse,
)
from droneops_core_domain import create_simulated_mission, transition_mission
from droneops_platform import get_clock

app = create_app()
app.state.missions = {}


@app.post(
    "/v1/simulated-orders",
    response_model=SimulatedOrderResponse,
    status_code=201,
    tags=["operational-support"],
    responses={
        200: {"description": "Existing mission returned for a duplicate idempotency key."},
        201: {"description": "New simulated mission created."},
    },
)
def create_simulated_order(
    payload: SimulatedOrderRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> SimulatedOrderResponse:
    """Create a simulation mission only. It does not reserve real airspace or command hardware."""
    if payload.altitude_reference is AltitudeReference.UNSPECIFIED:
        raise HTTPException(status_code=422, detail="altitude_reference must be explicit")

    key = idempotency_key or payload.idempotency_key
    if not key:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key header or body key is required"
        )

    store: dict[str, SimulatedOrderResponse] = request.app.state.missions
    if key in store:
        response.status_code = 200
        replayed: SimulatedOrderResponse = store[key]
        return replayed

    mission_id = uuid.uuid4()
    created = create_simulated_mission(
        mission_id=mission_id,
        order_id=uuid.uuid4(),
        origin_facility_id=payload.origin_facility_id,
        destination_facility_id=payload.destination_facility_id,
        priority=payload.priority_class,
        at=get_clock().now(),
    )

    # Phase 0 records a decision without inventing route, energy or airspace facts.
    transition_mission(created, MissionStatus.ELIGIBLE)

    simulated = SimulatedOrderResponse(
        mission_id=mission_id,
        status=created.status,
        phase="phase-1-simulation",
        authorized_for_flight=False,
        source_kind="simulated_order_acceptance",
        notes="No aircraft, pad, corridor, weather or regulatory source was consulted.",
    )
    store[key] = simulated
    return simulated
