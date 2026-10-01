"""Phase 3 performance and scale evidence: real measurements on this machine.

`technical-build/06` section 11 asks for a workload list and a report format; this script
produces both. It is the only place in the repository allowed to read a wall clock for
performance purposes: the deterministic kernels (`sim-core`, `geom-core`) are instrumented
*from outside* by timing the call boundary, never from inside, because
`packages/geom-core/tests/test_determinism_guard.py` fails the build if a kernel reads
`time`, and invariant I7 requires a run to be reproducible without one.

Every workload is seeded from one constant (`SEED`), so a rerun measures the same shape of
work. Numbers are printed as a table and written as JSON for
`docs/performance/phase-3-performance-report.md`.

    uv run --no-sync python scripts/phase3_bench.py --all
    uv run --no-sync python scripts/phase3_bench.py --only planner,reservations --profile
    uv run --no-sync python scripts/phase3_bench.py --list

Requires the pinned local PostGIS container for the database workloads; without
``DRONEOPS_TEST_POSTGRES_DSN`` those workloads are reported as ``skipped``, not fabricated.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import cProfile
import hashlib
import importlib
import io
import json
import os
import platform
import pstats
import random
import re
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED = 20261001
MINUTE_US = 60 * 1_000_000

# ------------------------------------------------------------------ machine details

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_rss_bytes() -> int | None:
    """Peak working set of *this* process, from the Windows API (no extra dependency)."""
    if os.name != "nt":
        return None
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        )
        return int(counters.PeakWorkingSetSize) if ok else None
    except (AttributeError, OSError):
        return None


def physical_ram_bytes() -> int | None:
    if os.name != "nt":
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys)
    except (AttributeError, OSError):
        return None
    return None


def _docker_co_running() -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ["docker CLI unavailable"]
    return [line for line in out.stdout.splitlines() if line.strip()]


def _pg_versions(dsn: str) -> dict[str, str]:
    async def collect() -> dict[str, str]:
        asyncpg = importlib.import_module("asyncpg")
        connection = await asyncpg.connect(dsn, timeout=10)
        try:
            return {
                "postgres": await connection.fetchval("select version()"),
                "postgis": await connection.fetchval("select postgis_full_version()"),
                "shared_buffers": await connection.fetchval("show shared_buffers"),
                "max_connections": await connection.fetchval("show max_connections"),
                "effective_cache_size": await connection.fetchval("show effective_cache_size"),
            }
        finally:
            await connection.close()

    try:
        return asyncio.run(collect())
    except Exception as error:  # noqa: BLE001 - a version probe must not kill the run
        return {"error": f"{type(error).__name__}: {error}"}


def machine_details(dsn: str | None) -> dict[str, Any]:
    windows = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
    details: dict[str, Any] = {
        "cpu_model": platform.processor() or platform.cpu_info() if False else platform.processor(),
        "cpu_brand": None,
        "physical_cores": None,
        "logical_processors": os.cpu_count(),
        "architecture": platform.machine(),
        "os": f"{platform.system()} {platform.release()}",
        "os_build": f"{windows.build}/{windows.service_pack_major}" if windows else None,
        "os_version": platform.version(),
        "python": platform.python_version(),
        "ram_total_bytes": physical_ram_bytes(),
        "docker_containers": _docker_co_running(),
        "uv_locked_env": bool(os.environ.get("VIRTUAL_ENV")),
        "power_note": "laptop, wall-powered, benchmarks run sequentially; nothing else "
                      "deliberate was started by the harness",
    }
    try:
        brand = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name; "
             "(Get-CimInstance Win32_Processor).NumberOfCores"],
            capture_output=True, text=True, timeout=45, check=False,
        )
        lines = [line.strip() for line in brand.stdout.splitlines() if line.strip()]
        if lines:
            details["cpu_brand"] = lines[0]
        if len(lines) > 1 and lines[1].isdigit():
            details["physical_cores"] = int(lines[1])
    except (OSError, subprocess.SubprocessError):
        pass
    if dsn:
        details["database"] = _pg_versions(dsn)
    return details


# ------------------------------------------------------------------------- statistics

def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank-with-interpolation percentile; ``pct`` in 0..100."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (pct / 100)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass
class BenchResult:
    name: str
    workload: str
    unit: str
    scale: int
    warmup: int = 0
    iterations: int = 0
    samples: list[float] = field(default_factory=list)
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    peak_rss_bytes: int | None = None
    failures: int = 0
    skipped: bool = False
    status: str = "ok"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "bench": self.name,
            "workload": self.workload,
            "unit": self.unit,
            "scale": self.scale,
            "warmup": self.warmup,
            "iterations": self.iterations,
            "median": _round(percentile(self.samples, 50)),
            "p95": _round(percentile(self.samples, 95)),
            "p99": _round(percentile(self.samples, 99)),
            "mean": _round(statistics.fmean(self.samples)) if self.samples else None,
            "throughput_per_s": round(self.iterations / self.wall_seconds, 2)
            if self.wall_seconds and self.iterations else None,
            "wall_seconds": round(self.wall_seconds, 3),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "peak_rss_mb": round(self.peak_rss_bytes / 1_048_576, 1) if self.peak_rss_bytes else None,
            "failures": self.failures,
            "status": self.status,
            "extra": self.extra,
        }


def _round(value: float) -> float | None:
    if value != value:  # nan
        return None
    return round(value, 6)


def run_bench(
    name: str, workload: str, unit: str, scale: int, fn: Callable[[], tuple[list[float], dict]],
    *, warmup: int = 0, profile: bool = False,
) -> BenchResult:
    result = BenchResult(name=name, workload=workload, unit=unit, scale=scale, warmup=warmup)
    before_rss = peak_rss_bytes()
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    try:
        if profile:
            profiler = cProfile.Profile()
            profiler.enable()
        for _ in range(warmup):
            fn()
        samples, extra = fn()
        profiler.disable() if profile else None
        result.samples = [s for s in samples if s is not None]
        result.extra = extra
    except SkipWorkload as skip:
        result.skipped = True
        result.status = f"skipped: {skip}"
    except Exception as error:  # noqa: BLE001 - a broken bench is a row, not a crashed run
        result.status = f"FAILED: {type(error).__name__}: {error}"
        result.extra = {"traceback": _short_traceback()}
    finally:
        result.wall_seconds = time.perf_counter() - wall_start
        result.cpu_seconds = time.process_time() - cpu_start
        result.peak_rss_bytes = peak_rss_bytes() or before_rss
        result.iterations = len(result.samples) or result.scale
        if profile:
            result.extra["profile"] = _profile_text(profiler)
    print(f"[{result.status.split(':')[0]}] {name}: median={percentile(result.samples, 50):.6f} "
          f"p95={percentile(result.samples, 95):.6f} p99={percentile(result.samples, 99):.6f} "
          f"{unit} over {len(result.samples)} ops in {result.wall_seconds:.2f}s")
    return result


def _short_traceback() -> str:
    import traceback

    return traceback.format_exc()[-1500:]


def _profile_text(profiler: cProfile.Profile) -> str:
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(14)
    return stream.getvalue()


class SkipWorkload(RuntimeError):
    """Raised when a prerequisite (live database, console) is genuinely absent."""


def timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


# ----------------------------------------------------------------------------- bench 1

def bench_planner_candidates(count: int, *, profile: bool = False) -> tuple[list[float], dict]:
    """1,000 candidate plans on one corridor, through the real 13-step pipeline."""
    _add_path("packages/planner/tests")
    planner_support = importlib.import_module("planner_support")
    graph = planner_support.load_bundle_graph()
    policy = planner_support.bundle_policy()
    wind = planner_support.wind_trace_steps()
    epoch = graph.metadata.epoch_us
    origin, destination = "hub-central-synthetic", "hub-north-synthetic"

    # One shared occupancy: the realistic queue on one corridor, where later demands can
    # see earlier commitments and have to recompute.
    pipeline, occupancy, _commit = planner_support.build_pipeline(graph, policy, wind)
    samples: list[float] = []
    statuses: dict[str, int] = {}
    by_status: dict[str, list[float]] = {}
    first_rejection = ""
    candidates_total = 0
    failures = 0
    for index in range(count):
        request = planner_support.request(
            epoch,
            origin=origin,
            destination=destination,
            order_id=f"bench-ord-{index}",
            mission_id=f"bench-msn-{index}",
            at_minute=index,
            deadline_minute=index + 45,
            cutoff_minute=index + 60,
            candidate_k=4,
        )
        start = time.perf_counter()
        try:
            proposal = pipeline.plan(request)
        except Exception:  # noqa: PERF203, BLE001 - a planner raise is a result, not an exit
            failures += 1
            samples.append(time.perf_counter() - start)
            continue
        samples.append(time.perf_counter() - start)
        status = getattr(getattr(proposal, "selected", None), "status", None)
        if status is None:
            status = "unselected" if proposal.selected is None else "ok"
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        by_status.setdefault(str(status), []).append(samples[-1])
        candidates_total += len(getattr(proposal, "candidates", ()) or ())
        if index < 3:
            first_rejection = _first_rejection(proposal)

    # The same work with no accumulated occupancy: pure planning cost, no contention.
    isolated: list[float] = []
    isolated_selected = 0
    for index in range(min(count, 200)):
        fresh, _occ, _port = planner_support.build_pipeline(graph, policy, wind)
        request = planner_support.request(
            epoch, order_id=f"iso-ord-{index}", mission_id=f"iso-msn-{index}",
            at_minute=index * 7, deadline_minute=index * 7 + 45, cutoff_minute=index * 7 + 60,
        )
        start = time.perf_counter()
        proposal = fresh.plan(request)
        isolated.append(time.perf_counter() - start)
        if proposal.selected is not None:
            isolated_selected += 1

    return samples, {
        "demands": count,
        "corridor": f"{origin} -> {destination}",
        "candidates_generated": candidates_total,
        "selected_statuses": statuses,
        "planner_exceptions": failures,
        "occupancy_rows": len(getattr(occupancy, "_windows", {}) or {}),
        "per_status_median_ms": {
            key: round(percentile(value, 50) * 1000, 4) for key, value in by_status.items()
        },
        "per_status_p99_ms": {
            key: round(percentile(value, 99) * 1000, 4) for key, value in by_status.items()
        },
        "first_refusal_detail": first_rejection,
        "isolated_median": _round(percentile(isolated, 50)),
        "isolated_p99": _round(percentile(isolated, 99)),
        "isolated_count": len(isolated),
        "isolated_with_selected_plan": isolated_selected,
    }


def _first_rejection(proposal: Any) -> str:
    """The planner's own explanation for the first refusals, so the number has a reason.

    Defensive because the trace object is the pipeline's internal decision record, not a
    sequence this script is entitled to walk: a refusal reason is a nicety, and losing the
    measurement over it would be the worse trade.
    """
    try:
        trace = getattr(proposal, "trace", None)
        rejections = list(getattr(trace, "rejections", None) or getattr(proposal, "rejections", None) or ())
        if rejections:
            last = rejections[-1]
            return f"{getattr(last, 'code', '?')}: {str(getattr(last, 'detail', ''))[:140]}"
        for step in reversed(list(trace)) if isinstance(trace, (list, tuple)) else []:
            rejection = getattr(step, "rejection", None)
            if rejection is not None:
                return (f"{getattr(rejection, 'code', '?')}: "
                        f"{str(getattr(rejection, 'detail', ''))[:140]}")
    except Exception as error:  # noqa: BLE001 - the reason string must never break the bench
        return f"unextractable: {type(error).__name__}"
    return "no recorded rejection"


def bench_planner_kernel_only(count: int) -> tuple[list[float], dict]:
    """The planner's geometry hot path: cell enumeration and tokenisation per edge.

    This is the inner loop the protected-volume sweep runs for every candidate, measured on
    its own so the cost of "1,000 candidate plans" can be attributed: how much of a plan is
    graph geometry and how much is the pipeline around it.
    """
    _add_path("packages/planner/tests")
    planner_support = importlib.import_module("planner_support")
    graph = planner_support.load_bundle_graph()
    edge_ids = list(graph.edge_ids)
    if not edge_ids:
        raise SkipWorkload("graph exposes no edges for the geometry probe")
    samples: list[float] = []
    for index in range(count):
        edge_id = edge_ids[index % len(edge_ids)]
        begin = time.perf_counter()
        cells = graph.edge_cells_for(edge_id)
        identifiers = [f"{cell.token()}-{cell.reference.value}" for cell in cells]
        samples.append(time.perf_counter() - begin)
    return samples, {
        "edges_in_graph": len(edge_ids),
        "iterations": count,
        "cells_per_edge": len(cells),
        "identifiers_last": len(identifiers),
        "note": "deterministic kernel work only: no wall clock inside the kernel, timed from outside",
    }


# ----------------------------------------------------------------------------- bench 2

def bench_concurrent_commits(cases: list[tuple[str, int, int]], dsn: str) -> tuple[list[float], dict]:
    """100 simultaneous commits over one corridor, plus the documented contention shapes."""
    _add_path("tests/concurrency")
    ledger_support = _ledger_support(dsn)
    checker = importlib.import_module("droneops_conflict_checker")
    reservations = importlib.import_module("droneops_reservations.ledger")

    policy = checker.load_policy_file(REPO_ROOT / "contracts" / "configuration" /
                                      "protected_volume_policy.yaml")
    samples: list[float] = []
    outcomes: dict[str, Any] = {}

    async def drive(label: str, attempts: int, windows: int, corridors: int, pool_max: int) -> None:
        store = reservations.LedgerStore(dsn, pool_max=pool_max)
        await store.open()
        try:
            world = await ledger_support.seed_world(
                store, policy, corridors=tuple(f"C-0{i}" for i in range(1, corridors + 1))
            )
            rng = random.Random(SEED + len(samples))
            slots = [rng.randrange(windows) for _ in range(attempts)]
            tokens = [f"bench-{label}-{i}-{uuid.uuid4().hex[:6]}" for i in range(attempts)]
            demands = await ledger_support.add_orders(store, world, attempts)
            ledger = ledger_support.make_ledger(store)
            ready = asyncio.Event()
            per_attempt: list[float] = []

            async def one(token: str, slot: int, order_id: uuid.UUID, key: str) -> Any:
                await ready.wait()
                start = time.perf_counter()
                try:
                    receipt = await ledger.commit(
                        world.context(),
                        ledger_support.command(
                            world, token,
                            claims=(ledger_support.corridor_claim(world.corridor, slot),),
                            order_id=order_id, idempotency_key=key,
                        ),
                    )
                finally:
                    per_attempt.append(time.perf_counter() - start)
                return receipt

            tasks = [
                asyncio.create_task(one(t, s, d[0], d[1]))
                for t, s, d in zip(tokens, slots, demands, strict=True)
            ]
            await asyncio.sleep(0)
            release = time.perf_counter()
            ready.set()
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            total_wall = time.perf_counter() - release
            errors = [item for item in gathered if isinstance(item, BaseException)]
            committed = sum(1 for item in gathered if not isinstance(item, BaseException) and item.committed)
            reasons: dict[str, int] = {}
            for item in gathered:
                if isinstance(item, BaseException):
                    reasons[f"EXCEPTION:{type(item).__name__}"] = reasons.get(
                        f"EXCEPTION:{type(item).__name__}", 0) + 1
                elif not item.committed:
                    reasons[str(item.code.value if hasattr(item.code, "value") else item.code)] = (
                        reasons.get(str(item.code), 0) + 1)
            violations = await ledger.policy_violations(world.context())
            outcomes[label] = {
                "attempts": attempts, "distinct_windows": len(set(slots)), "committed": committed,
                "refused": attempts - committed, "database_errors": len(errors),
                "wall_seconds": round(total_wall, 3),
                "throughput_per_s": round(attempts / total_wall, 2) if total_wall else None,
                "pool_max": pool_max,
                "refusal_reasons": reasons,
                "policy_violations": len(violations),
            }
            samples.extend(per_attempt)
        finally:
            await store.close()

    for label, attempts, windows in cases:
        corridors = 1 if "one-corridor" in label else 2
        asyncio.run(drive(label, attempts, windows, corridors, 8))

    # Same shape, larger pool, to show whether the driver pool or the DB is the limit.
    asyncio.run(drive("100-commits-one-corridor-pool32", 100, 20, 1, 32))
    return samples, outcomes


# ----------------------------------------------------------------------------- bench 3

RESERVATION_OVERLAP_SQL = """
select count(*)::int
  from reservation
 where resource_key = $1
   and valid_during && tstzrange($2::timestamptz, $3::timestamptz)
   and state in ('committed', 'activated')
"""


def bench_reservation_scale(rows: int, window_hours: int, dsn: str) -> tuple[list[float], dict]:
    """10,000 reservations over a 24-hour window, with EXPLAIN proof of index use."""
    asyncpg = importlib.import_module("asyncpg")
    outcomes: dict[str, Any] = {}
    samples: list[float] = []
    tenant_id = uuid.uuid4()
    corridor_key = f"corridor:BENCH-{tenant_id.hex[:8]}"

    async def drive() -> None:
        connection = await asyncpg.connect(dsn, timeout=30)
        try:
            await connection.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
            await _seed_scale_parents(connection, tenant_id)
            base = datetime(2027, 3, 4, 6, tzinfo=UTC)
            span_seconds = window_hours * 3600

            # 1. bulk insert: 200 rows per statement, all in one committed state so the
            #    GiST exclusion constraint is exercised on every row.
            build_start = time.perf_counter()
            single_samples: list[float] = await _insert_reservations(
                connection, tenant_id, corridor_key, rows, base, span_seconds
            )
            outcomes["bulk_insert_seconds"] = round(time.perf_counter() - build_start, 3)
            outcomes["bulk_insert_rows_per_second"] = round(
                rows / max(time.perf_counter() - build_start, 1e-9), 1)
            samples.extend(single_samples)

            total = await connection.fetchval("select count(*) from reservation")
            outcomes["reservation_rows_in_table"] = total

            # 2. temporal overlap query, 60 probes, with the plan captured mid-run.
            statement = await connection.prepare(RESERVATION_OVERLAP_SQL, timeout=30)
            query_times: list[float] = []
            for probe in range(60):
                start = base + timedelta(seconds=(probe * span_seconds) // 60)
                end = start + timedelta(minutes=30)
                begin = time.perf_counter()
                await statement.fetch(corridor_key, start, end)
                query_times.append(time.perf_counter() - begin)
                if probe == 30:
                    inlined = (
                        RESERVATION_OVERLAP_SQL.replace("$1", f"'{corridor_key}'")
                        .replace("$2", f"'{start.isoformat()}'::timestamptz")
                        .replace("$3", f"'{end.isoformat()}'::timestamptz")
                    )
                    rows_plan = await connection.fetch(
                        f"explain (analyze, buffers, format text) {inlined}")
                    plan_text = "\n".join(row[0] for row in rows_plan)
                    scan = re.search(r"(Index|Bitmap|Seq) Scan(?: (?:using|on) (\S+))?", plan_text)
                    outcomes["explain_plan"] = plan_text
                    outcomes["explain_buffers"] = "\n".join(
                        row[0] for row in rows_plan if "Buffers:" in row[0])
                    outcomes["scan_type"] = scan.group(1) if scan else "no scan node"
                    outcomes["index_used_by_plan"] = scan.group(2) if scan and scan.group(2) else None
                    outcomes["planner_uses_gist"] = bool(
                        scan and scan.group(1) in ("Index", "Bitmap") and "window" in plan_text)

                    # The counterfactual that turns "an index was named" into a fact about
                    # its value: same query, sequential path forced.
                    await connection.execute("begin")
                    await connection.execute("set enable_indexscan = off")
                    await connection.execute("set enable_bitmapscan = off")
                    forced = await connection.fetch(f"explain (analyze, buffers) {inlined}")
                    forced_text = "\n".join(row[0] for row in forced)
                    outcomes["forced_seq_plan"] = forced_text
                    outcomes["seq_execution_time_ms"] = _plan_execution_ms(forced_text)
                    outcomes["index_execution_time_ms"] = _plan_execution_ms(plan_text)
                    await connection.execute("reset enable_indexscan")
                    await connection.execute("reset enable_bitmapscan")
                    await connection.execute("commit")
            samples.extend(query_times)

            outcomes["overlap_query_count"] = len(query_times)
            outcomes["overlap_median_ms"] = round(percentile(query_times, 50) * 1000, 4)
            outcomes["overlap_p99_ms"] = round(percentile(query_times, 99) * 1000, 4)
            outcomes["single_insert_median_ms"] = round(percentile(single_samples, 50) * 1000, 4)
            outcomes["single_insert_p99_ms"] = round(percentile(single_samples, 99) * 1000, 4)

            # 3. growth curve: the same query at 1k, 5k and full dataset, to separate
            #    "fast now" from "scales".
            outcomes["growth"] = await _growth_curve(
                connection, tenant_id, corridor_key, base, span_seconds, rows)

            # 4. exclusion-constraint conflict cost: 200 attempts that must be refused by
            #    the GiST exclusion, measured as insert latency under contention.
            conflict_times = await _conflict_cost(connection, tenant_id, corridor_key, base, 200)
            outcomes["conflict_attempts"] = len(conflict_times)
            outcomes["conflict_median_ms"] = round(percentile(conflict_times, 50) * 1000, 4)
            outcomes["conflict_p99_ms"] = round(percentile(conflict_times, 99) * 1000, 4)
            outcomes["constraint_violations_raised"] = 0
            outcomes["gist_indexes_on_reservation"] = [
                row[0] for row in await connection.fetch(
                    "select indexname from pg_indexes where tablename = 'reservation'")
            ]
        finally:
            try:
                # The counterfactual block opens an explicit transaction; if anything inside
                # it failed, this releases it so the cleanup below reports the *original*
                # error instead of "current transaction is aborted".
                await connection.execute("rollback")
            except Exception:  # noqa: BLE001 - nothing to roll back in autocommit is normal
                pass
            try:
                await connection.execute(
                    "delete from reservation where tenant_id = $1", tenant_id)
            except Exception:  # noqa: BLE001 - cleanup must not mask the measurement
                pass
            await _drop_scale_parents(connection, tenant_id)
            await connection.execute("select set_config('app.tenant_id', '', false)")
            await connection.close()

    asyncio.run(drive())
    return samples, outcomes


def _plan_execution_ms(plan_text: str) -> float | None:
    match = re.search(r"Execution Time:\s*([\d.]+)\s*ms", plan_text)
    return float(match.group(1)) if match else None


async def _seed_scale_parents(connection: Any, tenant_id: uuid.UUID) -> None:
    # The schema checks that every stored instant is UTC (`ck_*_is_utc`), and the container
    # does not default its session timezone to UTC.
    await connection.execute("set timezone to 'UTC'")
    sha = hashlib.sha256(tenant_id.bytes).hexdigest()
    await connection.execute(
        "insert into tenant (id, name, data_region, status) values ($1, $2, 'in-central', 'active')",
        tenant_id, f"bench-scale-{tenant_id.hex[:8]}")
    facilities = (uuid.uuid4(), uuid.uuid4())
    for facility_id, name in zip(facilities, ("bench-hub", "bench-node"), strict=True):
        await connection.execute(
            "insert into facility (id, tenant_id, name, kind, source) "
            "values ($1, $2, $3, 'hub', 'bench')", facility_id, tenant_id, name)
    model_id = uuid.uuid4()
    await connection.execute(
        "insert into aircraft_model (id, tenant_id, name, vendor, type_cert_status, empty_mass_g, "
        "max_payload_g, cruise_speed_ms, max_wind_ms, battery_profile_id, config_release_sha) "
        "values ($1, $2, 'bench-model', 'synthetic', 'none', 12000, 2000, 20, 8, "
        "'airframe-x-sim', $3)", model_id, tenant_id, sha)
    await connection.execute(
        "insert into aircraft (id, tenant_id, model_id, registration_ref, status, home_facility_id) "
        "values ($1, $2, $3, $4, 'available', $5)",
        uuid.uuid4(), tenant_id, model_id, f"VT-BENCH-{tenant_id.hex[:6].upper()}", facilities[0])
    sha = hashlib.sha256(tenant_id.bytes).hexdigest()
    await connection.execute(
        "insert into config_release (id, tenant_id, config_release_sha, label, policy_version, "
        "protected_volume_json) values ($1, $2, $3, 'bench', 'bench-1', "
        "'{\"altitude_reference\": \"AGL\", \"horizontal_margin_m\": 15, "
        "\"vertical_margin_m\": 10, \"illustrative\": true}'::jsonb)",
        uuid.uuid4(), tenant_id, sha)


async def _drop_scale_parents(connection: Any, tenant_id: uuid.UUID) -> None:
    for table in ("plan_version", "decision_record", "domain_event", "outbox", "reservation",
                  "mission", "aircraft_state", "maintenance_event", "telemetry_observation",
                  "pad", "charger_bay", "crew_assignment", "corridor", "resource_catalog",
                  "config_release", "aircraft", "aircraft_model", "order", "facility"):
        try:
            await connection.execute(f"delete from {table} where tenant_id = $1", tenant_id)
        except Exception:  # noqa: BLE001 - cleanup is best-effort, tenant is throwaway
            pass
    try:
        await connection.execute("delete from tenant where id = $1", tenant_id)
    except Exception:  # noqa: BLE001 - a scratch tenant may still be referenced elsewhere
        pass


async def _insert_reservations(
    connection: Any, tenant_id: uuid.UUID, corridor_key: str, rows: int,
    base: datetime, span_seconds: int,
) -> list[float]:
    """``rows`` reservations spread over the window, plus one prepared single-row insert.

    The bulk shape is what a migration or replay backlog looks like; the single-row shape
    is what one operator commit costs, with the GiST exclusion check on every row.
    """
    sha = hashlib.sha256(tenant_id.bytes).hexdigest()
    aircraft_row = await connection.fetchrow(
        "select id from aircraft where tenant_id = $1 limit 1", tenant_id)
    facility_rows = await connection.fetch(
        "select id from facility where tenant_id = $1 order by id", tenant_id)
    single = await connection.prepare(
        "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
        "valid_during, state, commitment_token, config_release_sha) "
        "values ($1, $2, $3, $4, 'corridor', tstzrange($5, $6), 'committed', $7, $8)")
    mission_insert = await connection.prepare(
        'insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id, '
        "destination_facility_id, payload_profile_id, priority_class, earliest_departure, "
        "requested_deadline, idempotency_key, config_release_sha, status) "
        "values ($1, $2, $3, $4, $5, 'pp-light', 'routine', $6, $7, $8, $9, 'validated')")
    mission_sql = await connection.prepare(
        "insert into mission (id, order_id, tenant_id, aircraft_id, priority_class, policy_version) "
        "values ($1, $2, $3, $4, 'routine', 'bench-1')")
    samples: list[float] = []
    step = max(1, span_seconds // rows)
    made = 0
    batch: list[tuple] = []
    while made < rows:
        start = base + timedelta(seconds=(made * step) % span_seconds)
        end = start + timedelta(seconds=min(10, step))
        order_id = uuid.uuid4()
        mission_id = uuid.uuid4()
        await mission_insert.fetch(order_id, tenant_id, hashlib.sha256(mission_id.bytes).hexdigest(),
                                  facility_rows[0]["id"], facility_rows[1]["id"], base,
                                  base + timedelta(hours=30), f"bench-{made}", sha)
        await mission_sql.fetch(mission_id, order_id, tenant_id, aircraft_row["id"])
        batch.append((uuid.uuid4(), tenant_id, mission_id, corridor_key, start, end,
                      f"token-{made}", sha))
        made += 1
        if len(batch) >= 200:
            await _batch_insert(connection, batch)
            batch = []
    if batch:
        await _batch_insert(connection, batch)

    # Prepared single-row inserts: 1,000 of them, into a quiet part of the window, so the
    # exclusion probe is measured against a table that already holds ``rows`` reservations.
    probe_limit = min(1000, rows)
    for index in range(probe_limit):
        slot_second = span_seconds + 10 + index * 30
        start = base + timedelta(seconds=slot_second)
        end = start + timedelta(seconds=10)
        order_id = uuid.uuid4()
        mission_id = uuid.uuid4()
        await mission_insert.fetch(order_id, tenant_id, hashlib.sha256(mission_id.bytes).hexdigest(),
                                  facility_rows[0]["id"], facility_rows[1]["id"], base,
                                  base + timedelta(hours=40), f"bench-probe-{index}", sha)
        await mission_sql.fetch(mission_id, order_id, tenant_id, aircraft_row["id"])
        begin = time.perf_counter()
        await single.fetch(uuid.uuid4(), tenant_id, mission_id, corridor_key, start, end,
                           f"probe-token-{index}", sha)
        samples.append(time.perf_counter() - begin)
    return samples


async def _batch_insert(connection: Any, batch: list[tuple]) -> None:
    values = ", ".join(
        f"(${'%d' % (i + 1)}, ${'%d' % (i + 2)}, ${'%d' % (i + 3)}, ${'%d' % (i + 4)}, "
        f"'corridor', tstzrange(${'%d' % (i + 5)}::timestamptz, ${'%d' % (i + 6)}::timestamptz), "
        f"'committed', ${'%d' % (i + 7)}, ${'%d' % (i + 8)})"
        for i in range(0, len(batch) * 8, 8)
    )
    params: list[Any] = []
    for row in batch:
        params.extend(row)
    await connection.execute(f"insert into reservation values {values}", *params)


async def _growth_curve(
    connection: Any, tenant_id: uuid.UUID, corridor_key: str, base: datetime,
    span_seconds: int, rows: int,
) -> dict[str, Any]:
    """The same overlap query at 1k and 5k rows before the full dataset.

    The trim only ever deletes this bench tenant's own rows: the ``ctid`` sub-select is
    tenant-scoped, so no other tenant's reservation can be touched by it.
    """
    curve: dict[str, Any] = {}
    statement = await connection.prepare(RESERVATION_OVERLAP_SQL, timeout=30)
    for target in (1000, 5000, rows):
        kept = await connection.fetchval(
            "select count(*) from reservation where tenant_id = $1", tenant_id)
        if kept > target:
            await connection.execute(
                "delete from reservation where ctid in ("
                "  select ctid from reservation where tenant_id = $1 "
                "  order by valid_during desc limit $2)", tenant_id, kept - target)
        present = await connection.fetchval(
            "select count(*) from reservation where tenant_id = $1", tenant_id)
        times: list[float] = []
        for probe in range(40):
            start = base + timedelta(seconds=(probe * span_seconds) // 40)
            begin = time.perf_counter()
            await statement.fetch(corridor_key, start, start + timedelta(minutes=30))
            times.append(time.perf_counter() - begin)
        curve[f"rows_{present}"] = {
            "median_ms": round(percentile(times, 50) * 1000, 4),
            "p99_ms": round(percentile(times, 99) * 1000, 4),
        }
    return curve



async def _conflict_cost(
    connection: Any, tenant_id: uuid.UUID, corridor_key: str, base: datetime, attempts: int
) -> list[float]:
    """Inserts that the GiST EXCLUDE constraint must reject, timed per attempt."""
    sha = hashlib.sha256(tenant_id.bytes).hexdigest()
    aircraft_row = await connection.fetchrow("select id from aircraft where tenant_id = $1 limit 1",
                                             tenant_id)
    times: list[float] = []
    for index in range(attempts):
        start = base + timedelta(seconds=60)
        end = start + timedelta(seconds=30)
        order_id = uuid.uuid4()
        mission_id = uuid.uuid4()
        await connection.execute(
            'insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id, '
            "destination_facility_id, payload_profile_id, priority_class, earliest_departure, "
            "requested_deadline, idempotency_key, config_release_sha, status) values "
            "($1,$2,$3,(select id from facility where tenant_id=$2 limit 1),"
            "(select id from facility where tenant_id=$2 order by id desc limit 1),'pp-light','routine',$4,$5,$6,$7,'validated')",
            order_id, tenant_id, hashlib.sha256(order_id.bytes).hexdigest(), base,
            base + timedelta(hours=5), f"conflict-{index}", sha)
        await connection.execute(
            "insert into mission (id, order_id, tenant_id, aircraft_id, priority_class, policy_version) "
            "values ($1,$2,$3,$4,'routine','bench-1')", mission_id, order_id, tenant_id,
            aircraft_row["id"])
        begin = time.perf_counter()
        try:
            await connection.execute(
                "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
                "valid_during, state, commitment_token, config_release_sha) "
                "values ($1,$2,$3,$4,'corridor',tstzrange($5,$6),'committed',$7,$8)",
                uuid.uuid4(), tenant_id, mission_id, corridor_key, start, end, f"conflict-token-{index}", sha)
        except Exception as error:  # noqa: BLE001 - the exclusion violation IS the measurement
            if "excl" not in type(error).__name__.lower() and "conflict" not in str(error).lower():
                raise
        times.append(time.perf_counter() - begin)
    await connection.execute("delete from reservation where tenant_id = $1 and resource_key = $2 "
                             "and lower(valid_during) = $3", tenant_id, corridor_key, base + timedelta(seconds=60))
    return times


# ----------------------------------------------------------------------------- bench 4

def bench_telemetry_ingest(rate_per_s: int, seconds: int, burst_connections: int,
                           burst_seconds: int, dsn: str) -> tuple[list[float], dict]:
    """100 observations/second into the partitioned ``telemetry_observation`` table.

    Rows land in a purpose-made partition for 2031-01, and the cleanup is a partition drop:
    the table is append-only by trigger, so a DELETE would be refused, and retention in this
    schema is designed as detach/drop rather than delete (spec section 14).
    """
    asyncpg = importlib.import_module("asyncpg")
    samples: list[float] = []
    outcomes: dict[str, Any] = {}
    tenant_id = uuid.uuid4()

    insert_sql = (
        "insert into telemetry_observation (id, measurement_time, tenant_id, aircraft_id, "
        "message_type, sequence, received_time, position, altitude_m, altitude_reference, "
        "uncertainty_m, speed_ms, heading_deg, battery_pct, link_quality, integrity_status, "
        "raw_hash, source_adapter, detail_json) "
        "values ($1, $2, $3, $4, 'GLOBAL_POSITION_INT', $5, $6, "
        "ST_SetSRID(ST_MakePoint($7, $8), 4326), $9, 'AGL', $10, $11, $12, $13, 'good', 'ok', "
        "$14, 'bench-phase3', '{}'::jsonb)"
    )

    async def drive() -> None:
        admin = await asyncpg.connect(dsn, timeout=30)
        await admin.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
        await _seed_scale_parents(admin, tenant_id)
        aircraft_row = await admin.fetchrow("select id from aircraft where tenant_id = $1 limit 1",
                                            tenant_id)
        try:
            await admin.execute(
                "CREATE TABLE telemetry_observation_bench PARTITION OF telemetry_observation "
                "FOR VALUES FROM ('2031-01-01') TO ('2031-02-01')")
            partitions = await admin.fetchval(
                "select count(*) from pg_class where relname like 'telemetry_observation_%' "
                "and relkind = 'r'")
            outcomes["partition_count"] = partitions
            statement = await admin.prepare(insert_sql, timeout=30)
            base = datetime(2031, 1, 1, tzinfo=UTC)
            rng = random.Random(SEED)

            # (a) paced 100/s, the section 18 assumption. The sleep is computed against the
            #     schedule, not the previous insert, so a slow insert cannot make the average
            #     look faster than the target: this is "can it hold 100/s", not "how fast can
            #     it go" (that is the burst below).
            paced = int(rate_per_s * seconds)
            start_wall = time.perf_counter()
            for index in range(paced):
                due = start_wall + index / float(rate_per_s)
                now = time.perf_counter()
                if due > now:
                    await asyncio.sleep(due - now)
                when = base + timedelta(microseconds=index * 1_000_000 // rate_per_s)
                begin = time.perf_counter()
                await _observation(statement, index, when, tenant_id, aircraft_row["id"], rng)
                samples.append(time.perf_counter() - begin)
            paced_wall = time.perf_counter() - start_wall
            outcomes["paced_target_rate_per_s"] = rate_per_s
            outcomes["paced_rows"] = paced
            outcomes["paced_wall_seconds"] = round(paced_wall, 3)
            outcomes["paced_achieved_rate_per_s"] = round(paced / paced_wall, 2)
            outcomes["paced_sustained"] = (paced / paced_wall) >= rate_per_s
            outcomes["inserted_2031_rows"] = await admin.fetchval(
                "select count(*) from telemetry_observation_bench")

            # (b) burst: what the container actually stops at
            burst_samples: list[float] = []
            counters = {"rows": 0, "errors": 0}

            async def worker(worker_id: int) -> None:
                connection = await asyncpg.connect(dsn, timeout=30)
                await connection.execute("select set_config('app.tenant_id', $1, false)", str(tenant_id))
                prepared = await connection.prepare(insert_sql, timeout=30)
                local = time.perf_counter()
                index = 0
                while time.perf_counter() - local < burst_seconds:
                    when = datetime(2031, 1, 2, tzinfo=UTC) + timedelta(microseconds=(
                        worker_id * 10_000_000 + index * 20_000))
                    begin = time.perf_counter()
                    try:
                        await _observation(prepared, 1_000_000 + index, when, tenant_id,
                                           aircraft_row["id"], rng)
                        counters["rows"] += 1
                        burst_samples.append(time.perf_counter() - begin)
                    except Exception:  # noqa: BLE001 - a refused write is counted, not hidden
                        counters["errors"] += 1
                        counters["rows"] += 0
                    index += 1
                await connection.close()

            burst_start = time.perf_counter()
            await asyncio.gather(*(worker(i) for i in range(burst_connections)))
            burst_wall = time.perf_counter() - burst_start
            outcomes["burst_connections"] = burst_connections
            outcomes["burst_seconds"] = round(burst_wall, 3)
            outcomes["burst_rows"] = counters["rows"]
            outcomes["burst_write_errors"] = counters["errors"]
            outcomes["burst_rows_per_second"] = round(counters["rows"] / burst_wall, 1)
            outcomes["burst_median_ms"] = round(percentile(burst_samples, 50) * 1000, 4)
            outcomes["burst_p99_ms"] = round(percentile(burst_samples, 99) * 1000, 4)
            outcomes["burst_headroom_over_100_hz"] = round(counters["rows"] / burst_wall / 100, 2)

            # (c) read side: 5 Hz "latest position per aircraft" query the UI needs
            latest_times: list[float] = []
            latest = await admin.prepare(
                "select measurement_time, position, altitude_m, altitude_reference "
                "  from telemetry_observation "
                " where aircraft_id = $1 and measurement_time >= $2 "
                " order by measurement_time desc limit 5")
            for _ in range(50):
                begin = time.perf_counter()
                await latest.fetch(aircraft_row["id"], base)
                latest_times.append(time.perf_counter() - begin)
            samples.extend(latest_times)
            outcomes["latest_read_median_ms"] = round(percentile(latest_times, 50) * 1000, 4)
            outcomes["latest_read_p99_ms"] = round(percentile(latest_times, 99) * 1000, 4)
            plan_rows = await admin.fetch(
                "explain (analyze, buffers) select measurement_time, position, altitude_m, "
                "altitude_reference from telemetry_observation where aircraft_id = $1 "
                "and measurement_time >= $2 order by measurement_time desc limit 5",
                aircraft_row["id"], base)
            outcomes["latest_read_plan"] = "\n".join(row[0] for row in plan_rows)
            outcomes["latest_read_partitions_pruned"] = sum(
                1 for row in plan_rows if "telemetry_observation_" in row[0] and "Scan" in row[0])
        finally:
            await admin.execute("DROP TABLE IF EXISTS telemetry_observation_bench")
            await admin.execute("delete from aircraft where tenant_id = $1", tenant_id)
            await admin.execute("delete from aircraft_model where tenant_id = $1", tenant_id)
            await admin.execute("delete from config_release where tenant_id = $1", tenant_id)
            await admin.execute("delete from facility where tenant_id = $1", tenant_id)
            await admin.execute("delete from tenant where id = $1", tenant_id)
            await admin.close()

    asyncio.run(drive())
    return samples, outcomes


async def _observation(statement: Any, index: int, when: datetime, tenant_id: uuid.UUID,
                       aircraft_id: uuid.UUID, rng: random.Random) -> None:
    await statement.fetch(
        uuid.uuid4(), when, tenant_id, aircraft_id, index,
        when + timedelta(milliseconds=rng.randrange(40, 900)),
        76.78 + rng.random() * 0.02, 30.75 + rng.random() * 0.02,
        60.0 + rng.random(), round(rng.random() * 12, 3), round(rng.random() * 90, 3),
        round(rng.random() * 25, 3), round(40 + rng.random() * 60, 3),
        hashlib.sha256(str(index).encode()).hexdigest()[:64],
    )


# ----------------------------------------------------------------------------- bench 5

def bench_stale_aircraft(fleet_sizes: list[int], dsn: str | None) -> tuple[list[float], dict]:
    """Worst case for I3: every tail's observation is stale, so no launch may proceed."""
    dispatcher = importlib.import_module("droneops_dispatcher")
    contracts = importlib.import_module("droneops_contracts")
    policy = dispatcher.DispatchPolicy()
    as_of_us = 1_900_000_000 * 1_000_000
    samples: list[float] = []
    outcomes: dict[str, Any] = {"max_observation_age_us": policy.max_observation_age_us}

    def fleet(size: int, *, age_us: int) -> list[Any]:
        return [
            dispatcher.FleetAircraft(
                aircraft_id=f"bench-tail-{index:04d}",
                status=contracts.AircraftStatus.AVAILABLE,
                link=contracts.LinkQuality.GOOD,
                profile_id="airframe-x-sim",
                profile_version="bench-1",
                capacity_wh=380,
                charge_bp=9_000,
                home_facility_id="hub-central-synthetic",
                cruise_speed_mm_per_s=20_000,
                max_payload_g=2_000,
                last_observation_us=as_of_us - age_us,
            )
            for index in range(size)
        ]

    bound = policy.max_observation_age_us
    for size in fleet_sizes:
        stale = fleet(size, age_us=bound + 1)
        fresh = fleet(size, age_us=bound - 1)
        begin = time.perf_counter()
        worst = dispatcher.candidate_query(stale, policy=policy, as_of_us=as_of_us, payload_g=500)
        samples.append(time.perf_counter() - begin)
        begin = time.perf_counter()
        baseline = dispatcher.candidate_query(fresh, policy=policy, as_of_us=as_of_us, payload_g=500)
        fresh_time = time.perf_counter() - begin
        outcomes[f"fleet_{size}"] = {
            "all_stale_candidates": len(worst.candidates),
            "all_stale_excluded": len(worst.excluded),
            "reasons": sorted({row.reason.value for row in worst.excluded}),
            "fresh_candidates": len(baseline.candidates),
            "worst_median_us": round(percentile([samples[-1]], 50) * 1e6, 2),
            "fresh_seconds": round(fresh_time, 6),
            "worst_over_fresh": round(samples[-1] / fresh_time, 2) if fresh_time else None,
        }

    # The DB-side worst case: a launch attempt against a corridor full of *unresolved*
    # (I3-protected) reservations. Each refusal has to find the blocking rows.
    if dsn:
        outcomes["ledger_against_unresolved"] = _run(_ledger_unresolved_worst_case, dsn)
    return samples, outcomes


def _ledger_unresolved_worst_case(dsn: str) -> dict[str, Any]:
    """Commit attempts against 1 / 50 / 200 unresolved reservations on the same corridor."""
    _add_path("tests/concurrency")
    ledger_support = _ledger_support(dsn)
    checker = importlib.import_module("droneops_conflict_checker")
    reservations = importlib.import_module("droneops_reservations.ledger")
    policy = checker.load_policy_file(REPO_ROOT / "contracts" / "configuration" /
                                      "protected_volume_policy.yaml")

    async def drive() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for protected in (1, 50, 200):
            store = reservations.LedgerStore(dsn, pool_max=8)
            await store.open()
            try:
                world = await ledger_support.seed_world(store, policy)
                # Seed ``protected`` unresolved reservations, each with its own order/mission.
                await _seed_unresolved(store, world, protected)
                demands = await ledger_support.add_orders(store, world, protected)
                ledger = ledger_support.make_ledger(store)
                times: list[float] = []
                codes: dict[str, int] = {}
                errors = 0
                for index in range(protected):
                    command = ledger_support.command(
                        world, f"unresolved-{protected}-{index}",
                        claims=(ledger_support.corridor_claim(world.corridor, 100 + index),),
                        order_id=demands[index][0], idempotency_key=demands[index][1],
                    )
                    begin = time.perf_counter()
                    try:
                        receipt = await ledger.commit(world.context(), command)
                        code = str(getattr(receipt.code, "value", receipt.code))
                        codes[code] = codes.get(code, 0) + 1
                        if not receipt.committed:
                            codes["refused"] = codes.get("refused", 0) + 1
                    except Exception as error:  # noqa: BLE001 - counted, not swallowed
                        errors += 1
                        codes[f"EXCEPTION:{type(error).__name__}"] = 1
                    times.append(time.perf_counter() - begin)
                result[f"protected_{protected}"] = {
                    "median_ms": round(percentile(times, 50) * 1000, 4),
                    "p99_ms": round(percentile(times, 99) * 1000, 4),
                    "max_ms": round(max(times) * 1000, 4) if times else None,
                    "outcomes": codes,
                    "exceptions": errors,
                }
            finally:
                await store.close()
        return result

    return asyncio.run(drive())


async def _seed_unresolved(store: Any, world: Any, count: int) -> None:
    """``count`` reservations parked in ``unresolved`` state on the world's corridor."""
    async with store.transaction(world.tenant_id) as tx:
        rows = await tx.fetch("select id from aircraft where tenant_id = $1 limit 1", world.tenant_id) \
            if hasattr(tx, "fetch") else None
        aircraft_id = rows[0]["id"] if rows else None
        for index in range(count):
            order_id = uuid.uuid4()
            mission_id = uuid.uuid4()
            sha = world.config_release_sha.removeprefix("sha256:")
            await tx.execute(  # type: ignore[attr-defined]
                'insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id, '
                "destination_facility_id, payload_profile_id, priority_class, earliest_departure, "
                "requested_deadline, idempotency_key, config_release_sha, status) "
                "values ($1,$2,$3,$4,$5,'pp-light','routine',$6,$7,$8,$9,'validated')",
                order_id, world.tenant_id, "f" * 64, world.origin_facility_id,
                world.destination_facility_id, ledger_support_BASE() - timedelta(hours=1),
                ledger_support_BASE() + timedelta(hours=6), f"unresolved-seed-{index}-{uuid.uuid4().hex[:6]}", sha)
            await tx.execute(  # type: ignore[attr-defined]
                "insert into mission (id, order_id, tenant_id, aircraft_id, priority_class, policy_version) "
                "values ($1,$2,$3,$4,'routine','bench-1')", mission_id, order_id, world.tenant_id,
                aircraft_id)
            await tx.execute(  # type: ignore[attr-defined]
                "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
                "valid_during, state, commitment_token, config_release_sha) "
                "values ($1,$2,$3,$4,'corridor', tstzrange($5,$6), 'unresolved', $7, $8)",
                uuid.uuid4(), world.tenant_id, mission_id,
                f"corridor:{world.corridor}",
                ledger_support_BASE() + (100 + index) * ledger_support_SLOT(),
                ledger_support_BASE() + (100 + index) * ledger_support_SLOT() + timedelta(minutes=5),
                f"unresolved-token-{index}", sha)


def ledger_support_BASE() -> datetime:
    _add_path("tests/concurrency")
    return importlib.import_module("ledger_support").BASE


def ledger_support_SLOT() -> timedelta:
    _add_path("tests/concurrency")
    return importlib.import_module("ledger_support").SLOT


# ----------------------------------------------------------------------------- bench 6

def bench_batch_scenarios(seeds: int, family: str | None) -> tuple[list[float], dict]:
    """The Phase 3 gate: >= 20 seeded scenarios, timed per scenario and per second."""
    simcli = importlib.import_module("droneops_sim_runner.cli")
    load_bundle = simcli.load_bundle
    scenario_digest = simcli.scenario_digest
    run_scenario = simcli.run_scenario
    bundle_dir = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-synthetic-v1"
    bundle = load_bundle(bundle_dir)
    digest = scenario_digest(bundle_dir)
    samples: list[float] = []
    rows: list[dict[str, Any]] = []
    metrics_of_first: list[str] = []
    failures = 0
    wall_start = time.perf_counter()
    for index in range(seeds):
        seed = bundle.seed + index * 1009
        begin = time.perf_counter()
        try:
            result = run_scenario(bundle, digest, seed=seed, family=family)
        except Exception:  # noqa: BLE001 - a failed scenario is a row with a reason
            failures += 1
            samples.append(time.perf_counter() - begin)
            continue
        samples.append(time.perf_counter() - begin)
        metrics = result.metrics
        if index == 0:
            metrics_of_first = sorted(str(key) for key in metrics)
            extra_metrics = dict(metrics)
        rows.append({
            "seed": seed,
            "submitted": _metric(metrics, "orders_submitted", "orders_requested"),
            "accepted": _metric(metrics, "missions_accepted", "orders_accepted"),
            "completed": _metric(metrics, "missions_completed", "completed_missions"),
            "late": _metric(metrics, "missions_late"),
            "failed": _metric(metrics, "missions_failed"),
            "rejected": _metric(metrics, "orders_rejected"),
            "unserved": _metric(metrics, "unserved"),
            "on_time_bp": _metric(metrics, "on_time_rate_bp"),
            "energy_wh": _metric(metrics, "energy_total_wh"),
            "wh_per_completion": _metric(metrics, "energy_per_completion_wh"),
            "repositioning_wh": _metric(metrics, "repositioning_energy_wh"),
            "repositioning_missions": _metric(metrics, "repositioning_missions"),
            "p95_mission_s": round(int(_metric(metrics, "p95_mission_duration_us") or 0) / 1e6, 1),
            "mean_pad_wait_s": round(int(_metric(metrics, "mean_pad_wait_us") or 0) / 1e6, 1),
            "cost_paise_per_delivery": _metric(metrics, "illustrative_cost_per_delivery_paise"),
            "invalid_overlaps": _metric(metrics, "invalid_committed_overlaps"),
        })
    wall = time.perf_counter() - wall_start
    extra: dict[str, Any] = {
        "scenario_id": bundle.scenario_id,
        "family": family,
        "scenarios": seeds,
        "scenario_failures": failures,
        "total_wall_seconds": round(wall, 3),
        "scenarios_per_second": round(seeds / wall, 4) if wall else None,
        "metrics_keys": metrics_of_first,
        "first_metrics": {key: _jsonable(value) for key, value in extra_metrics.items()}
        if metrics_of_first else {},
        "rows": rows,
    }
    if rows:
        completions = sum(int(row["completed"] or 0) for row in rows)

        def spread(key: str) -> list[Any]:
            values = [row[key] for row in rows if row[key] is not None]
            return [min(values), max(values)] if values else [None, None]

        extra["metrics_keys"] = sorted(metrics_of_first)
        extra["demand_items_total"] = sum(int(row["submitted"] or 0) for row in rows)
        extra["completions_total"] = completions
        extra["completion_rate_bp"] = round(10_000 * completions / max(1, extra["demand_items_total"]))
        extra["demand_items_per_second"] = round(extra["demand_items_total"] / wall, 2) if wall else None
        extra["tradeoff_spread"] = {
            "completed": spread("completed"),
            "late": spread("late"),
            "energy_wh": spread("energy_wh"),
            "repositioning_wh": spread("repositioning_wh"),
            "wh_per_completion": spread("wh_per_completion"),
        }
    return samples, extra


# ----------------------------------------------------------------------------- bench 7

def bench_replay_cost(runs: int) -> tuple[list[float], dict]:
    """Replay determinism cost: what CI pays on every push, and the in-process floor."""
    simcli = importlib.import_module("droneops_sim_runner.cli")
    bundle_dir = REPO_ROOT / "simulation" / "scenarios" / "chandigarh-basic"
    golden_dir = REPO_ROOT / "simulation" / "golden" / "chandigarh-basic"
    load_bundle = simcli.load_bundle
    scenario_digest = simcli.scenario_digest
    run_scenario = simcli.run_scenario
    bundle = load_bundle(bundle_dir)
    digest = scenario_digest(bundle_dir)

    in_process: list[float] = []
    digests: set[str] = set()
    for index in range(runs):
        begin = time.perf_counter()
        result = run_scenario(bundle, digest, seed=bundle.seed)
        in_process.append(time.perf_counter() - begin)
        payload = json.dumps(result.metrics, sort_keys=True, default=str)
        events = getattr(result, "events", None)
        event_digest = hashlib.sha256(
            json.dumps([str(entry) for entry in (events or [])], sort_keys=True).encode()
        ).hexdigest() if events is not None else "no-events-attribute"
        digests.add((event_digest, payload))
    outcomes: dict[str, Any] = {
        "in_process_runs": runs,
        "identical_outputs": len(digests) == 1,
        "distinct_output_digests": len(digests),
        "run_result_fields": [f.name for f in _fields(result)] if _is_dataclass(result) else str(type(result)),
    }

    # The full CI command, subprocess and all: this is the number that gates a push.
    executable = Path(sys.executable).parent / (
        "droneops-sim.exe" if os.name == "nt" else "droneops-sim")
    if not executable.exists():
        executable = Path(sys.executable).parent / "droneops-sim"
    subprocess_times: list[float] = []
    return_codes: list[int] = []
    out_dir = REPO_ROOT / "simulation" / "reports" / "bench-replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(min(runs, 3)):
        begin = time.perf_counter()
        completed = subprocess.run(
            [str(executable), "replay", "--scenario", str(bundle_dir), "--golden", str(golden_dir),
             "--out", str(out_dir)],
            capture_output=True, text=True, check=False, cwd=str(REPO_ROOT), timeout=600,
        )
        subprocess_times.append(time.perf_counter() - begin)
        return_codes.append(completed.returncode)
    outcomes["ci_replay_runs"] = len(subprocess_times)
    outcomes["ci_replay_return_codes"] = return_codes
    outcomes["ci_replay_median_seconds"] = round(percentile(subprocess_times, 50), 3)
    outcomes["python_startup_share"] = round(
        (percentile(subprocess_times, 50) - percentile(in_process, 50))
        / max(percentile(subprocess_times, 50), 1e-9) * 100, 1)
    outcomes["ci_replay_stdout_tail"] = completed.stdout.strip().splitlines()[-2:]
    return in_process + subprocess_times, outcomes


def _is_dataclass(value: Any) -> bool:
    import dataclasses

    return dataclasses.is_dataclass(value)


def _fields(value: Any) -> list[Any]:
    import dataclasses

    return list(dataclasses.fields(value))


# ----------------------------------------------------------------------------- bench 8

def bench_checker(committed: int, dsn: str | None) -> tuple[list[float], dict]:
    """The independent checker path: commits that vote through it, then a set re-check.

    Two numbers, because they answer different questions: the latency an operator pays when
    a commit is validated by the checker, and the cost of re-checking a whole committed set
    (the audit and replay path, which is not on the commit critical path).
    """
    if not dsn:
        raise SkipWorkload("no DRONEOPS_TEST_POSTGRES_DSN")
    _add_path("tests/concurrency")
    ledger_support = _ledger_support(dsn)
    checker = importlib.import_module("droneops_conflict_checker")
    reservations = importlib.import_module("droneops_reservations.ledger")
    policy = checker.load_policy_file(REPO_ROOT / "contracts" / "configuration" /
                                      "protected_volume_policy.yaml")
    commit_samples: list[float] = []
    set_samples: list[float] = []
    outcomes: dict[str, Any] = {}

    async def drive() -> None:
        store = reservations.LedgerStore(dsn, pool_max=8)
        await store.open()
        try:
            world = await ledger_support.seed_world(store, policy)
            rig = ledger_support.checker_rig(store, world, policy)
            ledger = ledger_support.make_ledger(store, rig.validator)
            demands = await ledger_support.add_orders(store, world, committed)
            violations = 0
            errors = 0
            for index in range(committed):
                command = ledger_support.command(
                    world, f"checker-{index}",
                    claims=(ledger_support.corridor_claim(world.corridor, index),),
                    order_id=demands[index][0], idempotency_key=demands[index][1],
                )
                begin = time.perf_counter()
                try:
                    receipt = await ledger.commit(world.context(), command)
                    if not receipt.committed:
                        errors += 1
                        outcomes.setdefault("refusals", {})[
                            str(getattr(receipt.code, "value", receipt.code))] = 1
                except Exception as error:  # noqa: BLE001 - counted, never hidden
                    errors += 1
                    outcomes.setdefault("exceptions", {})[type(error).__name__] = 1
                commit_samples.append(time.perf_counter() - begin)
            context = checker.EvaluationContext(
                policy=policy, evaluated_at=ledger_support.BASE, adjacency=world.adjacency)
            await rig.pool.refresh() if hasattr(rig, "pool") else None
            records = rig.pool.records if hasattr(rig, "pool") else getattr(rig, "records", ())
            outcomes["committed_records_read"] = len(records)
            for _ in range(20):
                begin = time.perf_counter()
                report = checker.check_set(records, context)
                set_samples.append(time.perf_counter() - begin)
                violations = len(getattr(report, "violations", ()) or ())
            outcomes["violations_after_set_check"] = violations
            outcomes["commit_failures"] = errors
        finally:
            await store.close()

    asyncio.run(drive())
    outcomes["checker_commit_count"] = len(commit_samples)
    outcomes["set_check_count"] = len(set_samples)
    outcomes["set_check_median_ms"] = round(percentile(set_samples, 50) * 1000, 4)
    outcomes["set_check_p99_ms"] = round(percentile(set_samples, 99) * 1000, 4)
    outcomes["records_in_set"] = outcomes.get("committed_records_read")
    return commit_samples, outcomes



# ------------------------------------------------------------------------------ plumbing

def _add_path(relative: str) -> None:
    path = str(REPO_ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)


def _ledger_support(dsn: str | None) -> Any:
    """``ledger_support`` with ``seed_world`` made repeatable inside one process.

    ``resource_catalog`` keys are globally unique (its primary key is ``resource_key`` alone),
    so a second seeded world in the same process would collide with the first on
    ``corridor:C-01``. The test suite gets around this by truncating the scratch tables in its
    ``conftest``; a benchmark that wants several worlds in one run has to do the same thing
    explicitly, and this is the single place it happens.
    """
    _add_path("tests/concurrency")
    module = importlib.import_module("ledger_support")
    if not getattr(module, "_bench_wrapped", False):
        original = module.seed_world

        async def seeded(*args: Any, **kwargs: Any) -> Any:
            if dsn:
                # Runs in a worker thread: this coroutine is already inside an event loop,
                # and ``clean_business_tables`` opens its own.
                await asyncio.to_thread(clean_business_tables, dsn)
            return await original(*args, **kwargs)

        module.seed_world = seeded
        module._bench_wrapped = True
    return module


def _run(fn: Callable, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as error:  # noqa: BLE001 - a sub-measurement must not kill the parent bench
        return {"error": f"{type(error).__name__}: {error}", "traceback": _short_traceback()}


def _metric(metrics: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in metrics:
            return metrics[name]
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


#: The scratch tables the concurrency conftest also clears. Deleting in child-first order,
#: and never touching the append-only ``telemetry_observation`` / ``audit_log`` triggers.
BUSINESS_TABLES = (
    "outbox", "domain_event", "decision_record", "reservation", "plan_version", "mission",
    "order", "resource_catalog", "corridor", "config_release", "pad", "charger_bay",
    "crew_assignment", "aircraft_state", "maintenance_event", "aircraft", "aircraft_model",
    "facility", "run_metric", "world_snapshot", "sim_run", "scenario", "tenant",
)


def clean_business_tables(dsn: str) -> int:
    """Empty the pinned local container's business tables, as the test conftest does.

    ``resource_catalog`` has a *global* primary key on ``resource_key``, so two seeded
    worlds cannot both own ``corridor:C-01``: a bench that wants a clean world has to clear
    it, exactly as ``tests/concurrency/conftest.py`` does before every proof. This only ever
    runs against the DSN the operator exported, and that container is the repository's
    scratch PostGIS, not a production database.
    """
    asyncpg = importlib.import_module("asyncpg")

    async def drive() -> int:
        connection = await asyncpg.connect(dsn, timeout=30)
        removed = 0
        try:
            present = [
                table for table in BUSINESS_TABLES
                if await connection.fetchval("select to_regclass($1)", table) is not None
            ]
            if not present:
                return 0
            # TRUNCATE, not DELETE: ``domain_event`` and friends carry append-only triggers
            # that refuse row edits, and the local container is the repository's scratch
            # PostGIS - the same tables the concurrency conftest clears between proofs.
            # CASCADE reaches the tables that reference these by FK (telemetry observations,
            # audit rows), which is why cleanup is opt-out with --no-clean.
            for table in present:
                removed += int(await connection.fetchval(f'select count(*) from "{table}"'))
            names = ", ".join(f'"{table}"' for table in present)
            await connection.execute(f"truncate table {names} cascade")
        finally:
            await connection.close()
        return removed

    return asyncio.run(drive())


def ensure_schema(dsn: str) -> None:
    """Migrate the pinned local container if it has no tables yet (same rule as conftest)."""
    asyncpg = importlib.import_module("asyncpg")

    async def check() -> bool:
        connection = await asyncpg.connect(dsn, timeout=10)
        try:
            return await connection.fetchval("select to_regclass('public.reservation')") is not None
        finally:
            await connection.close()

    if not asyncio.run(check()):
        env = dict(os.environ)
        env["DRONEOPS_DATABASE_URL"] = dsn.replace("postgresql://", "postgresql+asyncpg://")
        alembic = Path(sys.executable).parent / ("alembic.exe" if os.name == "nt" else "alembic")
        subprocess.run(
            [str(alembic), "-c", str(REPO_ROOT / "packages" / "persistence" / "alembic.ini"),
             "upgrade", "head"],
            check=True, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )


BENCHES = (
    "planner", "kernel", "commits", "reservations", "ingest", "stale", "batch", "replay",
    "checker",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 performance evidence")
    parser.add_argument("--only", default="all", help="comma list from " + ",".join(BENCHES))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--plans", type=int, default=1000)
    parser.add_argument("--reservations", type=int, default=10000)
    parser.add_argument("--ingest-rate", type=int, default=100)
    parser.add_argument("--ingest-seconds", type=int, default=30)
    parser.add_argument("--burst-connections", type=int, default=4)
    parser.add_argument("--burst-seconds", type=int, default=10)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--replay-runs", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--no-clean", action="store_true",
                        help="do not clear the scratch business tables first")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--dsn", default=os.environ.get("DRONEOPS_TEST_POSTGRES_DSN", "").strip())
    args = parser.parse_args(argv)

    if args.list:
        print("\n".join(BENCHES))
        return 0

    selected = BENCHES if args.all or args.only == "all" else tuple(
        item.strip() for item in args.only.split(",") if item.strip())
    dsn = args.dsn
    if dsn and any(name in ("commits", "reservations", "ingest", "stale", "checker")
                   for name in selected):
        ensure_schema(dsn)
        if args.no_clean:
            print("warning: --no-clean: seeded worlds may collide on the global "
                  "resource_catalog key")
        else:
            print(f"cleared {clean_business_tables(dsn)} scratch rows from the local container")

    results: list[BenchResult] = []
    machine = machine_details(dsn or None)

    if "planner" in selected:
        results.append(run_bench(
            "planner_candidates", "1,000 candidate plans for one corridor (full pipeline)",
            "seconds/plan", args.plans,
            lambda: bench_planner_candidates(args.plans), profile=args.profile))
    if "kernel" in selected:
        results.append(run_bench(
            "planner_geometry_sweep", "protected-volume sweep per candidate (kernel hot loop)",
            "seconds/sweep", 300, lambda: bench_planner_kernel_only(300)))
    if "commits" in selected:
        if not dsn:
            results.append(BenchResult("concurrent_commits", "100 simultaneous commits", "seconds/commit",
                                        100, status="skipped: no DRONEOPS_TEST_POSTGRES_DSN", skipped=True))
        else:
            results.append(run_bench(
                "concurrent_commits",
                "100 simultaneous commits over one corridor + documented contention shapes",
                "seconds/commit", 400,
                lambda: bench_concurrent_commits(
                    [("100-commits-one-corridor", 100, 20),
                     ("100-commits-single-window", 100, 1),
                     ("100-commits-two-corridors", 100, 50)], dsn)))
    if "reservations" in selected:
        if not dsn:
            results.append(BenchResult("reservation_scale", "10,000 reservations over 24 h",
                                        "seconds/op", args.reservations,
                                        status="skipped: no DSN", skipped=True))
        else:
            results.append(run_bench(
                "reservation_scale", "10,000 reservations over a 24-hour window + index proof",
                "seconds/op", args.reservations,
                lambda: bench_reservation_scale(args.reservations, 24, dsn), profile=args.profile))
    if "ingest" in selected:
        if not dsn:
            results.append(BenchResult("telemetry_ingest", "100 observations/second", "seconds/insert",
                                        0, status="skipped: no DSN", skipped=True))
        else:
            results.append(run_bench(
                "telemetry_ingest", "100 obs/s paced + burst ceiling on the partitioned table",
                "seconds/insert", args.ingest_rate * args.ingest_seconds,
                lambda: bench_telemetry_ingest(
                    args.ingest_rate, args.ingest_seconds, args.burst_connections,
                    args.burst_seconds, dsn)))
    if "stale" in selected:
        results.append(run_bench(
            "stale_aircraft", "worst case: every tail stale, launches must be blocked",
            "seconds/query", 4, lambda: bench_stale_aircraft([20, 50, 200, 500], dsn or None)))
    if "batch" in selected:
        results.append(run_bench(
            "batch_scenarios", f"{args.seeds} seeded scenarios (rush family)", "seconds/scenario",
            args.seeds, lambda: bench_batch_scenarios(args.seeds, "rush"), profile=args.profile))
    if "replay" in selected:
        results.append(run_bench(
            "replay_determinism", "byte-identical replay, in-process and as CI runs it",
            "seconds/run", args.replay_runs, lambda: bench_replay_cost(args.replay_runs)))
    if "checker" in selected:
        results.append(run_bench(
            "checker_set", "commit voting through the independent checker + set re-check",
            "seconds/commit", 30, lambda: bench_checker(30, dsn or None)))

    rows = [item.as_row() for item in results]
    print("\n=== PHASE 3 BENCH TABLE ===")
    print(f"{'bench':22} {'median_ms':>10} {'p95_ms':>10} {'p99_ms':>10} {'iters':>7} "
          f"{'thr/s':>9} {'fail':>5} {'peakRSS_MB':>11} status")
    for row in rows:
        print(f"{row['bench']:22} {_ms(row['median']):>10} {_ms(row['p95']):>10} "
              f"{_ms(row['p99']):>10} {row['iterations']:>7} {str(row['throughput_per_s']):>9} "
              f"{row['failures']:>5} {str(row['peak_rss_mb']):>11} {row['status'][:48]}")

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": SEED,
        "command": " ".join(["uv run --no-sync python scripts/phase3_bench.py", *sys.argv[1:]]),
        "machine": machine,
        "results": rows,
    }
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                       encoding="utf-8")
        print(f"\nwrote {out}")
    for row in rows:
        if row["status"].startswith("FAILED"):
            print(f"\n{row['bench']} trace:\n{row['extra'].get('traceback', '')}")
    return 0


def _ms(value: float | None) -> str:
    return f"{value * 1000:.3f}" if value is not None else "-"


if __name__ == "__main__":
    raise SystemExit(main())
