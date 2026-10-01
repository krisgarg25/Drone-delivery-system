"""Composition root: assemble the control plane, bind failures to status codes.

This is the only module in ``apps/api`` that knows which implementations satisfy the
ports. Routers receive a
:class:`~droneops_api.ports.ControlPlaneServices` bundle built here, and a deployment
swaps the bundle without a router changing. There are two bundles, and they are named for
what they are:

:class:`build_reference_services`
    the **test double**. Everything is in memory, ``reserve`` answers from a book the
    process keeps, and nothing is written to a database. It is the default only because the
    Phase 4 simulation deployment has no database to talk to.
:class:`build_ledger_services`
    **the real plane**. ``reserve`` runs the eleven steps of ``technical-build/01``
    section 9 through :mod:`droneops_reservations.ledger` inside one PostgreSQL
    transaction, the vote is taken by :mod:`droneops_conflict_checker` over the
    adjacency-expanded pool, and audit appends chain into ``audit_log``.

``droneops_api`` never imports ``droneops_persistence`` and never holds a session: the
contract ``api-has-no-command-path`` in ``.importlinter`` stays KEPT, and it stays KEPT by
design - the only things injected are protocols and the ledger's own asyncpg store, and the
one collaborator this module cannot construct for itself (the audit sink) is passed in.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from droneops_api.dispatch_plane import DispatcherSimulationPort
from droneops_api.ledger_plane import (
    AuditChainPort,
    AuditSink,
    LedgerMissionPort,
    LedgerOrderPort,
    LedgerPlane,
    LedgerReservationPort,
    LoopRunner,
    PlaneWorld,
    frozen_adjacency,
)
from droneops_api.ports import (
    CapabilityDeniedError,
    ControlPlaneError,
    ControlPlaneServices,
    DuplicateCommitError,
    FacilityPort,
    FleetPort,
    IdempotencyKeyReusedError,
    Principal,
    ResourceNotFoundError,
    SimulationPort,
    StatusPort,
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
from droneops_conflict_checker import CheckerPolicy, CommittedPlanRecord
from droneops_reservations.commit import FinalSet
from droneops_reservations.ledger import (
    CheckerCommitValidator,
    LedgerStore,
    ReservationCommitLedger,
    utc_now,
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
    """THE TEST DOUBLE, explicitly named. Use it only as a fixture.

    Everything is in memory, ``reserve`` answers from a book the process keeps, and the
    simulation figures are seeded fixtures. Production composition is
    :func:`build_production_services`; this function exists so a test can name the double
    it is wiring rather than get it by default.
    """

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


def build_production_services(
    *,
    plane: InMemoryControlPlane | None = None,
    fleet: FleetPort | None = None,
    facilities: FacilityPort | None = None,
    simulation: SimulationPort | None = None,
    status: StatusPort | None = None,
) -> ControlPlaneServices:
    """The production composition: the real read plane, injected where a database is needed.

    What is real here, and what still needs a deployment to hand it something:

    ``simulations``
        :class:`droneops_api.dispatch_plane.DispatcherSimulationPort` - the dispatcher's
        seeded 21 x 3 comparison over ``droneops_planner``'s own routing strategies, with
        corridor-graph geometry and section-5.8 economics. Nothing about a run, a comparison
        or an economic figure is a fixture any more.
    ``fleet`` / ``facilities``
        whatever the deployment injects. A persistence-backed read model satisfies these
        ports structurally; this module cannot build one itself, because ``droneops_api``
        must not import ``droneops_persistence`` and does not. Left uninjected, the two
        reads stay on the double and are reported as such.
    ``status``
        the telemetry projection's event feed when injected, otherwise the double's log.
    ``orders`` / ``missions`` / ``reservations`` / ``audit``
        the ledger bundle when a deployment supplies a ``LedgerStore``, an audit sink and a
        loop runner (:func:`build_ledger_services`); without a database the workflow intake
        stays on the documented in-memory plane, and that is stated rather than hidden.
    """

    bundle_plane = plane or InMemoryControlPlane(tenants=(TENANT_A, TENANT_B))
    reference = build_services(bundle_plane)
    fleet = fleet or reference.fleet
    facilities = facilities or reference.facilities
    return ControlPlaneServices(
        orders=reference.orders,
        missions=reference.missions,
        reservations=reference.reservations,
        fleet=fleet,
        facilities=facilities,
        audit=reference.audit,
        simulations=simulation or DispatcherSimulationPort(config_release_sha=CONFIG_RELEASE_SHA),
        status=status or reference.status,
        idempotency=reference.idempotency,
    )


class _CommittedRecordPort:
    """Late binding for the checker's ``committed_records`` port.

    The validator needs a reader the ledger only exposes once it exists, and the ledger
    needs the validator to exist first. Rather than reach into either object after the fact,
    the port is a one-method object both can name, and it is bound exactly once, here.
    Unbound it returns nothing, which is the same answer as the validator's own ``None``
    default - a pool that was never supplied is not a pool that is empty.
    """

    def __init__(self) -> None:
        self._read: Callable[[FinalSet], Sequence[CommittedPlanRecord]] | None = None

    def bind(self, reader: Callable[[FinalSet], Sequence[CommittedPlanRecord]]) -> None:
        self._read = reader

    def __call__(self, final: FinalSet) -> Sequence[CommittedPlanRecord]:
        return () if self._read is None else self._read(final)


def build_checker_ledger(
    store: LedgerStore,
    policy: CheckerPolicy,
    *,
    evaluated_at: datetime | None = None,
    record_pool: bool = True,
) -> ReservationCommitLedger:
    """The real ledger, judged by the real checker, over the adjacency-expanded pool.

    Three wirings, each the answer to a gap that was open:

    ``adjacency=frozen_adjacency()``
        step 8 reads who holds *this* resource **and who holds next door**, so a neighbouring
        lane or cell reaches the vote instead of being invisible to it.
    ``committed_records=port``
        the checker's wider world: committed rows the ledger read but did not put in the
        final set, grouped per mission. Without it the port keeps its ``None`` default and
        the checker only ever sees same-key holders.
    ``record_pool=True``
        makes the ledger perform that read. Opt-in, because the 100-way stress proofs do not
        need a second query per commit.
    """
    grid = frozen_adjacency()
    port = _CommittedRecordPort()
    validator = CheckerCommitValidator(
        policy,
        evaluated_at or utc_now(),
        adjacency=grid,
        committed_records=port,
    )
    ledger = ReservationCommitLedger(
        store,
        validator.validate,
        adjacency=grid,
        record_pool=record_pool,
    )
    port.bind(ledger.committed_records)
    return ledger


def build_ledger_services(
    *,
    plane: InMemoryControlPlane,
    ledger: ReservationCommitLedger,
    store: LedgerStore,
    runner: LoopRunner,
    audit_sink: AuditSink,
    policy_version: str,
    clock: Callable[[], datetime] = utc_now,
) -> ControlPlaneServices:
    """The production bundle: commits through the ledger, audit through ``audit_log``.

    The commit path is the ledger's, end to end - ``POST /v1/missions/{id}/reserve`` runs
    the eleven steps and its refusal arrives as the reason code the ledger persisted. The
    read models with no ledger counterpart yet (fleet, facilities, simulation runs, the
    status stream, decision traces, telemetry observations and workflow proposals) are still
    served by the double passed in as ``plane``, and :class:`LedgerMissionPort` says so in
    its own docstring rather than in a comment here.

    ``audit_sink`` is the one collaborator this module cannot construct: it is
    ``droneops_persistence.audit_chain.PostgresAuditChain``, and importing it from here would
    break ``api-has-no-command-path``. A deployment opens it and passes it in, which is what
    makes this a composition root instead of a god object.
    """
    fallback = ReferenceMissionPort(plane)
    services_plane = LedgerPlane(
        store,
        ledger,
        runner,
        lambda principal: PlaneWorld(
            tenant_id=principal.tenant_id,
            facilities=tuple(
                (PlaneWorld.facility_id(principal.tenant_id, name), name)
                for name in ("hub-north", "node-hospital-a", "node-clinic-b")
            ),
            config_release_sha=CONFIG_RELEASE_SHA,
            policy_version=policy_version,
        ),
        clock=clock,
    )
    audit = AuditChainPort(audit_sink, runner, clock)
    reference = build_services(plane)
    return ControlPlaneServices(
        orders=LedgerOrderPort(services_plane),
        missions=LedgerMissionPort(
            services_plane,
            audit=lambda principal, kind, resource_id, code: audit.append_note(
                principal, kind, resource_id, code, principal.config_release_sha
            ),
            fallback=fallback,
        ),
        reservations=LedgerReservationPort(services_plane),
        fleet=reference.fleet,
        facilities=reference.facilities,
        audit=audit,
        simulations=reference.simulations,
        status=reference.status,
        idempotency=reference.idempotency,
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

    bundle = services or build_production_services()
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
