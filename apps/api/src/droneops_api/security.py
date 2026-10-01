"""Tenancy, roles and capability scopes.

``technical-build/04`` section 12 fixes the role vocabulary, and it fixes it by
exclusion as much as by inclusion: **there is no ``aircraft_commander`` role in this
system, in Phase 1 or in any later phase**, because there is nothing for such a role to
call. The capability set in :mod:`droneops_api.ports` contains no vehicle write, and this
module is where a credential is mapped onto a subset of it.

Resolution order, both carried by the composition root so a deployment can replace the
resolver without touching a router:

1. ``Authorization: Bearer <token>`` - an OIDC subject.
2. ``X-DroneOps-Credential: <key>`` - an API credential (``technical-build/04`` section 5).

The credential names the tenant, the roles and a read-only flag. Capabilities come from
roles, then the read-only flag subtracts every write capability: a read-only credential
cannot accept a proposal even if a mislabelled role says otherwise, because the flag is
applied last and only ever removes.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from droneops_api.ports import (
    ALL_CAPABILITIES,
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
    WRITE_CAPABILITIES,
    CapabilityDeniedError,
    Principal,
)
from droneops_contracts import OperatorRole

#: The only roles the platform recognises. ``aircraft_commander`` is absent by design.
ALLOWED_ROLES: frozenset[str] = frozenset(role.value for role in OperatorRole)

#: What each role may ask the control plane to do. Reads are the default; every entry
#: below is a workflow capability, never an aircraft capability.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    OperatorRole.PARTNER_ORDER_WRITER.value: frozenset(
        {CAPABILITY_ORDER_WRITE, CAPABILITY_ORDER_READ}
    ),
    OperatorRole.SCHEDULER_OPERATOR.value: frozenset(
        {
            CAPABILITY_ORDER_WRITE,
            CAPABILITY_ORDER_READ,
            CAPABILITY_MISSION_PLAN,
            CAPABILITY_MISSION_COMMIT,
            CAPABILITY_MISSION_CANCEL,
            CAPABILITY_PROPOSAL_ACCEPT,
            CAPABILITY_PROPOSAL_DECLINE,
            CAPABILITY_FACILITY_OUTAGE,
            CAPABILITY_READ,
            CAPABILITY_SIMULATION_READ,
        }
    ),
    OperatorRole.PILOT_OBSERVER.value: frozenset(
        {
            CAPABILITY_READ,
            #: Section 5.4 puts an Economics view and a comparison in front of the
            #: operations console, and the console signs in as an observer. Reading
            #: simulated results is a *read* scope, so it belongs here rather than in
            #: the general ``read:all`` scope, which would also hand it to a credential
            #: that should only see its own orders. It is not in ``WRITE_CAPABILITIES``,
            #: so a read-only credential keeps it and still cannot accept a proposal.
            CAPABILITY_SIMULATION_READ,
        }
    ),
    OperatorRole.SAFETY_REVIEWER.value: frozenset(
        {
            CAPABILITY_READ,
            CAPABILITY_AUDIT_READ,
            CAPABILITY_PROPOSAL_DECLINE,
            CAPABILITY_SIMULATION_READ,
        }
    ),
    OperatorRole.AUDITOR.value: frozenset(
        {CAPABILITY_READ, CAPABILITY_AUDIT_READ, CAPABILITY_SIMULATION_READ}
    ),
    OperatorRole.PLATFORM_ADMIN.value: ALL_CAPABILITIES,
}

#: A role that is missing from this map has no capabilities at all - fail closed.
DEFAULT_CAPABILITIES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Credential:
    """A resolved OIDC subject or API key: who, in which tenant, with what scopes."""

    tenant_id: UUID
    actor_id: str
    roles: tuple[str, ...]
    read_only: bool = False
    actor_type: str = "user"
    credential_kind: str = "oidc"
    config_release_sha: str = ""

    @property
    def capabilities(self) -> frozenset[str]:
        granted: set[str] = set()
        for role in self.roles:
            granted |= ROLE_CAPABILITIES.get(role, DEFAULT_CAPABILITIES)
        if self.read_only:
            granted -= set(WRITE_CAPABILITIES)
        return frozenset(granted)


class UnknownCredentialError(Exception):
    """The bearer token or API key is not one this deployment recognises."""


class CredentialResolver:
    """Registry of credentials for the simulation deployment.

    A production wiring injects an OIDC claim mapper instead; the router only ever sees
    the resolved :class:`Credential`.
    """

    def __init__(self) -> None:
        self._by_token: dict[str, Credential] = {}
        self._by_actor: dict[str, Credential] = {}

    def register(self, token: str, credential: Credential) -> None:
        self._by_token[token] = credential
        self._by_actor[credential.actor_id] = credential

    def resolve(self, token: str) -> Credential:
        credential = self._by_token.get(token) or self._by_actor.get(token)
        if credential is None:
            raise UnknownCredentialError(token)
        return credential

    def actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_actor))


def principal_from(credential: Credential, request_id: str, config_release_sha: str) -> Principal:
    """Project a credential onto the per-request principal the ports receive."""

    return Principal(
        tenant_id=credential.tenant_id,
        actor_id=credential.actor_id,
        actor_type=credential.actor_type,
        roles=credential.roles,
        capabilities=credential.capabilities,
        credential=credential.credential_kind,
        request_id=request_id,
        config_release_sha=config_release_sha or credential.config_release_sha,
    )


def require(capability: str) -> frozenset[str]:
    """Guard against a typo in a capability name at import time, not at request time."""

    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")
    return frozenset({capability})


def enforce(principal: Principal, capability: str) -> None:
    """Raise the neutral denial a router turns into a 403."""

    if not principal.may(capability):
        raise CapabilityDeniedError(capability)


def has_command_role_or_capability() -> bool:
    """True if any recognised role or capability implies aircraft authority.

    It must always return ``False``, and :mod:`tests.api` asserts that: the audit is
    structural, not a policy document someone hopes is obeyed.
    """

    forbidden_tokens = ("command", "aircraft_write", "mavlink", "upload", "firmware")
    names = {*(role for role in ALLOWED_ROLES), *ALL_CAPABILITIES}
    return any(token in name for name in names for token in forbidden_tokens)
