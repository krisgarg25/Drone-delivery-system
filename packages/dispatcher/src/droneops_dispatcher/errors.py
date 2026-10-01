"""Dispatcher errors: every refusal is typed, named and explainable.

Nothing here is raised to signal an ordinary rejection - rejections are *values*
(:class:`droneops_dispatcher.rejection.RejectionExplanation`) that go into a
decision record, because Phase 4's console reads decisions from records and a
stack trace is not a record. These exceptions mark a caller that asked an illegal
question: an unversioned policy, a request to overwrite an airborne commitment, a
proposal accepted twice with two different tokens.
"""

from __future__ import annotations


class DispatcherError(Exception):
    """Base class for every dispatcher refusal."""


class DispatchInputError(DispatcherError):
    """The caller supplied inputs the dispatcher cannot honestly evaluate."""


class PolicyVersionError(DispatcherError):
    """A dispatch policy without a version, a starvation bound or valid weights."""


class SolverDeterminismError(DispatcherError):
    """Solver settings that would silently break replay (see ``policy``)."""


class EligibilityQueryError(DispatcherError):
    """A fleet view that cannot be turned into a candidate query."""


class FairnessPolicyError(DispatcherError):
    """A fairness parameter that would let a mission starve without a bound."""


class CommitmentImmutableError(DispatcherError):
    """A request to delete, displace or downgrade a committed or airborne mission."""


class ProposalStateError(DispatcherError):
    """A proposal accepted, declined or expired twice, or out of order."""


class IdentityRequiredError(DispatcherError):
    """An acceptance without identity, role, reason, second factor or token."""


class WeakenedBaselineError(DispatcherError):
    """A strategy comparison whose arms did not receive identical inputs."""


class InsufficientRepetitionsError(DispatcherError):
    """Fewer seeded repetitions than the comparison claims to have run."""
