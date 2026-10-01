"""Composition root: assemble the control plane, bind failures to status codes.

This is the only module in ``apps/api`` that knows which implementations satisfy the
ports. Routers receive a
:class:`~droneops_api.ports.ControlPlaneServices` bundle built here, and a deployment
swaps the bundle - reference plane today, ``droneops_reservations.ledger`` plus
persistence-backed read models in production - without a router changing.

``droneops_api`` never imports ``droneops_persistence`` and never holds a session: the
contract ``api-has-no-command-path`` in ``.importlinter`` stays KEPT because the only
thing injected here is a set of protocols.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from droneops_api.ports import (
    CapabilityDeniedError,
    ControlPlaneError,
    ControlPlaneServices,
    DuplicateCommitError,
    IdempotencyKeyReusedError,
    Principal,
    ResourceNotFoundError,
    UnresolvedStateError,
)
from droneops_api.provenance import (
    SIMULATION_NOTICE,
    SIMULATION_PHASE,
    SourceKind,
    provenance_fields,
)
from droneops_api.reference import (
    CONFIG_RELEASE_SHA,
    InMemoryControlPlane,
    InMemoryIdempotencyLedger,
    ReferenceAuditPort,
    ReferenceFacilityPort,
    ReferenceFleetPort,
    ReferenceMissionPort,
    ReferenceOrderPort,
    ReferenceReservationPort,
    ReferenceSimulationPort,
    ReferenceStatusPort,
)
from droneops_api.routers import (
    audit_router,
    facilities_router,
    fleet_router,
    missions_router,
    orders_router,
    reservations_router,
    scenarios_router,
    status_router,
)
from droneops_api.schemas import (
    ConflictResponse,
    ErrorResponse,
    HealthResponse,
    VersionResponse,
)
from droneops_api.security import (
    Credential,
    CredentialResolver,
    UnknownCredentialError,
    principal_from,
)

APP_TITLE = "DroneOps Phase 4 Control-Plane API"
APP_VERSION = "0.4.0"
API_VERSION = "v1"
NO_COMMAND_NOTE = (
    "NO-COMMAND RULE: this service exposes no aircraft command, mission-file upload, "
    "parameter, firmware, offboard, MAV_CMD, takeoff, return-to-base or emergency-action "
    "operation. `activated` is a simulated mission state only. Preview and accept mutate a "
    "workflow proposal, never aircraft behaviour. Simulation only. Not authorised for flight."
)

TENANT_A = uuid.UUID("22222222-2222-2222-2222-222222222222")
TENANT_B = uuid.UUID("33333333-3333-3333-3333-333333333333")

#: The simulation deployment's credentials. A real deployment injects an OIDC mapper.
SIMULATION_CREDENTIALS: tuple[tuple[str, Credential], ...] = (
    (
        "sim-partner-a",
        Credential(
            TENANT_A, "actor-partner-a", ("partner_order_writer",), credential_kind="api_key"
        ),
    ),
    (
        "sim-scheduler-a",
        Credential(TENANT_A, "actor-scheduler-a", ("scheduler_operator",), credential_kind="oidc"),
    ),
    (
        "sim-observer-a",
        Credential(
            TENANT_A,
            "actor-observer-a",
            ("pilot_observer",),
            read_only=True,
            credential_kind="api_key",
        ),
    ),
    (
        "sim-safety-a",
        Credential(TENANT_A, "actor-safety-a", ("safety_reviewer",), credential_kind="oidc"),
    ),
    (
        "sim-auditor-a",
        Credential(TENANT_A, "actor-auditor-a", ("auditor",), credential_kind="api_key"),
    ),
    (
        "sim-admin-a",
        Credential(TENANT_A, "actor-admin-a", ("platform_admin",), credential_kind="oidc"),
    ),
    (
        "sim-scheduler-b",
        Credential(TENANT_B, "actor-scheduler-b", ("scheduler_operator",), credential_kind="oidc"),
    ),
    (
        "sim-auditor-b",
        Credential(TENANT_B, "actor-auditor-b", ("auditor",), credential_kind="api_key"),
    ),
)


def build_reference_services() -> ControlPlaneServices:
    """The default bundle: one in-memory plane behind every port."""

    plane = InMemoryControlPlane(tenants=(TENANT_A, TENANT_B))
    return build_services(plane)


def build_services(plane: InMemoryControlPlane) -> ControlPlaneServices:
    return ControlPlaneServices(
        orders=ReferenceOrderPort(plane),
        missions=ReferenceMissionPort(plane),
        reservations=ReferenceReservationPort(plane),
        fleet=ReferenceFleetPort(plane),
        facilities=ReferenceFacilityPort(plane),
        audit=ReferenceAuditPort(plane),
        simulations=ReferenceSimulationPort(plane),
        status=ReferenceStatusPort(plane),
        idempotency=InMemoryIdempotencyLedger(),
    )


def default_resolver() -> CredentialResolver:
    resolver = CredentialResolver()
    for token, credential in SIMULATION_CREDENTIALS:
        resolver.register(token, credential)
    return resolver


def _token_of(request: Request) -> str | None:
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("X-DroneOps-Credential") or None


def make_principal_resolver(
    resolver: CredentialResolver,
) -> Callable[[Request], Principal]:
    """Bind a request to a principal, once, before any router runs."""

    def resolve(request: Request) -> Principal:
        token = _token_of(request)
        if token is None:
            raise HTTPException(status_code=401, detail="missing credential")
        try:
            credential = resolver.resolve(token)
        except UnknownCredentialError as exc:
            raise HTTPException(status_code=401, detail="unknown credential") from exc
        request_id = (
            request.headers.get("X-Request-Id")
            or hashlib.sha256(f"{token}|{request.method}|{request.url.path}".encode()).hexdigest()[
                :12
            ]
        )
        principal = principal_from(credential, request_id, CONFIG_RELEASE_SHA)
        request.state.principal = principal
        return principal

    return resolve


def _error_body(code: str, message: str, request_id: str) -> dict[str, Any]:
    payload = ErrorResponse.model_validate(
        {
            "error_code": code,
            "message": message,
            "request_id": request_id,
            **provenance_fields(SourceKind.SYNTHETIC),
        }
    )
    return jsonable(payload)


def jsonable(model: Any) -> dict[str, Any]:
    """Serialise a wire model without letting a second error escape."""

    loaded: dict[str, Any] = json.loads(model.model_dump_json())
    return loaded


def register_exception_handlers(app: FastAPI) -> None:
    """Documented bodies for every failure: never a bare 500, never an existence leak."""

    @app.exception_handler(ResourceNotFoundError)
    def _not_found(request: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_error_body("tenant_not_authorized", exc.message, _request_id(request, exc)),
        )

    @app.exception_handler(CapabilityDeniedError)
    def _denied(request: Request, exc: CapabilityDeniedError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content=_error_body("tenant_not_authorized", exc.message, _request_id(request, exc)),
        )

    @app.exception_handler(DuplicateCommitError)
    def _duplicate(request: Request, exc: DuplicateCommitError) -> JSONResponse:
        payload = ConflictResponse(
            error_code="duplicate_commit_conflict",
            message=exc.message,
            existing_resource_id=exc.existing_resource_id,
            request_id=_request_id(request, exc),
            phase=SIMULATION_PHASE,
            source_kind=SourceKind.SYNTHETIC,
            authoritative=False,
            simulation_notice=SIMULATION_NOTICE,
        )
        return JSONResponse(status_code=409, content=jsonable(payload))

    @app.exception_handler(IdempotencyKeyReusedError)
    def _reused(request: Request, exc: IdempotencyKeyReusedError) -> JSONResponse:
        payload = ConflictResponse(
            error_code="idempotency_key_reused",
            message=exc.message,
            existing_resource_id=exc.existing_resource_id,
            request_id=_request_id(request, exc),
            phase=SIMULATION_PHASE,
            source_kind=SourceKind.SYNTHETIC,
            authoritative=False,
            simulation_notice=SIMULATION_NOTICE,
        )
        return JSONResponse(status_code=409, content=jsonable(payload))

    @app.exception_handler(UnresolvedStateError)
    def _unresolved(request: Request, exc: UnresolvedStateError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_error_body("unresolved", exc.message, _request_id(request, exc)),
        )

    @app.exception_handler(ControlPlaneError)
    def _invalid(request: Request, exc: ControlPlaneError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body(exc.error_code, exc.message, _request_id(request, exc)),
        )

    @app.exception_handler(RequestValidationError)
    def _schema(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content=_error_body("validation_error", problems[:400], _request_id(request, None)),
        )

    @app.exception_handler(StarletteHTTPException)
    def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "tenant_not_authorized" if exc.status_code in (401, 403, 404) else "validation_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail), _request_id(request, None)),
        )

    @app.exception_handler(Exception)
    def _fallback(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_error_body(
                "unresolved", "Internal error; see the request id.", _request_id(request, exc)
            ),
        )


def _request_id(request: Request, exc: BaseException | None) -> str:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal) and principal.request_id:
        return principal.request_id
    del exc
    return str(uuid.uuid4())[:12]


def create_app(
    services: ControlPlaneServices | None = None,
    resolver: CredentialResolver | None = None,
) -> FastAPI:
    """The control plane as one object: routers, ports, tenancy, status codes."""

    bundle = services or build_reference_services()
    credential_resolver = resolver or default_resolver()

    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=f"Simulation-only operator API. {NO_COMMAND_NOTE}",
        openapi_tags=[
            {"name": "orders"},
            {"name": "missions"},
            {"name": "fleet"},
            {"name": "facilities"},
            {"name": "reservations"},
            {"name": "monitoring"},
            {"name": "audit"},
            {"name": "scenarios"},
        ],
    )
    app.state.services = bundle
    app.state.resolver = credential_resolver
    app.state.principal_resolver = make_principal_resolver(credential_resolver)
    app.state.control_plane_phase = SIMULATION_PHASE

    register_exception_handlers(app)

    app.include_router(orders_router)
    app.include_router(missions_router)
    app.include_router(fleet_router)
    app.include_router(facilities_router)
    app.include_router(reservations_router)
    app.include_router(audit_router)
    app.include_router(scenarios_router)
    app.include_router(status_router)

    @app.get("/health", response_model=HealthResponse, tags=["operational-support"])
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            phase="phase-1-simulation",
            control_plane_phase=SIMULATION_PHASE,
            authorized_for_flight=False,
            detail=SIMULATION_NOTICE,
        )

    @app.get("/version", response_model=VersionResponse, tags=["operational-support"])
    def version() -> VersionResponse:
        return VersionResponse(
            version=APP_VERSION,
            api_version=API_VERSION,
            domain_schema_version=1,
            config_release_sha=CONFIG_RELEASE_SHA,
            simulation_notice=SIMULATION_NOTICE,
        )

    return app
