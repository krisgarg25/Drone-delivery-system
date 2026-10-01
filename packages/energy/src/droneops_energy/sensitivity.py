"""Documented sensitivity: how much the margin moves when an input moves.

Every coefficient behind a margin is illustrative, so a margin on its own invites
false confidence. The Phase 3 exit criteria ask for trade-off visibility, so the
model exposes a sweep: re-run the *whole* kernel (not a linear approximation) at a
grid of perturbations per input and report where the decision flips. Reviewers can
then see, for example, that the mission's feasibility is worth about one watt-hour
per 25 grams of payload but eight watt-hours per metre per second of headwind -
which input to go and measure first.

Steps are plain integers in each knob's own unit, the sweep order is fixed, and no
random or wall-clock input is consulted, so a sweep replays identically.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from enum import StrEnum

from .errors import EnergyError
from .feasibility import MarginReport, MissionEnergyRequest, TermName, assess_mission
from .numeric import BP_PER_UNIT
from .weather import WindSample, WindSnapshot


class Knob(StrEnum):
    """An input a reviewer can perturb, with the unit its delta is in."""

    SUSTAINED_WIND_DMPS = "sustained_wind_dmps"
    GUST_DMPS = "gust_dmps"
    WIND_REVERSAL = "wind_reversal"
    PAYLOAD_G = "payload_g"
    ROUTE_SCALE_BP = "route_scale_bp"
    HANDOVER_US = "handover_us"
    CHARGE_BP = "charge_bp"
    STATE_OF_HEALTH_BP = "state_of_health_bp"


#: The knob that dominates each margin term, in the order a report lists them.
SWEEP_ORDER: tuple[Knob, ...] = (
    Knob.SUSTAINED_WIND_DMPS,
    Knob.WIND_REVERSAL,
    Knob.GUST_DMPS,
    Knob.PAYLOAD_G,
    Knob.ROUTE_SCALE_BP,
    Knob.HANDOVER_US,
    Knob.CHARGE_BP,
    Knob.STATE_OF_HEALTH_BP,
)

#: Deltas per knob, in that knob's unit. Chosen to span the plausible Chandigarh
#: synthetic range, not tuned to any measured aircraft.
DEFAULT_STEPS: dict[Knob, tuple[int, ...]] = {
    Knob.SUSTAINED_WIND_DMPS: (-40, -20, 20, 40, 60),
    Knob.GUST_DMPS: (0, 20, 40, 80),
    Knob.WIND_REVERSAL: (0, 1),
    Knob.PAYLOAD_G: (-500, -250, 250, 500, 1000),
    Knob.ROUTE_SCALE_BP: (7500, 9000, 11000, 12500),
    Knob.HANDOVER_US: (0, 30_000_000, 60_000_000),
    Knob.CHARGE_BP: (-2000, -1000, 1000),
    Knob.STATE_OF_HEALTH_BP: (-3000, -1500, 1500),
}


def _scale_routes(request: MissionEnergyRequest, factor_bp: int) -> MissionEnergyRequest:
    """Scale every horizontal distance in the mission, recovery included."""
    return replace(
        request,
        outbound=request.outbound.scaled_by_bp(factor_bp),
        handover=request.handover.scaled_by_bp(factor_bp),
        recovery=None if request.recovery is None else request.recovery.scaled_by_bp(factor_bp),
    )


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _wind_speed(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, weather=request.weather.shifted_by(delta))


def _gust(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, weather=_gust_shifted(request.weather, delta))


def _reversal(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, weather=request.weather.reversed() if delta else request.weather)


def _payload(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, payload_g=max(0, request.payload_g + delta))


def _distance(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return _scale_routes(request, delta)


def _hover(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    handover = request.handover
    held = handover.with_hover_us(max(0, handover.hover_us + delta))
    return replace(request, handover=held)


def _charge(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, charge_bp=_bounded(request.charge_bp + delta, 0, BP_PER_UNIT))


def _health(request: MissionEnergyRequest, delta: int) -> MissionEnergyRequest:
    return replace(request, soh_bp=_bounded(request.soh_bp + delta, 0, BP_PER_UNIT))


#: One handler per knob, so adding a knob is a single registration, not a branch.
_PERTURBATIONS: dict[Knob, Callable[[MissionEnergyRequest, int], MissionEnergyRequest]] = {
    Knob.SUSTAINED_WIND_DMPS: _wind_speed,
    Knob.GUST_DMPS: _gust,
    Knob.WIND_REVERSAL: _reversal,
    Knob.PAYLOAD_G: _payload,
    Knob.ROUTE_SCALE_BP: _distance,
    Knob.HANDOVER_US: _hover,
    Knob.CHARGE_BP: _charge,
    Knob.STATE_OF_HEALTH_BP: _health,
}


def perturb(request: MissionEnergyRequest, knob: Knob, delta: int) -> MissionEnergyRequest:
    """Return the same mission with one input moved, everything else held.

    Battery state deltas are clamped into ``[0, 10000]`` basis points rather than
    rejected: a sweep that asks for 120 % charge is asking about headroom, and the
    clamp keeps the answer honest instead of raising mid-table.
    """
    try:
        handler = _PERTURBATIONS[knob]
    except KeyError as error:  # pragma: no cover - Knob is a closed enum
        raise EnergyError(f"no perturbation defined for knob {knob}") from error
    return handler(request, delta)


def _gust_shifted(weather: WindSnapshot, delta_dmps: int) -> WindSnapshot:
    """Move only the gust column: the reserve-uplift knob, not the consumption one."""
    return weather.with_samples(
        WindSample(
            minute=row.minute,
            speed_dmps=row.speed_dmps,
            bearing_mdeg=row.bearing_mdeg,
            gust_dmps=max(row.speed_dmps, row.gust_dmps + delta_dmps),
        )
        for row in weather.samples
    )


class KnobResponse:
    """How the margin answered one knob."""

    __slots__ = ("baseline_wh", "knob", "refusals", "rows", "unit")

    def __init__(
        self,
        knob: Knob,
        unit: str,
        baseline_wh: int,
        rows: Sequence[tuple[int, MarginReport]],
        refusals: Sequence[tuple[int, str]] = (),
    ) -> None:
        self.knob = knob
        self.unit = unit
        self.baseline_wh = baseline_wh
        self.rows = tuple(rows)
        self.refusals = tuple(refusals)

    @property
    def max_abs_shift_wh(self) -> int:
        """The largest margin movement this knob produced: the tornado bar."""
        return max((abs(report.margin_wh - self.baseline_wh) for _, report in self.rows), default=0)

    @property
    def flip_delta(self) -> int | None:
        """Smallest |delta| at which feasibility changes state, or ``None``."""
        if not self.rows:
            return None
        baseline_feasible = self.rows[0][1].feasible
        candidates = [delta for delta, report in self.rows if report.feasible != baseline_feasible]
        if not candidates:
            return None
        return min(candidates, key=lambda delta: (abs(delta), delta))

    def as_rows(self) -> tuple[tuple[int, int, bool, TermName | None], ...]:
        """``(delta, margin_wh, feasible, breaking term)`` per step."""
        return tuple(
            (delta, report.margin_wh, report.feasible, report.breaking_term)
            for delta, report in self.rows
        )


class SensitivityReport:
    """A full documented sweep: one :class:`KnobResponse` per input."""

    __slots__ = ("baseline", "rows", "steps_requested")

    def __init__(self, baseline: MarginReport, rows: Sequence[KnobResponse]) -> None:
        self.baseline = baseline
        self.rows = tuple(rows)
        self.steps_requested = sum(len(row.rows) for row in rows)

    @property
    def disclaimer(self) -> str:
        return self.baseline.disclaimer

    @property
    def profile_sha256(self) -> str:
        return self.baseline.profile_sha256

    def knob(self, name: Knob) -> KnobResponse:
        """Look one knob up."""
        for row in self.rows:
            if row.knob is name:
                return row
        raise EnergyError(f"that knob was not swept: {name}")

    def tornado(self) -> tuple[Knob, ...]:
        """Knobs ranked by how far they move the margin: measure this first."""
        return tuple(
            sorted(
                (row.knob for row in self.rows),
                key=lambda name: (-self.knob(name).max_abs_shift_wh, name.value),
            )
        )

    def as_table(self) -> tuple[tuple[str, str, int, int, int | None], ...]:
        """``(knob, unit, max shift Wh, flip delta, margin at flip)`` for a report."""
        rows: list[tuple[str, str, int, int, int | None]] = []
        for response in self.rows:
            flip = response.flip_delta
            margin_at_flip = None
            if flip is not None:
                margin_at_flip = next(
                    report.margin_wh for delta, report in response.rows if delta == flip
                )
            rows.append(
                (
                    response.knob.value,
                    response.unit,
                    response.max_abs_shift_wh,
                    flip if flip is not None else 0,
                    margin_at_flip,
                )
            )
        return tuple(rows)


_UNITS: dict[Knob, str] = {
    Knob.SUSTAINED_WIND_DMPS: "dm/s of sustained wind",
    Knob.GUST_DMPS: "dm/s of peak gust",
    Knob.WIND_REVERSAL: "0 = as frozen, 1 = reversed",
    Knob.PAYLOAD_G: "gram of payload",
    Knob.ROUTE_SCALE_BP: "basis points of route length",
    Knob.HANDOVER_US: "microseconds of handover",
    Knob.CHARGE_BP: "basis points of state of charge",
    Knob.STATE_OF_HEALTH_BP: "basis points of state of health",
}


def sweep_sensitivity(
    request: MissionEnergyRequest,
    *,
    knobs: Sequence[Knob] = SWEEP_ORDER,
) -> SensitivityReport:
    """Re-run the whole kernel over a fixed grid of single-input perturbations.

    Deliberately recomputes rather than differentiating: the model is nonlinear in
    wind (a headwind buys time, and time buys power) and kinked at every curve
    knot, so a local derivative would overstate how well a reviewer knows the
    answer. The step grid is a published constant, not a caller knob: a sweep whose
    grid can be quietly narrowed by a caller is not evidence.
    """
    baseline = assess_mission(request)
    wanted = set(knobs)
    ordered = [knob for knob in SWEEP_ORDER if knob in wanted]
    if len(ordered) != len(wanted):
        raise EnergyError("sweep asked for an unknown knob")
    responses: list[KnobResponse] = []
    for knob in ordered:
        rows: list[tuple[int, MarginReport]] = [(0, baseline)]
        refusals: list[tuple[int, str]] = []
        for delta in DEFAULT_STEPS[knob]:
            probed = perturb(request, knob, delta)
            try:
                rows.append((delta, assess_mission(probed)))
            except EnergyError as error:
                # The profile refused to price this step (an impossible payload, say).
                # It is reported as a refusal, never as a zero-cost row.
                refusals.append((delta, str(error)))
        responses.append(KnobResponse(knob, _UNITS[knob], baseline.margin_wh, rows, refusals))
    return SensitivityReport(baseline, responses)


__all__ = [
    "DEFAULT_STEPS",
    "Knob",
    "KnobResponse",
    "SensitivityReport",
    "SWEEP_ORDER",
    "perturb",
    "sweep_sensitivity",
]
