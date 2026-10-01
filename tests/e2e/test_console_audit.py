"""Independent Phase 4 console audit, driven in a real browser.

Every test re-verifies a builder claim or a Phase 4 exit criterion from the outside:
route render matrix, safety banner, panel roots, console hygiene, keyboard-path counts,
reduced-motion computed styles, legend second channels, axe-core, honesty of numbers,
and the structural no-command / no-scheduling boundaries (checked against the BUILT bundle).

Two surfaces are used on purpose:
  * ``DRONEOPS_CONSOLE_URL`` (default :4173) - the built preview. It has NO fixture
    transport at all. ``?transport=fixtures`` is accepted only as a dev-only alias of
    ``?transport=synthetic`` (``src/main.ts`` guards both behind ``!import.meta.env.PROD``),
    so on the built artifact the keyword stays inert - which is what this suite pins.
  * ``DRONEOPS_CONSOLE_DEV_URL`` (default :5173) - ``npm run dev``, where synthetic
    fixtures make the rejection/drawer/replay path reachable.

Skipped when the needed server is not answering. Artifacts: apps/console/tests/artifacts/.

Simulation only. Not authorised for flight.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

CONSOLE_URL = os.environ.get("DRONEOPS_CONSOLE_URL", "http://127.0.0.1:4173")
DEV_URL = os.environ.get("DRONEOPS_CONSOLE_DEV_URL", "http://127.0.0.1:5173")
API_HEALTH_URL = os.environ.get("DRONEOPS_API_HEALTH_URL", "http://127.0.0.1:8000/health")
BANNER = "Simulation only. Not authorised for flight."

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / "apps" / "console" / "tests" / "artifacts"
VENDOR_AXE = REPO / "apps" / "console" / "tests" / "vendor" / "axe.min.js"
DIST = REPO / "apps" / "console" / "dist" / "assets"

ROUTE_ROOTS = {
    "map": ["exceptions"],
    "missions": ["mission-queue", "mission-board"],
    "fleet": ["fleet"],
    "economics": ["economics"],
    "decision": ["decision-trace"],
    "replay": ["replay"],
    "comparison": ["comparison"],
}

# The audit's axe defects are repaired, so the allowlist that pinned them is empty:
#   * dlitem            - `field()` in panels/support/kit.ts is now a dl of its own, so a
#     label/value pair can never be emitted outside a list (was 10 nodes on #/map and 30 on
#     #/comparison).
#   * landmark-*        - index.html stopped nesting role=banner / role=contentinfo /
#     complementary inside the header and footer landmarks, and #map-root no longer shares the
#     legend's accessible name.
#   * region            - fixed with the same landmark pass.
# Any entry appearing here again is a regression, and the test fails on every serious,
# moderate or critical violation regardless of this list.
AXE_KNOWN: set[str] = set()

pytestmark = pytest.mark.e2e


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return bool(200 <= response.status < 400)
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="module")
def _browser():
    if not (_reachable(CONSOLE_URL + "/") and _reachable(API_HEALTH_URL)):
        pytest.skip("console preview or API not running")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def _dev():
    if not _reachable(DEV_URL + "/"):
        pytest.skip("console dev server not running (npm run dev)")
    return True


class Session:
    def __init__(self, browser, base: str, reduced_motion: str = "no-preference") -> None:
        self.js_errors: list[str] = []
        self.http_errors: list[str] = []
        self.context = browser.new_context(reduced_motion=reduced_motion)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda err: self.js_errors.append(f"uncaught: {err}"))
        self.page.on(
            "console",
            lambda m: m.type == "error" and self.http_errors.append(m.text),
        )
        self.base = base

    def go(self, route: str, transport: str | None = None, settle_ms: int = 5000) -> None:
        url = f"{self.base}/#/{route}"
        if transport:
            url = f"{self.base}/?transport={transport}#/{route}"
        self.page.goto(url, wait_until="load")
        self.page.wait_for_selector(".safety-banner")
        roots = ROUTE_ROOTS.get(route, [])
        if roots:
            try:
                self.page.wait_for_selector(f"[data-panel-root='{roots[0]}']", timeout=20_000)
            except Exception:
                pass
        self.page.wait_for_timeout(settle_ms)

    def close(self) -> None:
        self.context.close()


def _sweep(browser, base: str, transport: str | None, prefix: str) -> dict:
    sess = Session(browser, base)
    out: dict[str, dict] = {}
    for route, roots in ROUTE_ROOTS.items():
        sess.go(route, transport=transport)
        page = sess.page
        banners = page.locator(".safety-banner")
        out[route] = {
            "banners": banners.count(),
            "banner_text_ok": BANNER in (banners.first.inner_text() or ""),
            "banner_visible": banners.first.is_visible(),
            "roots_present": {r: page.locator(f"[data-panel-root='{r}']").count() > 0 for r in roots},
            "placeholder_leak": "No panel is registered" in page.inner_text("body"),
            "js_errors": list(sess.js_errors),
            "console_error_lines": list(sess.http_errors),
        }
        page.screenshot(path=str(ARTIFACTS / f"{prefix}-{route}.png"))
    sess.close()
    (ARTIFACTS / f"{prefix}-sweep.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def test_built_preview_route_matrix(_browser) -> None:
    out = _sweep(_browser, CONSOLE_URL, None, "live")
    for route, res in out.items():
        assert res["banners"] >= 2 and res["banner_text_ok"] and res["banner_visible"], route
        assert all(res["roots_present"].values()), f"{route} roots: {res['roots_present']}"
        assert not res["placeholder_leak"], f"{route} shows a placeholder"
        assert res["js_errors"] == [], f"{route} uncaught JS: {res['js_errors']}"


def test_preview_has_no_fixture_transport(_browser) -> None:
    # `?transport=fixtures` is a dev-only alias of `?transport=synthetic`. On the built
    # preview it must still change nothing: production has no synthetic transport to reach.
    sess = Session(_browser, CONSOLE_URL)
    sess.go("economics", transport="fixtures")
    body = sess.page.inner_text("body")
    lower = body.lower()
    sess.close()
    assert "synthetic dev transport" not in lower, "fixtures mode somehow active in a built bundle"
    assert "forced by ?transport" not in lower, "the built bundle accepted a fixture transport"
    # Inverted on purpose (audit F-7 / docs/05 5.9, docs/07 7.2): cost figures are REQUIRED to
    # carry the illustrative label. The old assertion forbade the word, which pinned the very
    # honesty rule the panel exists to enforce.
    money = re.findall(r"₹|\bINR\b|\bUSD\b|x road", body, re.I)
    assert money, f"economics rendered no cost figure at all on live: {body[:250]}"
    assert "illustrative" in lower, f"cost figures without the illustrative label: {money[:6]}"
    cost_line = body.lower().find("modelled cost")
    assert cost_line >= 0, "the cost card is gone from the economics panel"
    window = lower[max(0, cost_line - 400): cost_line + 400]
    assert "illustrative" in window, f"cost card without the label nearby: {window[-260:]!r}"


def test_dev_synthetic_route_matrix(_browser, _dev) -> None:
    out = _sweep(_browser, DEV_URL, "synthetic", "synthetic")
    seen: set[str] = set()
    for route, res in out.items():
        seen.update(r for r, ok in res["roots_present"].items() if ok)
        assert res["banner_text_ok"] and not res["placeholder_leak"], route
        assert res["js_errors"] == [], f"{route} JS: {res['js_errors']}"
    all_roots = {r for roots in ROUTE_ROOTS.values() for r in roots}
    assert seen == all_roots, f"8 panel roots expected, mounted {sorted(seen)}"


def test_keyboard_rejection_path_measured(_browser, _dev) -> None:
    sess = Session(_browser, DEV_URL)
    sess.go("missions", transport="synthetic", settle_ms=10000)
    page = sess.page
    row = page.locator("button", has_text=re.compile(r"Open its decision trace", re.I))
    assert row.count() >= 1, "synthetic queue offers no refused-mission trigger"
    row.first.click()
    page.wait_for_selector("[data-panel-root='decision-trace']", timeout=15_000)
    page.wait_for_timeout(6000)
    trigger = page.locator("button", has_text=re.compile(r"Open the technical detail", re.I))
    assert trigger.count() >= 1, (
        "rejection technical-detail trigger not reachable after selecting a refused mission; "
        "builder claim of an 8-stop path cannot be reproduced: "
        + page.inner_text("[data-panel-root='decision-trace']")[:300]
    )
    # Measure from the true document start, the way the builder wrote the claim. Chrome keeps
    # the sequential-focus chain at the clicked queue row after a blur, so rewind by tabbing
    # until the skip link - the document's first stop - holds focus. Without this the count
    # includes the tail of the panel the click already left the operator in, which is how the
    # audit landed on 13 presses for the rejection path (F-1).
    page.evaluate("document.activeElement && document.activeElement.blur()")
    at_document_start = False
    for _ in range(40):
        page.keyboard.press("Tab")
        at_document_start = bool(
            page.evaluate("() => document.activeElement?.classList?.contains('skip-link') === true")
        )
        if at_document_start:
            break
    assert at_document_start, "never reached the skip link: the document has no first tab stop"

    state: dict = {}

    def tab_until(pattern: re.Pattern, limit: int = 40) -> tuple[int, dict]:
        last: dict = {}
        for i in range(1, limit + 1):
            page.keyboard.press("Tab")
            last = page.evaluate(
                """() => {const a=document.activeElement; const cs=getComputedStyle(a);
                   return {tag:a.tagName, text:(a.textContent||'').trim().slice(0,80),
                           aria:a.getAttribute('aria-label')||'',
                           outline:`${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`,
                           focus_visible:a.matches(':focus-visible')};}"""
            )
            hay = (last["text"] + " " + last["aria"]).lower()
            if pattern.search(hay):
                return i, last
        return -1, last

    n_reject, reject_info = tab_until(re.compile(r"refus|reject"))
    n_detail, detail_info = tab_until(re.compile(r"open the technical detail"), limit=20)
    assert n_detail > 0, f"drawer trigger not keyboard-reachable: {detail_info}"
    # Repairs for audit F-1, now pinned rather than claimed: the refusal is its own tab stop
    # (it was a non-focusable h4), the route list is one stop instead of seven, and the hop
    # from the refusal to its technical explanation is a handful of presses, not thirteen.
    assert n_reject > 0, "the refusal is not reachable by Tab at all: still a non-focusable heading"
    assert reject_info["tag"] == "H4", f"first reachable rejection is not the refusal heading: {reject_info}"
    assert n_reject <= 4, f"document start -> refusal took {n_reject} stops (was 13)"
    assert n_detail <= 4, f"refusal -> technical detail took {n_detail} stops"

    trigger.first.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".kit-drawer", timeout=5_000)
    drawer_focus = page.evaluate("() => document.activeElement.closest('.kit-drawer') !== null")
    inside = 0
    for _ in range(10):
        page.keyboard.press("Tab")
        if not page.evaluate("() => document.activeElement.closest('.kit-drawer') !== null"):
            break
        inside += 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    returned = page.evaluate("() => /technical detail/i.test(document.activeElement.textContent||'')")
    page.screenshot(path=str(ARTIFACTS / "drawer-open.png"))

    measured = {
        "route": "decision on dev+synthetic",
        "stops_to_first_rejection_word": n_reject,
        "stops_from_cold_document_start_to_refusal": n_reject + 1,
        "stops_from_cold_document_start_to_detail_trigger": n_reject + 1 + n_detail,
        "stops_onward_to_technical_detail_trigger": n_detail,
        "refusal_stop_tag": reject_info["tag"],
        "refusal_is_a_tab_stop": reject_info["tag"] == "H4",
        "trigger_outline": detail_info["outline"],
        "trigger_focus_visible": detail_info["focus_visible"],
        "drawer_grabs_focus_on_open": drawer_focus,
        "tabs_spent_cycling_inside_drawer": inside,
        "escape_returns_focus_to_trigger": returned,
        "builder_claim": "8 stops to rejection, 1-stop hop, Escape restores",
    }
    (ARTIFACTS / "keyboard-path.json").write_text(json.dumps(measured, indent=2), encoding="utf-8")
    assert drawer_focus, "drawer does not take focus on open"
    assert returned, "Escape does not return focus to the trigger"
    assert inside >= 1, "drawer does not hold focus"
    assert sess.js_errors == []
    sess.close()


def _select_refused_mission_and_open_replay(sess: Session) -> None:
    """Replay controls only mount for a mission in scope: pick the refused one in the
    queue, jump to its decision trace, then press 'Step the replay for this mission'."""
    sess.go("missions", transport="synthetic", settle_ms=10000)
    row = sess.page.locator("button", has_text=re.compile(r"Open its decision trace", re.I))
    assert row.count() >= 1, "synthetic queue offers no refused-mission trigger"
    row.first.click()
    sess.page.wait_for_selector("[data-panel-root='decision-trace']", timeout=15_000)
    sess.page.wait_for_timeout(5000)
    step = sess.page.locator("button", has_text=re.compile(r"Step the replay", re.I))
    assert step.count() >= 1, "decision trace offers no replay handoff"
    step.first.click()
    sess.page.wait_for_selector("[data-panel-root='replay']", timeout=15_000)
    sess.page.wait_for_timeout(6000)


def test_reduced_motion_removes_play_and_transition(_browser, _dev) -> None:
    sess = Session(_browser, DEV_URL)
    _select_refused_mission_and_open_replay(sess)
    normal_play = sess.page.locator("button", has_text=re.compile(r"\bplay\b", re.I)).count()
    sess.close()

    sess = Session(_browser, DEV_URL, reduced_motion="reduce")
    _select_refused_mission_and_open_replay(sess)
    page = sess.page
    reduced_play = page.locator("button", has_text=re.compile(r"\bplay\b", re.I)).count()
    steps = page.locator("button", has_text=re.compile(r"step|frame", re.I)).count()
    nonzero = page.evaluate(
        """() => [...document.querySelectorAll('body *')].map(el => {
             const cs = getComputedStyle(el);
             return [cs.transitionDuration, cs.animationDuration]; })
         .filter(([t,a]) => (t !== '0s' && parseFloat(t) > 0.002) || (a !== '0s' && parseFloat(a) > 0.002)).slice(0,8)"""
    )
    micro = page.evaluate(
        """() => [...document.querySelectorAll('body *')].filter(el => {
             const cs = getComputedStyle(el);
             return cs.transitionDuration === '0.000001s' || cs.transitionDuration === '1e-06s';}).length"""
    )
    slider = page.locator("input[type=range], [role=slider]").first
    slid = slider.count() > 0
    before = after = None
    if slid:
        slider.focus()
        before = slider.evaluate("el => el.getAttribute('aria-valuenow') || el.value")
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(300)
        after = slider.evaluate("el => el.getAttribute('aria-valuenow') || el.value")
    live_regions = page.locator("[aria-live], [role=status]").count()
    sess.close()
    report = {
        "play_buttons_normal_motion": normal_play,
        "play_buttons_reduced_motion": reduced_play,
        "step_buttons_reduced": steps,
        "elements_with_nonzero_motion_styles": nonzero,
        "elements_with_microsecond_1e_06s_durations": micro,
        "slider_present": slid,
        "valuenow_before": before,
        "valuenow_after_arrowright": after,
        "live_regions": live_regions,
    }
    (ARTIFACTS / "reduced-motion.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert reduced_play == 0, "play control survives prefers-reduced-motion: reduce"
    assert reduced_play <= normal_play
    assert nonzero == [], f"motion leaks under reduced motion: {nonzero}"
    # Audit F-5: the reduced-motion override used to compute 0.000001s. The claim is now the
    # behaviour - zero elements report a microsecond residual, everything is a literal 0s.
    assert micro == 0, f"{micro} elements still compute a 0.000001s transition under reduce"
    if slid:
        assert before != after, "arrow-key stepping does not move the playhead"


def test_legend_second_channels(_browser, _dev) -> None:
    sess = Session(_browser, DEV_URL)
    sess.go("map", transport="synthetic", settle_ms=6000)
    page = sess.page
    legend = page.locator(".map-summary").first.inner_text()
    canvas = page.locator("canvas").count() > 0
    layer_rows = page.locator(".map-summary").first.inner_text()
    tags = {t: layer_rows.count(t) for t in ("SYNTH", "LEDGER", "PLAN", "MEASURED", "UNKNOWN", "REJECTED")}
    descriptive = {w: legend.lower().count(w) for w in ("dash", "hollow", "diamond", "hatch", "ring", "opacity")}
    page.screenshot(path=str(ARTIFACTS / "legend-map.png"))
    sess.close()
    (ARTIFACTS / "legend.json").write_text(
        json.dumps({"legend": legend, "state_tags": tags, "channel_words": descriptive}, indent=2),
        encoding="utf-8",
    )
    assert canvas, "map canvas missing"
    for tag_word in ("SYNTH", "PLAN", "MEASURED", "LEDGER"):
        assert tags[tag_word] > 0, f"legend has no non-colour tag for {tag_word}"
    assert tags["UNKNOWN"] > 0, "uncertain state has no text channel in legend"
    assert "not official" in legend.lower(), "synthetic keep-outs not worded as unofficial"


def test_axe_over_every_route(_browser, _dev) -> None:
    assert VENDOR_AXE.exists(), "vendor axe.min.js missing"
    script = VENDOR_AXE.read_text(encoding="utf-8")
    sess = Session(_browser, DEV_URL)
    results: dict[str, dict] = {}
    new_violations: list[str] = []
    for route in ROUTE_ROOTS:
        sess.go(route, transport="synthetic", settle_ms=4000)
        sess.page.evaluate(script)
        res = sess.page.evaluate(
            "async () => await axe.run(document,{runOnly:['wcag2a','wcag2aa','wcag21a','wcag21aa','best-practice']})"
        )
        rows = [
            {"id": v["id"], "impact": v.get("impact"), "nodes": len(v["nodes"]),
             "targets": [n["target"] for n in v["nodes"][:2]]}
            for v in res["violations"]
        ]
        results[route] = {"violations": rows,
                          "passes": res["passes"].__len__()}
        for v in rows:
            if v["id"] not in AXE_KNOWN and v["impact"] in ("critical", "serious", "moderate"):
                new_violations.append(f"{route}: {v['id']} ({v['impact']}, {v['nodes']} nodes)")
        sess.close()
        sess = Session(_browser, DEV_URL)
    by_impact: dict[str, int] = {}
    for sweep in results.values():
        for v in sweep["violations"]:
            impact = v["impact"] or "unknown"
            by_impact[impact] = by_impact.get(impact, 0) + v["nodes"]
    report = {"by_impact_nodes": by_impact, "per_route": results}
    (ARTIFACTS / "axe-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert new_violations == [], "axe critical/serious/moderate violations:\n" + "\n".join(new_violations)
    assert by_impact.get("serious", 0) == 0, f"serious nodes remain: {by_impact}"
    assert by_impact.get("moderate", 0) == 0, f"moderate nodes remain: {by_impact}"
    assert by_impact.get("critical", 0) == 0, f"critical nodes remain: {by_impact}"


def test_live_denial_states_are_designed(_browser) -> None:
    """The designed 403 state, provoked with the credential that still gets one.

    `simulation:read` is granted to the console's observer role (`sim-observer-a`) now, so the
    panels show figures instead of a denial under the shipped credential - that change is
    deliberate and asserted by test_preview_has_no_fixture_transport. The denial probe is
    re-pointed at `sim-partner-a` (partner_order_writer), which still gets a 403 on the
    simulation reads, so the designed denial state stays covered without pinning a permission
    the read plane no longer withholds.
    """
    def _status(token: str) -> int:
        request = urllib.request.Request(
            API_HEALTH_URL.replace("/health", "/v1/simulations/chandigarh-basic"),
            headers={"authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)

    observer, partner = _status("sim-observer-a"), _status("sim-partner-a")
    assert observer == 200, f"observer lost simulation:read ({observer}); the panels show a denial"
    assert partner == 403, f"sim-partner-a is no longer the denial probe (got {partner})"

    sess = Session(_browser, CONSOLE_URL)
    sess.page.route(
        "**/v1/**",
        lambda route: route.continue_(
            headers={**dict(route.request.headers), "authorization": "Bearer sim-partner-a"}
        ),
    )
    sess.go("economics", settle_ms=6000)
    econ = sess.page.inner_text("[data-panel-root='economics']")
    sess.go("comparison", settle_ms=6000)
    comp = sess.page.inner_text("[data-panel-root='comparison']")
    sess.go("missions", settle_ms=6000)
    queue_body = sess.page.inner_text("[data-panel-root='mission-queue']")
    sess.page.screenshot(path=str(ARTIFACTS / "live-empty-queue.png"))
    sess.close()
    denial = re.compile(r"(permission|denied|authoris|capability 'simulation:read'|403)", re.I)
    report = {"economics_denial": bool(denial.search(econ)),
              "comparison_denial": bool(denial.search(comp)),
              "denial_credential": "sim-partner-a",
              "observer_status": observer,
              "partner_status": partner,
              "queue_head": queue_body[:300]}
    (ARTIFACTS / "live-states.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["economics_denial"], f"economics denial state missing: {econ[:200]}"
    assert report["comparison_denial"], f"comparison denial state missing: {comp[:200]}"
    assert econ.lower().count("₹") == 0, "economics invented figures while denied"


def test_honesty_of_numbers_under_synthetic(_browser, _dev) -> None:
    sess = Session(_browser, DEV_URL)
    sess.go("economics", transport="synthetic", settle_ms=8000)
    body = sess.page.inner_text("body")
    lower = body.lower()
    assert "illustrative" in lower, "synthetic cost figures lack the illustrative label"
    assert "formula" in lower and "denominator" in lower, "formulas/denominators not reachable"
    assert "synthetic dev transport" in lower, "synthetic provenance not named on screen"
    forbidden = re.compile(r"(validated business case|approved route|now authorised|has been authorised|cleared for flight|flight ready)", re.I)
    m = forbidden.search(body)
    assert m is None, f"authorisation-implying wording: {m}"
    sess.page.screenshot(path=str(ARTIFACTS / "economics-synthetic.png"))
    sess.go("comparison", transport="synthetic", settle_ms=8000)
    comp = sess.page.inner_text("body").lower()
    assert "not a recommendation" in comp or "illustrative" in comp
    sess.close()


def test_no_command_affordance_all_routes(_browser, _dev) -> None:
    forbidden = re.compile(
        r"\b(launch|takeoff|take-off|land|landing|return to base|rtl|arm|disarm|upload mission|"
        r"set param\w*|flash firmware|send command|execute mission|emergency stop|fly it)\b",
        re.I,
    )
    sess = Session(_browser, DEV_URL)
    offenders: list[str] = []
    for route in ROUTE_ROOTS:
        sess.go(route, transport="synthetic", settle_ms=2500)
        labels = sess.page.evaluate(
            """() => [...document.querySelectorAll('button, input[type=submit], input[type=button], a, [role=button], form')]
                 .map(el => `${el.tagName}|${(el.textContent||'').trim().slice(0,90)}|${el.getAttribute('aria-label')||''}|${el.getAttribute('action')||''}`)"""
        )
        offenders += [f"{route}: {l}" for l in labels if forbidden.search(l)]
        hidden = sess.page.evaluate(
            """() => [...document.querySelectorAll('button, form')].filter(el => el.closest('[hidden]')).length"""
        )
        assert hidden == 0, f"{route}: {hidden} hidden command-capable controls"
    sess.close()
    (ARTIFACTS / "command-scan.json").write_text(json.dumps(offenders, indent=2), encoding="utf-8")
    assert offenders == [], "command-like affordance: " + "; ".join(offenders)


def test_bundle_has_no_scheduling_or_command_code_path() -> None:
    chunks = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in DIST.glob("index-*.js")}
    assert chunks, "no built console chunks in dist/assets"
    joined = "\n".join(chunks.values())
    methods = re.findall(r"method\s*:\s*[\"'](POST|PUT|PATCH|DELETE)[\"']", joined)
    write_paths = re.findall(r"[\"'`]/v1/[a-z0-9/_-]*(command|launch|upload|firmware|param)[a-z0-9/_-]*[\"'`]", joined, re.I)
    sched = re.findall(r"\b(rankCandidates|sortCandidates|selectAircraft|validateReservation|commitTransaction|assignAircraft)\b", joined)
    http_verbs = re.findall(r"\bfetch\([^)]*\bmethod\b", joined)
    report = {"non_get_methods": methods, "command_paths": write_paths,
              "scheduler_identifiers": sched, "fetch_with_method": http_verbs,
              "chunks_scanned": sorted(chunks)}
    (ARTIFACTS).mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "bundle-scan.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert methods == [], f"console bundle issues write methods: {methods}"
    assert write_paths == [], f"command endpoints in bundle: {write_paths}"
    assert sched == [], f"scheduling identifiers in bundle: {sched}"
