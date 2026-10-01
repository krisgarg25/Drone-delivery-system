"""Battery-model disagreement is a review trigger, never an acceptance.

The failure model (architecture section 19) says: *battery model disagreement -
do not accept; require review or alternate aircraft*. Two profiles that both claim
to describe the same airframe and disagree about whether a mission fits are a
symptom that one of them is wrong, and a dispatch decision cannot rest on which
one the code happened to read first.

So the same mission is run against every supplied profile version and the results
are compared. The tolerance is a named, versioned policy object rather than a
literal inside a comparison, and the outcome is three-valued - agree, disagree,
review - mirroring the conflict checker's ``UNPROVEN`` discipline: uncertain means
blocked, not cleared.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .errors import EnergyError
from .feasibility import MarginReport, MissionEnergyRequest, RejectReason, assess_mission
from .profile import BatteryProfileVersion


class Agreement(StrEnum):
    """Whether independent battery models reach the same conclusion."""

    AGREE_FEASIBLE = "agree_feasible"
    AGREE_INFEASIBLE = "agree_infeasible"
    DISAGREE_ON_VERDICT = "disagree_on_verdict"
    DISAGREE_ON_MARGIN = "disagree_on_margin"


@dataclass(frozen=True)
class ReviewPolicy:
    """How much battery-model spread a reviewer will not be paged about.

    Versioned and explicitly illustrative, like the profiles: the tolerance is an
    experiment parameter, and a run must record which one was in force. Dropping
    ``illustrative`` requires a claim of authority this build cannot make.
    """

    policy_version: str
    tolerance_wh: int = 15
    illustrative: bool = True

    def __post_init__(self) -> None:
        if self.tolerance_wh < 0:
            raise EnergyError("a battery review tolerance cannot be negative")
        if not self.illustrative:
            raise EnergyError(
                f"{self.policy_version}: a review tolerance may not drop its illustrative "
                "flag until it is backed by vendor or field data"
            )


#: The only review policy shipped in Phase 3: a 15 Wh spread, about 4 % of the
#: 400 Wh illustrative pack. No measured basis - it is a placeholder to review.
DEFAULT_REVIEW_POLICY = ReviewPolicy(
    policy_version="battery-review-illustrative-1", tolerance_wh=15
)


@dataclass(frozen=True)
class ModelComparison:
    """One battery model's answer to the same complete-mission question."""

    profile_id: str
    parameters_sha256: str
    margin_wh: int
    computed: bool
    feasible: bool
    note: str = ""

    def as_row(self) -> tuple[str, int, bool]:
        """``(profile id, margin, feasible)`` for a review table."""
        return (self.profile_id, self.margin_wh, self.feasible)


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of cross-checking battery models for one mission."""

    primary: MarginReport
    agreement: Agreement
    review_required: bool
    comparisons: tuple[ModelComparison, ...]
    policy_version: str
    spread_wh: int = field(default=0)

    @property
    def accepted(self) -> bool:
        """Only a feasible *and* agreed mission is accepted; a review blocks it."""
        return self.primary.feasible and not self.review_required

    @property
    def disclaimer(self) -> str:
        """The basis statement travels with the review, not just the report."""
        return self.primary.disclaimer

    def recommendation(self) -> str:
        """What a human should do next, phrased for a review queue."""
        if self.accepted:
            return "Proceed: independent battery models agree the complete mission fits."
        if self.agreement is Agreement.DISAGREE_ON_VERDICT:
            return (
                "Do not accept. Battery models disagree on feasibility: require review or "
                "assign an alternate aircraft (failure model, architecture section 19)."
            )
        if self.review_required:
            return (
                f"Do not accept. Margin spread {self.spread_wh} Wh exceeds the "
                f"{self.policy_version} tolerance; reconcile the profiles before dispatch."
            )
        return "Not feasible on any battery model: reject and offer an alternate aircraft."


def _compare(
    request: MissionEnergyRequest,
    profile: BatteryProfileVersion,
    *,
    primary: bool,
) -> tuple[ModelComparison, MarginReport | None]:
    """Evaluate one profile, turning a refusal to compute into an infeasible row."""
    probe = request if primary else replace(request, profile=profile)
    try:
        report = assess_mission(probe)
    except EnergyError as error:
        # A model that cannot carry the load does not get to vote "feasible".
        return (
            ModelComparison(
                profile_id=profile.profile_id,
                parameters_sha256=profile.parameters_sha256,
                margin_wh=0,
                computed=False,
                feasible=False,
                note=str(error),
            ),
            None,
        )
    return (
        ModelComparison(
            profile_id=profile.profile_id,
            parameters_sha256=profile.parameters_sha256,
            margin_wh=report.margin_wh,
            computed=True,
            feasible=report.feasible,
        ),
        report,
    )


def compare_battery_models(
    request: MissionEnergyRequest,
    profiles: Sequence[BatteryProfileVersion],
    *,
    policy: ReviewPolicy = DEFAULT_REVIEW_POLICY,
) -> ReviewOutcome:
    """Run the same complete mission against several battery model versions.

    ``request.profile`` is the primary model and must itself compute; every other
    model is a cross-check. A model that refuses to compute - typically because it
    cannot carry the payload - is an infeasible row, which is precisely the
    divergence this check exists to surface.
    """
    if not profiles:
        raise EnergyError("cross-checking battery models needs at least one profile")
    comparisons: list[ModelComparison] = []
    reports: list[MarginReport] = []
    for index, candidate in enumerate(profiles):
        comparison, report = _compare(request, candidate, primary=index == 0)
        comparisons.append(comparison)
        if report is not None:
            reports.append(report)
    if not reports:
        raise EnergyError(
            "no battery model could evaluate this mission: "
            + "; ".join(f"{row.profile_id}: {row.note}" for row in comparisons)
        )

    verdicts = {row.feasible for row in comparisons}
    margins = [row.margin_wh for row in comparisons]
    spread = max(margins) - min(margins)
    if len(verdicts) > 1:
        agreement = Agreement.DISAGREE_ON_VERDICT
    elif spread > policy.tolerance_wh:
        agreement = Agreement.DISAGREE_ON_MARGIN
    elif True in verdicts:
        agreement = Agreement.AGREE_FEASIBLE
    else:
        agreement = Agreement.AGREE_INFEASIBLE

    review_required = agreement in {
        Agreement.DISAGREE_ON_VERDICT,
        Agreement.DISAGREE_ON_MARGIN,
    }
    primary = reports[0]
    if review_required:
        reasons = {*primary.reasons, RejectReason.BATTERY_MODELS_DISAGREE}
        primary = replace(
            primary,
            feasible=False,
            reasons=tuple(sorted(reasons, key=lambda reason: reason.value)),
        )
    return ReviewOutcome(
        primary=primary,
        agreement=agreement,
        review_required=review_required,
        comparisons=tuple(comparisons),
        policy_version=policy.policy_version,
        spread_wh=spread,
    )


__all__ = [
    "DEFAULT_REVIEW_POLICY",
    "Agreement",
    "ModelComparison",
    "ReviewOutcome",
    "ReviewPolicy",
    "compare_battery_models",
]
