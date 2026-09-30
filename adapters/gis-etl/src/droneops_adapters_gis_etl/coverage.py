"""The measured vertical-coverage evidence, re-measured on every run.

Spec 12 section 4 quotes numbers from 30 September 2026. Quoting them forever would
turn a measurement into folklore, so the ETL measures them again and writes both the
fresh value and the baseline next to it. If the network is down, the report records
that the claim is *not* current and which cached measurement it is resting on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import GisConfig
from .errors import GisEtlError, ImplausibleEndpointError
from .overpass import OverpassClient, count_breakdown
from .provenance import SOURCE_MODE_LIVE
from .queries import Metric, central_metrics, service_metrics

#: Below this a baseline zero-vs-nonzero is not evidence of a broken mirror; a metric
#: legitimately equal to zero today (say aeroway in a shrunk box) must stay reportable.
PLAUSIBILITY_FLOOR_BASELINE = 100
#: The floor is *one*, not the baseline value. OSM is edited hourly and the 30 September
#: figures cannot be reproduced with these filters, so a smaller number today is a
#: measurement, not a broken mirror. Measured on 2026-09-30 in this run: maps.mail.ru,
#: overpass-api.de, kumi.systems and private.coffee all independently answered 360 power
#: towers where the baseline recorded 2,923, and 29 power lines where it recorded 5,674.
#: Four agreeing mirrors are corroboration; a baseline-sized floor would have thrown all
#: four away. What is never acceptable is a *zero* for features the same endpoints prove
#: exist - and that is exactly what floor=1 rejects.


@dataclass
class MetricResult:
    """One re-measured count with its full provenance."""

    name: str
    description: str
    query_sha256: str
    retrieved_at: str
    endpoint: str
    source_mode: str
    degraded: bool
    response_sha256: str
    raw_path: str
    replication_timestamp: str
    nodes: int
    ways: int
    relations: int
    total: int
    baseline_value: int | None
    delta: int | None
    suspicious: bool = False
    note: str = ""
    chunked: bool = False
    chunks: int = 1
    chunk_query_sha256s: list[str] = field(default_factory=list)
    double_count_possible: bool = False
    primary_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoverageReport:
    """The evidence file: what we measured today, against what was measured on 30 Sep."""

    measured_at: str
    dataset_id: str
    config_version: str
    bbox_service: list[float]
    bbox_central: list[float]
    bbox_shape_km: list[float]
    baseline: dict[str, Any]
    metrics: list[dict[str, Any]]
    failures: list[dict[str, Any]] = field(default_factory=list)
    live_metrics: int = 0
    cached_metrics: int = 0
    coverage: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    claim_current: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> str:
        from .geom import write_json

        return write_json(path, self.as_dict())


def run_coverage(client: OverpassClient, config: GisConfig) -> CoverageReport:
    """Measure every coverage metric. Never abort the whole report over one metric."""
    metrics: list[MetricResult] = []
    failures: list[dict[str, Any]] = []
    notes: list[str] = []
    baseline = config.baseline

    for metric in service_metrics(config.service_bbox) + central_metrics(config.central_bbox):
        expected = _optional_int(baseline.get(metric.baseline_key))
        floor = 1 if expected is not None and expected >= PLAUSIBILITY_FLOOR_BASELINE else 0
        try:
            result = _measure(client, metric, config, floor=floor)
        except (ImplausibleEndpointError, GisEtlError) as exc:
            failures.append(
                {
                    "name": metric.name,
                    "query_sha256": _sha(metric),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:900],
                    "baseline_value": expected,
                }
            )
            notes.append(
                f"{metric.name}: not measured. The baseline value is quoted, not "
                f"confirmed, and the manifest must mark this claim non-current."
            )
            continue
        metrics.append(result)
        if expected and expected >= PLAUSIBILITY_FLOOR_BASELINE and not 0.75 <= result.total / expected <= 1.33:
            notes.append(
                f"{metric.name}: measured {result.total:,} in this run against "
                f"{expected:,} recorded at {baseline.get('retrieved_at', 'the reference time')}. "
                f"Both numbers are kept as they are - the count is not adjusted to the "
                "baseline and the baseline is not overwritten. A divergence this large is "
                "most likely a filter difference in the 30 September query, and it is a "
                "finding for review, not something to hide."
            )

    report = _assemble(config, metrics, failures, notes)
    return report


def _measure(client: OverpassClient, metric: Metric, config: GisConfig, *, floor: int) -> MetricResult:
    query = metric.query(timeout_s=int(config.overpass.request_timeout_s // 2) or 25)
    result = client.fetch(query, label=f"metric:{metric.name}", min_expected_total=floor)
    payload = result.json()
    if not isinstance(payload, dict):
        raise GisEtlError(f"metric {metric.name} returned a non-object body")
    breakdown = count_breakdown(payload)
    expected = _optional_int(config.baseline.get(metric.baseline_key))
    measured = breakdown["total"]
    suspicious = bool(
        expected is not None
        and expected >= PLAUSIBILITY_FLOOR_BASELINE
        and measured == 0
        and result.source_mode == SOURCE_MODE_LIVE
    )
    return MetricResult(
        name=metric.name,
        description=metric.description,
        query_sha256=result.query_sha256,
        retrieved_at=result.record.retrieved_at,
        endpoint=result.record.endpoint,
        source_mode=result.source_mode,
        degraded=result.degraded,
        response_sha256=result.record.response_sha256,
        raw_path=_relative(result.record.path, config.root),
        replication_timestamp=result.record.replication_timestamp,
        nodes=breakdown["nodes"],
        ways=breakdown["ways"],
        relations=breakdown["relations"],
        total=measured,
        baseline_value=expected,
        delta=(measured - expected) if expected is not None else None,
        suspicious=suspicious,
        note=result.note,
    )


def _assemble(
    config: GisConfig,
    metrics: list[MetricResult],
    failures: list[dict[str, Any]],
    notes: list[str],
) -> CoverageReport:
    by_name = {metric.name: metric for metric in metrics}
    live = sum(1 for metric in metrics if metric.source_mode == SOURCE_MODE_LIVE)
    cached = len(metrics) - live
    buildings = by_name.get("building_ways")
    height = by_name.get("tagged_height")
    levels = by_name.get("tagged_building_levels")
    any_vertical = by_name.get("buildings_with_any_vertical_tag")

    coverage: dict[str, Any] = {
        "building_count_measured": buildings.total if buildings else None,
        "tagged_height": height.total if height else None,
        "tagged_building_levels": levels.total if levels else None,
        "any_vertical_tag": any_vertical.total if any_vertical else None,
        "building_height_coverage_string": _coverage_string(height, buildings),
        "building_levels_coverage_string": _coverage_string(levels, buildings),
        "any_vertical_coverage_string": _coverage_string(any_vertical, buildings),
        "vertical_coverage_ratio": _ratio(any_vertical, buildings),
        "power_lines": by_name.get("power_lines").total if by_name.get("power_lines") else None,
        "power_towers": by_name.get("power_towers").total if by_name.get("power_towers") else None,
        "power_poles": by_name.get("power_poles").total if by_name.get("power_poles") else None,
        "highway_ways": by_name.get("highway_ways").total if by_name.get("highway_ways") else None,
        "man_made_towers": by_name.get("man_made_towers").total if by_name.get("man_made_towers") else None,
        "masts": by_name.get("masts").total if by_name.get("masts") else None,
        "aeroway_ways": by_name.get("aeroway_ways").total if by_name.get("aeroway_ways") else None,
        "trees": by_name.get("trees").total if by_name.get("trees") else None,
        "central_buildings": by_name.get("central_buildings").total if by_name.get("central_buildings") else None,
        "central_with_height": by_name.get("central_with_height").total if by_name.get("central_with_height") else None,
    }

    claim_current = live > 0 and not failures and cached == 0
    verdict = _verdict(coverage)
    if failures:
        notes.append(
            f"{len(failures)} metric(s) could not be measured in this run; the "
            f"30 September baseline is quoted for those and nothing was invented."
        )
    if cached:
        notes.append(
            f"{cached} metric(s) came from the raw cache, not from a live retrieval. "
            f"They are real OSM measurements but their age is the cache entry's age."
        )
    earliest = min((metric.retrieved_at for metric in metrics), default="")
    return CoverageReport(
        measured_at=earliest or datetime.now().isoformat(timespec="seconds"),
        dataset_id=config.dataset_id,
        config_version=config.config_version,
        bbox_service=list(config.service_bbox.as_tuple()),
        bbox_central=list(config.central_bbox.as_tuple()),
        bbox_shape_km=[
            round(config.service_bbox.height_km(), 2),
            round(config.service_bbox.width_km(config.reference_latitude_deg), 2),
        ],
        baseline=dict(config.baseline),
        metrics=[metric.as_dict() for metric in metrics],
        failures=failures,
        live_metrics=live,
        cached_metrics=cached,
        coverage=coverage,
        verdict=verdict,
        claim_current=claim_current,
        notes=notes,
    )


def _coverage_string(numerator: MetricResult | None, denominator: MetricResult | None) -> str:
    if numerator is None or denominator is None or denominator.total == 0:
        return "not measured in this run"
    return f"{numerator.total:,} of {denominator.total:,} tagged"


def _ratio(numerator: MetricResult | None, denominator: MetricResult | None) -> float | None:
    if numerator is None or denominator is None or denominator.total == 0:
        return None
    return round(100.0 * numerator.total / denominator.total, 4)


def _verdict(coverage: dict[str, Any]) -> str:
    ratio = coverage.get("vertical_coverage_ratio")
    buildings = coverage.get("building_count_measured")
    highways = coverage.get("highway_ways")
    power = coverage.get("power_lines")
    parts = [
        f"OSM provides the horizontal network here ({highways} highway ways, "
        f"{buildings} building footprints, {power} power line ways).",
    ]
    if ratio is None:
        parts.append(
            "Vertical coverage could not be re-measured in this run; the 30 September "
            "2026 measurement (1 of 33,351 buildings tagged height) is quoted, not confirmed."
        )
    else:
        parts.append(
            f"Vertical coverage is {coverage.get('any_vertical_tag')} of "
            f"{buildings} buildings ({ratio}% carry any height evidence at all), so the "
            "vertical model in this snapshot is synthetic by necessity, not by preference."
        )
    parts.append(
        "Consequence: no route derived from this data may be described as having obstacle "
        "clearance. A 'safe route' cannot be produced from OSM alone."
    )
    return " ".join(parts)


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _sha(metric: Metric) -> str:
    from .hashing import query_sha256

    return query_sha256(metric.query())


def _relative(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root)).replace("\\", "/")
    except ValueError:
        return path


def load_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
