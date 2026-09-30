"""Error and conflict response contracts.

Duplicate-commit conflicts resolve to HTTP 409 with a stable error code; the
retry of a committed request returns the original result (invariant I2).
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .provenance import ContractModel


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    DUPLICATE_COMMIT_CONFLICT = "duplicate_commit_conflict"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    UNKNOWN_STATE = "unknown_state"
    MISSING_ALTITUDE_REFERENCE = "missing_altitude_reference"
    NON_UTC_TIMESTAMP = "non_utc_timestamp"
    FORBIDDEN_COMMAND_SURFACE = "forbidden_command_surface"
    TENANT_NOT_AUTHORIZED = "tenant_not_authorized"
    UNRESOLVED = "unresolved"


class ErrorDetail(ContractModel):
    field: str
    reason: str


class ErrorResponse(ContractModel):
    error_code: ErrorCode
    message: str
    request_id: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ConflictResponse(ContractModel):
    """409 body returned when a duplicate POST hits a committed identity."""

    error_code: ErrorCode = ErrorCode.DUPLICATE_COMMIT_CONFLICT
    message: str
    existing_resource_id: str
    request_id: str
