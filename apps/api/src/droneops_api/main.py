from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from droneops_contracts import (
    AltitudeReference,
    MissionStatus,
    SimulatedOrderRequest,
    SimulatedOrderResponse,
)
from droneops_core_domain import create_simulated_mission, transition_mission
from droneops_platform import get_clock

APP_TITLE = "DroneOps Phase 1 API"
APP_VERSION = "0.1.0"
PHASE_NOTE = "Simulation only. This service contains no aircraft command path."


class HealthResponse(BaseModel):
    status: str
    phase: str
    authorized_for_flight: bool
    detail: str


class VersionResponse(BaseModel):
    version: str
    api_version: str
    domain_schema_version: int


app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=PHASE_NOTE,
    openapi_tags=[
        {"name": "operational-support", "description": "Simulated planning and audit workflows."},
    ],
)
app.state.missions = {}


@app.get("/health", response_model=HealthResponse, tags=["operational-support"])
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        phase="phase-1-simulation",
        authorized_for_flight=False,
        detail=PHASE_NOTE,
    )


@app.get("/version", response_model=VersionResponse, tags=["operational-support"])
def version() -> VersionResponse:
    return VersionResponse(version=APP_VERSION, api_version="v1", domain_schema_version=1)


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
        raise HTTPException(status_code=422, detail="Idempotency-Key header or body key is required")

    store = request.app.state.missions
    if key in store:
        response.status_code = 200
        return store[key]

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

    response = SimulatedOrderResponse(
        mission_id=mission_id,
        status=created.status,
        phase="phase-1-simulation",
        authorized_for_flight=False,
        source_kind="simulated_order_acceptance",
        notes="No aircraft, pad, corridor, weather or regulatory source was consulted.",
    )
    store[key] = response
    return response
