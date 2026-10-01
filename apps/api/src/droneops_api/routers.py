"""Routers: validate, authorise, delegate. No database, no policy, no aircraft.

``technical-build/01`` section 4 gives ``apps/api`` one job - be the composition root's
edge. Every handler here does exactly four things:

1. resolve the :class:`~droneops_api.ports.Principal` (tenancy + capability scope);
2. bind and check the request body against the wire model;
3. require the ``Idempotency-Key`` header to agree with the body key, then replay or
   execute through the idempotency ledger;
4. hand the command to an injected port and return what the port said.

There is no session, no query, no repository import, and no operation that could be
mistaken for a vehicle command: a grep of this module for ``sqlalchemy``, ``session`` or
``persistence`` returns nothing, and :mod:`scripts.openapi_gate` proves the same of the
published surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from droneops_api.ports import (
    CAPABILITY_AUDIT_READ,
    CAPABILITY_FACILITY_OUTAGE,
    CAPABILITY_MISSION_CANCEL,
    CAPABILITY_MISSION_COMMIT,
    CAPABILITY_MISSION_PLAN,
    CAPABILITY_ORDER_READ,
    CAPABILITY_ORDER_WRITE,
    CAPABILITY_PROPOSAL_ACCEPT,
    CAPABILITY_PROPOSAL_DECLINE,
    CAPABILITY_READ,
    CAPABILITY_SIMULATION_READ,
    CancelDraft,
    CommitDraft,
    ControlPlaneError,
    ControlPlaneServices,
    IdempotencyKeyReusedError,
    OrderDraft,
    OutageDraft,
    PlanDraft,
    Principal,
    ProposalDraft,
    StoredRequest,
)
from droneops_api.provenance import SourceKind, provenance_fields
from droneops_api.schemas import (
    CancelRequest,
    OrderRequest,
    OutageRequest,
    PlanRequest,
    ProposalDecisionRequest,
    ReserveRequest,
    StatusStreamEventList,
)
from droneops_api.security import enforce

EPOCH_FLOOR = datetime(1970, 1, 1, tzinfo=UTC)
HORIZON = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1)]


def services_of(request: Request) -> ControlPlaneServices:
    services: ControlPlaneServices = request.app.state.services
    return services


def principal_of(request: Request) -> Principal:
    principal: Principal = request.app.state.principal_resolver(request)
    return principal


PrincipalDep = Annotated[Principal, Depends(principal_of)]
ServicesDep = Annotated[ControlPlaneServices, Depends(services_of)]

#: Reads accepted with either the general read scope or the resource's own read scope.
READ_SCOPES = {
    "orders": (CAPABILITY_READ, CAPABILITY_ORDER_READ),
    "default": (CAPABILITY_READ,),
}


def require_read(principal: Principal, area: str = "default") -> None:
    if not any(principal.may(scope) for scope in READ_SCOPES[area]):
        enforce(principal, CAPABILITY_READ)


def payload_digest(payload: BaseModel) -> str:
    """Content hash of a request body, excluding the client key itself."""

    body = payload.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def check_key_agrees(header_key: str, body_key: str) -> None:
    """The header and the body must name the same key, or the retry is ambiguous."""

    if header_key != body_key:
        raise ControlPlaneError("Idempotency-Key header and body idempotency_key must be identical")


def execute_write(
    services: ControlPlaneServices,
    principal: Principal,
    *,
    operation: str,
    key: str,
    digest: str,
    status_code: int,
    work: Callable[[BaseModel | None], BaseModel],
) -> JSONResponse:
    """Replay a committed result, or run the work once and record its answer.

    A retry of the same key with the same body never re-executes: it returns the original
    response with status 200, so "you already asked for this" is visible on the wire. A
    reused key with a different body is a 409 from the ledger, and a second commitment of
    an already committed identity is a 409 from the ports.
    """

    stored = services.idempotency.lookup(principal, operation, key)
    if stored is not None:
        if stored.request_digest != digest:
            raise IdempotencyKeyReusedError(
                f"Idempotency-Key '{key}' was already used for a different request.",
                stored.request_digest,
            )
        return JSONResponse(status_code=200, content=stored.body)
    result = work(None)
    body = json.loads(result.model_dump_json())
    services.idempotency.record(
        principal,
        operation,
        StoredRequest(
            operation=operation,
            key=key,
            request_digest=digest,
            status_code=status_code,
            body=body,
        ),
    )
    return JSONResponse(status_code=status_code, content=body)


def time_window(
    time_from: Annotated[datetime | None, Query(alias="time_from")] = None,
    time_to: Annotated[datetime | None, Query(alias="time_to")] = None,
) -> tuple[datetime, datetime]:
    start = time_from or EPOCH_FLOOR
    end = time_to or HORIZON
    if start.tzinfo is None or end.tzinfo is None:
        raise ControlPlaneError("non_utc_timestamp: time bounds must be timezone-aware")
    if start > end:
        raise ControlPlaneError("time_from must not be after time_to")
    return start, end


# ------------------------------------------------------------------ orders

orders_router = APIRouter(tags=["orders"])


@orders_router.post(
    "/v1/orders",
    response_model=None,
    status_code=201,
    operation_id="createOrder",
    summary="Register a delivery order (simulation only)",
)
def create_order(
    payload: OrderRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_ORDER_WRITE)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    draft = OrderDraft(
        external_ref=payload.external_ref,
        origin_facility_id=payload.origin_facility_id,
        destination_facility_id=payload.destination_facility_id,
        payload_profile_id=payload.payload_profile_id,
        priority_class=payload.priority_class,
        earliest_departure=payload.earliest_departure,
        requested_deadline=payload.requested_deadline,
        idempotency_key=payload.idempotency_key,
    )
    return execute_write(
        services,
        principal,
        operation="POST /v1/orders",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=201,
        work=lambda _: services.orders.create(principal, draft),
    )


@orders_router.get("/v1/orders", response_model=None, operation_id="listOrders")
def list_orders(
    services: ServicesDep,
    principal: PrincipalDep,
    bounds: Annotated[tuple[datetime, datetime], Depends(time_window)],
) -> Any:
    require_read(principal, "orders")
    return services.orders.list(principal, bounds[0], bounds[1])


@orders_router.get("/v1/orders/{id}", response_model=None, operation_id="getOrder")
def get_order(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal, "orders")
    return services.orders.fetch(principal, id)


@orders_router.post(
    "/v1/orders/{id}/cancel",
    response_model=None,
    status_code=200,
    operation_id="cancelOrder",
)
def cancel_order(
    id: UUID,
    payload: CancelRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_ORDER_WRITE)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/orders/{id}/cancel",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.orders.cancel(
            principal, id, CancelDraft(payload.idempotency_key, payload.reason_code, payload.note)
        ),
    )


# ------------------------------------------------------------------ missions

missions_router = APIRouter(tags=["missions"])


@missions_router.post(
    "/v1/missions/{id}/plan",
    response_model=None,
    status_code=202,
    operation_id="requestMissionPlan",
)
def plan_mission(
    id: UUID,
    payload: PlanRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_MISSION_PLAN)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/plan",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=202,
        work=lambda _: services.missions.plan(
            principal,
            id,
            PlanDraft(payload.idempotency_key, payload.config_release_sha, payload.reason_code),
        ),
    )


@missions_router.get(
    "/v1/missions/{id}/plan-versions", response_model=None, operation_id="listMissionPlanVersions"
)
def list_plan_versions(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.missions.plan_versions(principal, id)


@missions_router.post(
    "/v1/missions/{id}/reserve",
    response_model=None,
    status_code=201,
    operation_id="reserveMissionResources",
)
def reserve_mission(
    id: UUID,
    payload: ReserveRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_MISSION_COMMIT)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/reserve",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=201,
        work=lambda _: services.missions.reserve(
            principal,
            id,
            CommitDraft(
                payload.idempotency_key,
                payload.plan_version_id,
                payload.config_release_sha,
                payload.reason_code,
            ),
        ),
    )


@missions_router.get(
    "/v1/missions/{id}/decision", response_model=None, operation_id="getMissionDecisionTrace"
)
def mission_decision(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.missions.decision(principal, id)


@missions_router.get(
    "/v1/missions/{id}/uncertainty",
    response_model=None,
    operation_id="getMissionUncertaintyState",
    tags=["monitoring"],
)
def mission_uncertainty(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.missions.uncertainty(principal, id)


@missions_router.post(
    "/v1/missions/{id}/cancel", response_model=None, status_code=200, operation_id="cancelMission"
)
def cancel_mission(
    id: UUID,
    payload: CancelRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_MISSION_CANCEL)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/cancel",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.missions.cancel(
            principal, id, CancelDraft(payload.idempotency_key, payload.reason_code, payload.note)
        ),
    )


@missions_router.get(
    "/v1/missions/{id}/reservations",
    response_model=None,
    operation_id="listMissionReservations",
    tags=["reservations"],
)
def mission_reservations(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.missions.reservations(principal, id)


@missions_router.get(
    "/v1/missions/{id}/observations",
    response_model=None,
    operation_id="listMissionObservations",
    tags=["monitoring"],
)
def mission_observations(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.missions.observations(principal, id)


def _proposal_capability(principal: Principal, capability: str) -> None:
    """Previewing needs a decision-maker: accept or decline, never a passive reader."""

    enforce(principal, capability)


@missions_router.post(
    "/v1/missions/{id}/proposals/{proposal_id}/preview",
    response_model=None,
    status_code=200,
    operation_id="previewMissionProposal",
)
def preview_proposal(  # noqa: PLR0917 - path, body, key and injected scope are all FastAPI args
    id: UUID,
    proposal_id: UUID,
    payload: ProposalDecisionRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    if not (
        principal.may(CAPABILITY_PROPOSAL_ACCEPT) or principal.may(CAPABILITY_PROPOSAL_DECLINE)
    ):
        _proposal_capability(principal, CAPABILITY_PROPOSAL_ACCEPT)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/proposals/{proposal_id}/preview",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.missions.preview_proposal(
            principal, id, proposal_id, ProposalDraft(*_draft_args(payload))
        ),
    )


@missions_router.post(
    "/v1/missions/{id}/proposals/{proposal_id}/accept",
    response_model=None,
    status_code=200,
    operation_id="acceptMissionProposal",
)
def accept_proposal(  # noqa: PLR0917 - path, body, key and injected scope are all FastAPI args
    id: UUID,
    proposal_id: UUID,
    payload: ProposalDecisionRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_PROPOSAL_ACCEPT)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/proposals/{proposal_id}/accept",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.missions.accept_proposal(
            principal, id, proposal_id, ProposalDraft(*_draft_args(payload))
        ),
    )


@missions_router.post(
    "/v1/missions/{id}/proposals/{proposal_id}/decline",
    response_model=None,
    status_code=200,
    operation_id="declineMissionProposal",
)
def decline_proposal(  # noqa: PLR0917 - path, body, key and injected scope are all FastAPI args
    id: UUID,
    proposal_id: UUID,
    payload: ProposalDecisionRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_PROPOSAL_DECLINE)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/missions/{id}/proposals/{proposal_id}/decline",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.missions.decline_proposal(
            principal, id, proposal_id, ProposalDraft(*_draft_args(payload))
        ),
    )


def _draft_args(payload: ProposalDecisionRequest) -> tuple[str, str, str]:
    return (payload.idempotency_key, payload.reason_code, payload.note)


# ------------------------------------------------------------------ fleet and facilities

fleet_router = APIRouter(tags=["fleet"])
facilities_router = APIRouter(tags=["facilities"])


@fleet_router.get("/v1/aircraft", response_model=None, operation_id="listAircraft")
def list_aircraft(services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.fleet.list(principal)


@fleet_router.get("/v1/aircraft/{id}", response_model=None, operation_id="getAircraft")
def get_aircraft(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.fleet.fetch(principal, id)


@facilities_router.get("/v1/facilities", response_model=None, operation_id="listFacilities")
def list_facilities(services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.facilities.list(principal)


@facilities_router.get(
    "/v1/facilities/{id}/capacity", response_model=None, operation_id="getFacilityCapacity"
)
def facility_capacity(id: UUID, services: ServicesDep, principal: PrincipalDep) -> Any:
    require_read(principal)
    return services.facilities.capacity(principal, id)


@facilities_router.post(
    "/v1/facilities/{id}/outage",
    response_model=None,
    status_code=200,
    operation_id="reportFacilityOutage",
)
def report_outage(
    id: UUID,
    payload: OutageRequest,
    services: ServicesDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    enforce(principal, CAPABILITY_FACILITY_OUTAGE)
    check_key_agrees(idempotency_key, payload.idempotency_key)
    return execute_write(
        services,
        principal,
        operation="POST /v1/facilities/{id}/outage",
        key=idempotency_key,
        digest=payload_digest(payload),
        status_code=200,
        work=lambda _: services.facilities.outage(
            principal,
            id,
            OutageDraft(
                payload.idempotency_key,
                payload.reason_code,
                payload.valid_from,
                payload.valid_until,
            ),
        ),
    )


# ------------------------------------------------------------------ reservations, audit, replay

reservations_router = APIRouter(tags=["reservations"])
audit_router = APIRouter(tags=["audit"])
scenarios_router = APIRouter(tags=["scenarios"])


@reservations_router.get("/v1/reservations", response_model=None, operation_id="listReservations")
def list_reservations(
    services: ServicesDep,
    principal: PrincipalDep,
    bounds: Annotated[tuple[datetime, datetime], Depends(time_window)],
) -> Any:
    require_read(principal)
    return services.reservations.list(principal, bounds[0], bounds[1])


@audit_router.get("/v1/audit-events", response_model=None, operation_id="listAuditEvents")
def list_audit_events(
    services: ServicesDep,
    principal: PrincipalDep,
    bounds: Annotated[tuple[datetime, datetime], Depends(time_window)],
) -> Any:
    enforce(principal, CAPABILITY_AUDIT_READ)
    return services.audit.list(principal, bounds[0], bounds[1])


@scenarios_router.get(
    "/v1/simulations/{run_id}", response_model=None, operation_id="getSimulationRun"
)
def simulation_run(run_id: str, services: ServicesDep, principal: PrincipalDep) -> Any:
    enforce(principal, CAPABILITY_SIMULATION_READ)
    return services.simulations.run(principal, run_id)


@scenarios_router.get(
    "/v1/simulations/{run_id}/replay-diff",
    response_model=None,
    operation_id="getSimulationReplayDiff",
)
def simulation_replay_diff(run_id: str, services: ServicesDep, principal: PrincipalDep) -> Any:
    enforce(principal, CAPABILITY_SIMULATION_READ)
    return services.simulations.replay_diff(principal, run_id)


@scenarios_router.get(
    "/v1/scenarios/{scenario_id}/compare",
    response_model=None,
    operation_id="compareScenarios",
)
def compare_scenarios(
    scenario_id: str,
    against: Annotated[str, Query(alias="against")],
    services: ServicesDep,
    principal: PrincipalDep,
) -> Any:
    enforce(principal, CAPABILITY_SIMULATION_READ)
    return services.simulations.compare(principal, scenario_id, against)


# ------------------------------------------------------------------ status stream

status_router = APIRouter(tags=["monitoring"])


@status_router.get(
    "/v1/status/stream",
    operation_id="readStatusStream",
    summary="Server-sent events for the operations console (simulation state only)",
    response_model=None,
)
def status_stream(
    services: ServicesDep,
    principal: PrincipalDep,
    request: Request,
    after: Annotated[int, Query(alias="after", ge=0)] = 0,
    limit: Annotated[int, Query(alias="limit", ge=1, le=500)] = 100,
) -> StreamingResponse:
    """A bounded read of the append-only status log, as ``text/event-stream``.

    No broker: the console reconnects with the last sequence it saw. Every event carries
    its own provenance, so a planned event is never mistaken for a committed one.
    """

    require_read(principal)
    last_event_id = request.headers.get("Last-Event-ID", "")
    start = after
    if last_event_id.isdigit():
        start = max(start, int(last_event_id))

    def encode(events: list[Any]) -> Iterator[str]:
        for event in events:
            body = json.dumps(json.loads(event.model_dump_json()))
            yield f"id: {event.sequence}\nevent: {event.kind}\ndata: {body}\n\n"

    collected = services.status.events(principal, start, limit)
    if not collected:
        snapshot = StatusStreamEventList(
            items=[],
            last_sequence=start,
            **provenance_fields(SourceKind.SYNTHETIC),
        )
        yield_marker = json.dumps(json.loads(snapshot.model_dump_json()))

        def keepalive() -> Iterator[str]:
            yield f"event: keepalive\ndata: {yield_marker}\n\n"

        return StreamingResponse(keepalive(), media_type="text/event-stream")
    return StreamingResponse(encode(collected), media_type="text/event-stream")
