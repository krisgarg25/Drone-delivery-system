"""Reading a golden scenario bundle: a file on disk, not a fixture in code.

``technical-build/06`` makes the golden conflict scenarios the acceptance evidence
for Phase 2, and evidence a test generates in its own ``setUp`` is not evidence.
So each scenario is a machine-readable document that carries its raw plan points,
its policy release, its expected verdict, the invariant it guards, the metrics it
has to reproduce, and the mandatory field that keeps the suite honest: **what this
scenario does not prove**. A bundle that cannot be read is a build failure, not a
skipped test.

The document shape is the *record* shape on purpose: ``plan_version``, ``status``,
``points[]`` with absolute UTC instants and geodetic positions, ``resources[]`` as
canonical keys. Nothing here knows about a planner, a route object or a corridor
edge, so a bundle can only ever describe what a ledger row would contain.

Every list is sorted by the order the field's own documentation gives it, and
repeated keys are rejected rather than merged, so two bundles that differ only in
the order a human wrote them in are the same scenario - and a bundle that
contradicts itself is refused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from droneops_geom_core import GridSpec, Millimetre

from .checks import Basis, ConflictReport, check_pair, check_set
from .policy import AllowRuleSet, CheckerPolicy, allow_rule_from_mapping, policy_from_mapping
from .records import CommittedPlanRecord, EvaluationContext, PlannerClaim, PlanPoint
from .verdict import PairVerdict

#: One second, because the bundles index time in seconds from the scenario's start.
_SECONDS_UNIT: Final[str] = "second"

_REQUIRED_BUNDLE_KEYS: Final[tuple[str, ...]] = (
    "scenario_id",
    "title",
    "description",
    "invariant",
    "what_this_does_not_prove",
    "policy",
    "evaluation",
    "records",
    "expected",
)


class BundleError(ValueError):
    """A scenario document is incomplete, contradictory or unreadable."""


@dataclass(frozen=True)
class ScenarioExpectation:
    """What a bundle claims the checker will say, and what the claim is worth."""

    verdict: str
    pair: tuple[str, str] | None
    checks: frozenset[str]
    metrics: Mapping[str, float]
    invariants: tuple[str, ...]
    description: str
    does_not_prove: tuple[str, ...]
    basis: Basis
    step_seconds: float | None

    @property
    def requires_contacts(self) -> bool:
        return self.verdict == "CONFLICT"

    @property
    def requires_reasons(self) -> bool:
        return self.verdict == "UNPROVEN"


@dataclass(frozen=True)
class ScenarioBundle:
    """One golden conflict scenario, ready to run."""

    scenario_id: str
    title: str
    description: str
    invariant: str
    does_not_prove: tuple[str, ...]
    policy: CheckerPolicy
    context: EvaluationContext
    records: tuple[CommittedPlanRecord, ...]
    expectations: tuple[ScenarioExpectation, ...]
    rules: AllowRuleSet
    release_allowed: bool

    def record(self, plan_version: str) -> CommittedPlanRecord:
        """Look a record up by plan version.

        Raises:
            BundleError: no record, or two records with the same plan version.
        """
        found = [record for record in self.records if record.plan_version == plan_version]
        if len(found) != 1:
            raise BundleError(
                f"{self.scenario_id}: expected exactly one record named {plan_version!r}, "
                f"found {len(found)}"
            )
        return found[0]

    def evaluate(self, expectation: ScenarioExpectation) -> tuple[PairVerdict, ConflictReport]:
        """Run one expectation on its own basis.

        Each expectation carries the construction it was written against, so a
        bundle can hold a sweep answer and a frame answer for the *same* pair in
        the same file. Re-deriving the basis from the bundle instead would make
        gc08's whole point - that the two differ - impossible to express.
        """
        records = (
            (self.record(expectation.pair[0]), self.record(expectation.pair[1]))
            if expectation.pair is not None
            else (self.records[0], self.records[1])
        )
        step = _step_us(expectation)
        verdict = check_pair(
            records[0], records[1], self.context, basis=expectation.basis, step_us=step,
            rules=self.rules,
        )
        report = check_set(
            self.records, self.context, basis=expectation.basis, step_us=step, rules=self.rules
        )
        return verdict, report

    def run(self) -> tuple[PairVerdict | None, ConflictReport]:
        """Evaluate the scenario's first expectation, and the whole set."""
        return self.evaluate(self.expectations[0])


def _step_us(expectation: ScenarioExpectation) -> int | None:
    if expectation.step_seconds is None:
        return None
    step = int(round(expectation.step_seconds * 1_000_000))
    if step <= 0:
        raise BundleError(f"{expectation.basis} needs a positive sampling interval")
    return step


# --------------------------------------------------------------------------- document reading
def _require(document: Mapping[str, object], key: str, where: str) -> object:
    if key not in document:
        raise BundleError(f"{where}: required field {key!r} is missing")
    return document[key]


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise BundleError(f"{where} must be a mapping, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, where: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise BundleError(f"{where} must be a list, got {type(value).__name__}")
    return tuple(value)


def _instant(value: object, where: str) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise BundleError(f"{where}: {value!r} has no UTC offset")
    return stamp


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleError(f"{where} must be an integer, got {value!r}")
    return int(value)


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BundleError(f"{where} must be a number, got {value!r}")
    return float(value)


def record_from_mapping(document: Mapping[str, object], where: str) -> CommittedPlanRecord:
    """One committed record, exactly as a bundle writes it.

    Raises:
        BundleError: a field is missing or the points are not a list of positions.
    """
    item = _mapping(document, where)
    origin = _instant(_require(item, "time_origin_utc", where), f"{where}.time_origin_utc")
    raw_points = _sequence(_require(item, "points", where), f"{where}.points")
    points: list[PlanPoint] = []
    for index, entry in enumerate(raw_points):
        point = _mapping(entry, f"{where}.points[{index}]")
        label = f"{where}.points[{index}]"
        # A bundle may index time either way: an absolute instant, or whole and
        # fractional seconds from the scenario's start. Both mean absolute time once
        # the origin is fixed, and only the first one can hide a mistake.
        if "at_utc" in point:
            at = _instant(point["at_utc"], f"{label}.at_utc")
        else:
            offset = _number(point.get("t_s", 0), f"{label}.t_s")
            at = origin + timedelta(seconds=offset)
        reference = point.get("altitude_reference")
        points.append(
            PlanPoint(
                at=at,
                lat_deg=_number(_require(point, "lat", label), f"{label}.lat"),
                lon_deg=_number(_require(point, "lon", label), f"{label}.lon"),
                altitude_m=_number(_require(point, "altitude_m", label), f"{label}.altitude_m"),
                altitude_reference=None if reference is None else str(reference),
                speed_mm_per_s=_integer(point.get("speed_mm_per_s", 0), f"{label}.speed_mm_per_s"),
            )
        )
    claim = item.get("planner_claim")
    planner: PlannerClaim | None = None
    if claim is not None:
        document_claim = _mapping(claim, f"{where}.planner_claim")
        planner = PlannerClaim(
            verdict=str(_require(document_claim, "verdict", f"{where}.planner_claim.verdict")),
            policy_version=str(_require(document_claim, "policy_version", "planner_claim")),
            allow_rule_id=(None if document_claim.get("allow_rule_id") is None
                           else str(document_claim["allow_rule_id"])),
            allow_rule_version=(None if document_claim.get("allow_rule_version") is None
                                else str(document_claim["allow_rule_version"])),
        )
    expiry = item.get("restriction_expires_at_utc")
    fix = item.get("telemetry_last_fix_utc")
    radius = _integer(_require(item, "body_radius_mm", where), "body_radius_mm")
    half_height = _integer(_require(item, "body_half_height_mm", where), "body_half_height_mm")
    return CommittedPlanRecord(
        plan_version=str(_require(item, "plan_version", where)),
        mission_id=str(_require(item, "mission_id", where)),
        status=str(_require(item, "status", where)),
        points=tuple(points),
        resource_keys=tuple(
            sorted({str(key) for key in _sequence(_require(item, "resources", where), "resources")})
        ),
        body_radius_mm=radius,
        body_half_height_mm=half_height,
        policy_release=str(_require(item, "policy_release", where)),
        source=str(item.get("source", "golden-bundle")),
        restriction_expires_at=(None if expiry is None
                                else _instant(expiry, f"{where}.restriction_expires_at_utc")),
        telemetry_last_fix=(None if fix is None else _instant(fix, f"{where}.telemetry_last_fix")),
        planner_claim=planner,
    )


def _policy(document: Mapping[str, object], where: str) -> CheckerPolicy:
    return policy_from_mapping(_mapping(document, where))


def _grid(document: Mapping[str, object] | None) -> GridSpec | None:
    """The optional grid, so a bundle can ask the checker to name the cell it hit."""
    if document is None:
        return None
    item = _mapping(document, "evaluation.grid")
    return GridSpec(
        epsg=_integer(item.get("epsg", 32643), "epsg"),
        cell_size_mm=Millimetre(_integer(_require(item, "cell_size_mm", "grid"), "cell_size_mm")),
        vertical_band_mm=Millimetre(
            _integer(_require(item, "vertical_band_mm", "grid"), "vertical_band_mm")
        ),
    )


def _pair(value: object, where: str) -> tuple[str, str] | None:
    """A named plan-version pair, canonicalised so argument order cannot matter."""
    if value is None:
        return None
    entries = sorted(str(name) for name in _sequence(value, where))
    if len(entries) != 2:
        raise BundleError(f"{where} must name exactly two plan versions")
    return (entries[0], entries[1])


def _basis(value: object, where: str) -> Basis:
    """One of the three bases, spelled out rather than cast: the type is the contract."""
    name = str(value)
    if name == "CONTINUOUS":
        return "CONTINUOUS"
    if name == "DISCRETISED":
        return "DISCRETISED"
    if name == "FRAME":
        return "FRAME"
    raise BundleError(f"{where} {name!r} is not one of CONTINUOUS, DISCRETISED, FRAME")


def _expectation(document: Mapping[str, object], where: str) -> ScenarioExpectation:
    item = _mapping(document, where)
    metrics = _mapping(item.get("metrics", {}), f"{where}.metrics")
    step = item.get("step_seconds")
    return ScenarioExpectation(
        verdict=str(_require(item, "verdict", f"{where}.verdict")),
        pair=_pair(item.get("pair"), f"{where}.pair"),
        checks=frozenset(str(name) for name in _sequence(item.get("checks", []), f"{where}.checks")),
        metrics={str(key): _number(value, f'{where}.metrics.{key}')
                 for key, value in metrics.items()},
        invariants=tuple(sorted(
            str(name) for name in _sequence(item.get("invariants", []), f"{where}.invariants")
        )),
        description=str(item.get("description", "")),
        does_not_prove=tuple(
            str(text) for text in _sequence(
                item.get("what_this_does_not_prove", []), f"{where}.what_this_does_not_prove"
            )
        ),
        basis=_basis(item.get("basis", "CONTINUOUS"), f"{where}.basis"),
        step_seconds=None if step is None else _number(step, f"{where}.step_seconds"),
    )


def bundle_from_document(document: Mapping[str, object], where: str = "scenario") -> ScenarioBundle:
    """Read a whole scenario bundle. Every defect in the file is an error, not a default.

    Raises:
        BundleError: a required field is missing, a policy cannot be read, a record
            has no points, or the expectations name a record the bundle does not contain.
    """
    item = _mapping(document, where)
    for key in _REQUIRED_BUNDLE_KEYS:
        if key not in item:
            raise BundleError(f"{where}: field {key!r} is required (a bundle that cannot be "
                              "read is a build failure, not a skipped test)")
    policy = _policy(_mapping(item["policy"], f"{where}.policy"), f"{where}.policy")
    evaluation = _mapping(item["evaluation"], f"{where}.evaluation")
    adjacency = {
        str(key): tuple(sorted(str(value) for value in _sequence(neighbours, "adjacency")))
        for key, neighbours in _mapping(
            evaluation.get("adjacency", {}), f"{where}.evaluation.adjacency"
        ).items()
    }
    budget = evaluation.get("telemetry_budget_s")
    context = EvaluationContext(
        policy=policy,
        evaluated_at=_instant(
            _require(evaluation, "evaluated_at_utc", f"{where}.evaluation"), "evaluated_at"
        ),
        adjacency=adjacency,
        telemetry_budget_us=0 if budget is None else int(_number(budget, "telemetry_budget_s")
                                                         * 1_000_000),
        grid=_grid(None if evaluation.get("grid") is None else _mapping(evaluation["grid"], f"{where}.evaluation.grid")),
    )
    records = tuple(
        sorted(
            (
                record_from_mapping(_mapping(entry, f"{where}.records[{index}]"),
                                    f"{where}.records[{index}]")
                for index, entry in enumerate(_sequence(item["records"], f"{where}.records"))
            ),
            key=lambda record: record.plan_version,
        )
    )
    if len({record.plan_version for record in records}) != len(records):
        raise BundleError(f"{where}: two records share a plan_version, so a pair is ambiguous")
    rules = AllowRuleSet(
        rules=tuple(
            sorted(
                (
                    allow_rule_from_mapping(_mapping(entry, f"{where}.allow_rules[{index}]"))
                    for index, entry in enumerate(_sequence(item.get("allow_rules", []), "allow_rules"))
                ),
                key=lambda rule: (rule.rule_id, rule.rule_version),
            )
        )
    )
    expectations = tuple(
        _expectation(_mapping(entry, f"{where}.expected[{index}]"), f"{where}.expected[{index}]")
        for index, entry in enumerate(_sequence(item["expected"], f"{where}.expected"))
    )
    if not expectations:
        raise BundleError(f"{where}: `expected` needs at least one verdict")
    for expectation in expectations:
        if expectation.pair is not None:
            for name in expectation.pair:
                if name not in {record.plan_version for record in records}:
                    raise BundleError(f"{where}: expectation names unknown record {name!r}")
        if expectation.does_not_prove == () and expectation.verdict != "CONFLICT":
            raise BundleError(f"{where}: every expectation must say what it does not prove")
    release = bool(item.get("release_allowed", False))
    return ScenarioBundle(
        scenario_id=str(_require(item, "scenario_id", where)),
        title=str(_require(item, "title", where)),
        description=str(_require(item, "description", where)),
        invariant=str(_require(item, "invariant", where)),
        does_not_prove=tuple(
            str(text) for text in _sequence(item["what_this_does_not_prove"], "what_not_proven")
        ),
        policy=policy,
        context=context,
        records=records,
        expectations=expectations,
        rules=rules,
        release_allowed=release,
    )


def load_bundle(path: Path | str) -> ScenarioBundle:
    """Read one bundle from disk.

    Raises:
        BundleError: the file is missing, is not JSON, or is not a complete bundle.
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise BundleError(f"no scenario bundle at {candidate}")
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleError(f"{candidate}: unreadable JSON ({error})") from error
    if not isinstance(document, dict):
        raise BundleError(f"{candidate}: a bundle must be a JSON object")
    return bundle_from_document({str(key): value for key, value in document.items()}, str(candidate))


def bundles_in(directory: Path | str) -> Sequence[ScenarioBundle]:
    """Every bundle in a directory, in filename order, so a suite run is reproducible."""
    root = Path(directory)
    files = sorted(path for path in root.glob("*.json"))
    return tuple(load_bundle(path) for path in files)
