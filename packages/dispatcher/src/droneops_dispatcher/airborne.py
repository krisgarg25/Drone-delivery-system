"""Airborne change: a proposal an operator accepts, never an action the system takes.

``technical-build/01`` section 13 defines the protocol - ``proposal -> preview ->
explicit acceptance -> recorded execution intent``, with ``decline`` and ``expiry``
as first-class exits - and this module is that protocol in types. The reason it is
strict is a hard one: an airborne aircraft is in someone's airspace with someone
flying it, and a resequencing algorithm that "optimises" it out of the way would be
reordering a physical fact with a spreadsheet.

Two properties hold by construction here, and the tests assert both:

* :attr:`AirborneChangeProposal.commitment_intact` is ``True`` until an operator
  accepts, and stays ``True`` after a decline or an expiry. Refusing to answer is
  not a way to lose a commitment.
* even *acceptance* records an intent, not a command. ``command_issued`` is a
  constant ``False`` - the dispatcher has no vehicle link (``.importlinter``:
  *Dispatcher cannot reach a vehicle link*), and the record is what the ledger and
  the pilot-in-command act on later.

A preview carries what section 13 lists and nothing hand-wavy: the plan and
configuration hash, affected resources, expected delay, energy margin, the
alternatives considered, affected pilots, the safety parameters in force, and the
consequences of accepting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .errors import IdentityRequiredError, ProposalStateError
from .policy import SIM_ONLY_BANNER, DispatchPolicy, digest_of
from .priority import MissionStatusRow, ResequenceRefusal, airborne_change_authority

COMMAND_AUTHORITY: str = "none: dispatcher holds no vehicle link"


class ProposalState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


@dataclass(frozen=True)
class SafetyParameters:
    """The numbers in force when the preview was drawn - never a live read."""

    policy_version: str
    config_release_sha: str
    horizontal_margin_mm: int
    vertical_margin_mm: int
    timing_uncertainty_us: int
    energy_basis: str
    banner: str = SIM_ONLY_BANNER
    authoritative: bool = False

    def as_rows(self) -> tuple[tuple[str, str], ...]:
        return (
            ("policy_version", self.policy_version),
            ("config_release_sha", self.config_release_sha),
            ("horizontal_margin_mm", str(self.horizontal_margin_mm)),
            ("vertical_margin_mm", str(self.vertical_margin_mm)),
            ("timing_uncertainty_us", str(self.timing_uncertainty_us)),
            ("energy_basis", self.energy_basis),
            ("banner", self.banner),
        )


@dataclass(frozen=True)
class AlternativeOption:
    """One alternative the operator could choose instead, priced."""

    label: str
    aircraft_id: str | None
    expected_delay_us: int
    energy_margin_wh: int
    units: int
    note: str

    def as_row(self) -> tuple[str, str, int, int, int]:
        return (
            self.label,
            self.aircraft_id or "-",
            self.expected_delay_us,
            self.energy_margin_wh,
            self.units,
        )


@dataclass(frozen=True)
class ChangePreview:
    """Everything section 13 says a preview must return."""

    proposal_id: str
    mission_id: str
    aircraft_id: str
    plan_and_config_hash: str
    affected_resources: tuple[tuple[str, int, int], ...]
    expected_delay_us: int
    energy_margin_wh: int
    alternatives: tuple[AlternativeOption, ...]
    affected_pilots: tuple[tuple[str, str], ...]
    safety_parameters: SafetyParameters
    consequences: tuple[str, ...]
    expires_at_us: int
    created_at_us: int
    banner: str = SIM_ONLY_BANNER
    authoritative: bool = False
    safety_claim: bool = False

    def __post_init__(self) -> None:
        if self.expires_at_us <= self.created_at_us:
            raise ProposalStateError("a preview that expires before it exists is not a preview")
        if not self.consequences:
            raise ProposalStateError("a preview must state the consequences of accepting")
        if not self.plan_and_config_hash:
            raise ProposalStateError("a preview must carry the plan and configuration hash")

    def digest(self) -> str:
        return digest_of(
            [
                ("proposal_id", self.proposal_id),
                ("mission_id", self.mission_id),
                ("aircraft_id", self.aircraft_id),
                ("plan_and_config_hash", self.plan_and_config_hash),
                ("expected_delay_us", self.expected_delay_us),
                ("energy_margin_wh", self.energy_margin_wh),
                (
                    "affected_resources",
                    ";".join(f"{k}:{a}-{b}" for k, a, b in self.affected_resources),
                ),
                (
                    "alternatives",
                    ";".join("|".join(map(str, row.as_row())) for row in self.alternatives),
                ),
                ("pilots", ";".join(f"{p}:{r}" for p, r in self.affected_pilots)),
                ("consequences", ";".join(self.consequences)),
                ("expires_at_us", self.expires_at_us),
                *self.safety_parameters.as_rows(),
            ]
        )

    def as_document(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "mission_id": self.mission_id,
            "aircraft_id": self.aircraft_id,
            "plan_and_config_hash": self.plan_and_config_hash,
            "affected_resources": [list(row) for row in self.affected_resources],
            "expected_delay_us": self.expected_delay_us,
            "energy_margin_wh": self.energy_margin_wh,
            "alternatives": [list(row.as_row()) for row in self.alternatives],
            "affected_pilots": [list(row) for row in self.affected_pilots],
            "safety_parameters": dict(self.safety_parameters.as_rows()),
            "consequences": list(self.consequences),
            "expires_at_us": self.expires_at_us,
            "banner": self.banner,
            "command_authority": COMMAND_AUTHORITY,
            "authoritative": False,
            "safety_claim": False,
        }


@dataclass(frozen=True)
class AcceptanceToken:
    """Identity, role, reason, second factor, idempotent token - all five or nothing."""

    operator_id: str
    role: str
    reason: str
    second_factor: bool
    idempotency_token: str

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("operator_id", self.operator_id),
                ("role", self.role),
                ("reason", self.reason),
                ("idempotency_token", self.idempotency_token),
            )
            if not value.strip()
        ]
        if not self.second_factor:
            missing.append("second_factor")
        if missing:
            raise IdentityRequiredError(
                "acceptance requires " + ", ".join(missing) + " (section 13 protocol)"
            )

    def key(self) -> str:
        return self.idempotency_token


@dataclass(frozen=True)
class AcceptanceRecord:
    """The audit row an acceptance writes. Atomic with the decision, by contract."""

    proposal_id: str
    state: ProposalState
    operator_id: str
    role: str
    reason: str
    idempotency_token: str
    recorded_at_us: int
    commitment_intact: bool
    command_issued: bool = False
    banner: str = SIM_ONLY_BANNER

    def digest(self) -> str:
        return digest_of(
            [
                ("proposal_id", self.proposal_id),
                ("state", self.state.value),
                ("operator_id", self.operator_id),
                ("role", self.role),
                ("reason", self.reason),
                ("idempotency_token", self.idempotency_token),
                ("recorded_at_us", self.recorded_at_us),
                ("commitment_intact", self.commitment_intact),
                ("command_issued", self.command_issued),
            ]
        )


class AirborneChangeProposal:
    """One pending request to change an airborne or committed mission."""

    def __init__(
        self,
        *,
        preview: ChangePreview,
        target: MissionStatusRow,
        requesting: MissionStatusRow,
        policy: DispatchPolicy,
    ) -> None:
        authority = airborne_change_authority(requesting)
        if authority is not None:
            raise ProposalStateError(
                f"{authority.code}: {authority.reason}; no proposal may even be drafted"
            )
        if not target.is_protected():
            raise ProposalStateError(
                f"{target.mission_id} is unlaunched: resequence it in the queue instead of "
                "raising a human-acceptance proposal for it"
            )
        self.preview = preview
        self.target = target
        self.requesting = requesting
        self.policy_version = policy.policy_version
        self.state = ProposalState.PENDING
        self.records: tuple[AcceptanceRecord, ...] = ()

    @property
    def proposal_id(self) -> str:
        return self.preview.proposal_id

    @property
    def commitment_intact(self) -> bool:
        """True unless an operator accepted. Decline and expiry keep it True."""
        return self.state is not ProposalState.ACCEPTED

    @property
    def command_issued(self) -> bool:
        return False

    def accept(self, token: AcceptanceToken, *, at_us: int) -> AcceptanceRecord:
        """Record an acceptance, or return the existing one for the same token."""
        self._expire_if_due(at_us)
        repeat = self._repeat(token)
        if repeat is not None:
            return repeat
        if self.state is not ProposalState.PENDING:
            raise ProposalStateError(
                f"proposal {self.proposal_id} is {self.state.value}; a different operator "
                "cannot re-open it with a new token"
            )
        record = self._record(ProposalState.ACCEPTED, token, at_us, commitment_intact=False)
        self.state = ProposalState.ACCEPTED
        return record

    def decline(self, token: AcceptanceToken, *, at_us: int) -> AcceptanceRecord:
        self._expire_if_due(at_us)
        repeat = self._repeat(token)
        if repeat is not None:
            return repeat
        if self.state is not ProposalState.PENDING:
            raise ProposalStateError(f"proposal {self.proposal_id} is {self.state.value}")
        record = self._record(ProposalState.DECLINED, token, at_us, commitment_intact=True)
        self.state = ProposalState.DECLINED
        return record

    def expire(self, at_us: int) -> AcceptanceRecord | None:
        """Lapse the proposal. The commitment is untouched, which is the whole point."""
        if self.state is not ProposalState.PENDING or at_us < self.preview.expires_at_us:
            return None
        self.state = ProposalState.EXPIRED
        return self._system_record(ProposalState.EXPIRED, at_us, commitment_intact=True)

    def _expire_if_due(self, at_us: int) -> None:
        if self.state is ProposalState.PENDING and at_us >= self.preview.expires_at_us:
            self.state = ProposalState.EXPIRED
            self._system_record(ProposalState.EXPIRED, at_us, commitment_intact=True)

    def _system_record(
        self, state: ProposalState, at_us: int, *, commitment_intact: bool
    ) -> AcceptanceRecord:
        record = AcceptanceRecord(
            proposal_id=self.proposal_id,
            state=state,
            operator_id="system:expiry",
            role="kernel",
            reason="the preview window closed without a decision",
            idempotency_token=f"expiry:{self.proposal_id}",
            recorded_at_us=at_us,
            commitment_intact=commitment_intact,
        )
        self.records = (*self.records, record)
        return record

    def _repeat(self, token: AcceptanceToken) -> AcceptanceRecord | None:
        for record in self.records:
            if record.idempotency_token == token.key():
                if record.operator_id != token.operator_id or record.role != token.role:
                    raise ProposalStateError(
                        f"idempotency token {token.key()} was already used by another operator"
                    )
                return record
        return None

    def _record(
        self, state: ProposalState, token: AcceptanceToken, at_us: int, *, commitment_intact: bool
    ) -> AcceptanceRecord:
        record = AcceptanceRecord(
            proposal_id=self.proposal_id,
            state=state,
            operator_id=token.operator_id,
            role=token.role,
            reason=token.reason,
            idempotency_token=token.key(),
            recorded_at_us=at_us,
            commitment_intact=commitment_intact,
        )
        self.records = (*self.records, record)
        return record

    def status(self) -> tuple[tuple[str, object], ...]:
        return (
            ("proposal_id", self.proposal_id),
            ("state", self.state.value),
            ("commitment_intact", self.commitment_intact),
            ("command_issued", self.command_issued),
            ("target_mission_id", self.target.mission_id),
            ("requesting_priority", self.requesting.priority.value),
            ("policy_version", self.policy_version),
            ("preview_digest", self.preview.digest()),
        )


def draft_airborne_change(
    *,
    preview: ChangePreview,
    target: MissionStatusRow,
    requesting: MissionStatusRow,
    policy: DispatchPolicy,
) -> tuple[AirborneChangeProposal | None, ResequenceRefusal | None]:
    """Ask first: ``(proposal, refusal)`` - exactly one of the two is not ``None``."""
    authority = airborne_change_authority(requesting)
    if authority is not None:
        return None, authority
    if not target.is_protected():
        return None, ResequenceRefusal(
            requesting_mission_id=requesting.mission_id,
            target_mission_id=target.mission_id,
            reason=(
                f"{target.mission_id} has not launched; priority resequences it directly "
                "and no operator acceptance is needed"
            ),
            code="unlaunched_needs_no_proposal",
            target_protected=False,
        )
    return AirborneChangeProposal(
        preview=preview, target=target, requesting=requesting, policy=policy
    ), None


def register_of(proposals: Sequence[AirborneChangeProposal]) -> dict[str, AirborneChangeProposal]:
    """Keyed by proposal id, for a caller that has several open at once."""
    return {proposal.proposal_id: proposal for proposal in proposals}


__all__ = [
    "COMMAND_AUTHORITY",
    "AcceptanceRecord",
    "AcceptanceToken",
    "AirborneChangeProposal",
    "AlternativeOption",
    "ChangePreview",
    "ProposalState",
    "SafetyParameters",
    "draft_airborne_change",
    "register_of",
]
