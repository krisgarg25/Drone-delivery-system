# Phase 4 operations console — independent audit

Audited 2026-10-01 by an agent that did not build the console. All browser evidence is from
Chromium (Playwright 1.63) against `http://127.0.0.1:4173` (built preview) and
`http://127.0.0.1:5173` (`npm run dev`), API `http://127.0.0.1:8000` (Postgres on :55432).
Enforcing specs: `tests/e2e/test_console_audit.py` (11 tests) and
`apps/console/tests/console-audit.spec.ts` (10 tests). Status at delivery:
`uv run --no-sync pytest tests/e2e -q --no-cov` → **14 passed** (3 builder smoke tests included);
`npx playwright test --config tests/playwright.config.ts` → **10 passed**.
Artifacts (screenshots + JSON measurements): `apps/console/tests/artifacts/`.

Exact commands re-run by the auditor:

```
npm --prefix apps/console run build          # tsc --noEmit && vite build — OK, 967 modules
npx --prefix apps/console tsc --noEmit       # OK
npx --prefix apps/console eslint .           # OK (after tests/ was added to ignores; see F-8)
apps/console: npm run preview  (:4173)       # already running at audit start; verified it served
                                             # the freshly built dist (asset hash index-DGCRliBp.js matched)
uv run --no-sync uvicorn --app-dir apps/api/src droneops_api.main:app --port 8000  # API already running; /health and /version confirmed, not restarted
apps/console: npm run dev (:5173)            # started by auditor for the fixture transport
```

## Findings table

| # | Sev | Finding (observed evidence) | Where | Reproduce |
|---|-----|------------------------------|-------|-----------|
| F-1 | **major** | The builder's "rejection path in 8 tab stops" is not reproducible as claimed. From document start on `#/decision` (dev+synthetic, refused mission selected) the **measured** distance to the "Open the technical detail" trigger is **13 stops**, not 8 (`artifacts/keyboard-path.json`). The refusal itself is never a tab stop — "Refused: …" is a non-focusable `h4`. The 1-stop hop to the drawer *is* true (the trigger opens it directly), Escape returns focus to the trigger, drawer traps focus (≥10 cycles), ring is `solid 3px rgb(255,224,102)` with `:focus-visible` true. | `apps/console/src/panels/decision-trace.ts`, `panels/support/kit.ts` | `test_keyboard_rejection_path_measured` |
| F-2 | **major** | The rejection→detail→replay flow is **unreachable on the shipped artifact**. Production builds contain no fixture transport (by design, `main.ts:40`) and the live DB has zero orders, so on :4173 every per-mission panel is an honest empty/denial state. All interactive-path evidence exists only on the dev server. Not a code defect — but the builder's headline interaction claims cannot be verified against the thing they ship. | `src/main.ts`, live DB | `test_built_preview_route_matrix` + manual: no button other than nav/CTA on :4173 |
| F-3 | **major** | axe-core reports a **serious `dlitem` violation** (dt/dd not inside a `dl`): 10 nodes on `#/map`, **30 nodes on `#/comparison`** (`artifacts/axe-results.json`). Builder's "26/26" was a contrast-only audit; screen readers will lose the label/value grouping on exactly the two panels that carry legend and metric semantics. | `panels/support/kit.ts` field-list markup | `test_axe_over_every_route` (allowlist `AXE_KNOWN` pins it so it must not grow) |
| F-4 | minor | Duplicate/conflicting landmarks on **every** route (6 moderate axe rules): `p[role=banner]` + `p[role=contentinfo]` in `index.html` nest inside `header#masthead`/`footer#colophon`, producing `landmark-banner-is-top-level`, `landmark-no-duplicate-banner`, `landmark-unique`, etc. The banner *text* is present and visible everywhere (the actual safety requirement passes); the landmark nesting is wrong. | `apps/console/index.html` | axe sweep above |
| F-5 | minor | "transition-duration computes 0s" is **inaccurate**: under `prefers-reduced-motion: reduce`, 181 elements compute `0.000001s`, not `0s` (`artifacts/reduced-motion.json`). Behaviourally compliant (nothing ≥2 ms), claim wording false. Play control absent under reduce (0 vs 1 with motion), 4 step buttons remain, range-scrubber arrows step. | `src/styles/tokens.css` motion override | `test_reduced_motion_removes_play_and_transition` |
| F-6 | minor | Builder evidence keyword "`?transport=fixtures`" **does not exist**. The app knows `?transport=synthetic` (dev-only) and `?transport=live`. On the built preview the flag is inert (pinned by test). The admission therefore misdescribes the only fixture mode that exists, though the direction is safe: fixtures cannot leak into production. | `src/main.ts:21-27` | `test_preview_has_no_fixture_transport` |
| F-7 | minor | Economics/comparison on live show the designed `simulation:read` denial verbatim ("Credential is not authorised for capability 'simulation:read'") with **no invented figures** — claim confirmed. Queue empty because `listOrders` returns none — confirmed. Under synthetic, every number is labelled, formulas **and** denominators are printed inline, provenance reads "synthetic dev transport (fixtures, not live data)", comparison carries "Not a recommendation"; no wording asserts flight authorisation (the only "AUTHORISED FOR FLIGHT" is a label whose value is "no"). Honesty claims pass. | `panels/economics.ts`, `comparison.ts`, `lib/api/mock.ts` | `test_live_denial_states_are_designed`, `test_honesty_of_numbers_under_synthetic` |
| F-8 | minor | `npm run lint` (eslint **plus** prettier) fails on shipped files the builder never checked: `prettier --check .` flags `README.md` and `src/map/map-view.ts`. Their claim "npx eslint . clean" is literally true; their own lint script is not. | repo `apps/console` | `npx prettier --check .` in `apps/console` |
| F-9 | minor | Touch targets: **9 interactive controls below 44×44 px** at a 390 px viewport on `#/missions` (measured via `getBoundingClientRect`; includes queue row buttons/links). None verified under 24 px (WCAG 2.2 AA floor), so this is AAA/Apple-friendliness, not an AA blocker — but the "controller on a tablet" persona deserves a fix pass. | `panels/mission-queue.css` / `panels.css` | probe snippet in audit session |
| F-10 | info | **No scheduling or command code path in the browser — exit criterion holds.** Scanned the *built* chunks `dist/assets/index-*.js`: no non-GET `method:`, no `/v1/...command|launch|upload|firmware|param` strings, no scheduler identifiers (`rankCandidates` etc.) — `artifacts/bundle-scan.json`. DOM sweep of all 7 routes × all `button/a/form/[role=button]` incl. aria-labels found zero forbidden command verbs and zero hidden controls inside `[hidden]` subtrees. | built bundle + every route | `test_no_command_affordance_all_routes`, `test_bundle_has_no_scheduling_or_command_code_path` |
| F-11 | info | Legend/second-channel: the on-page legend (`.map-summary`) names every layer with **live feature counts drawn from the same stores the canvas renders** and a non-colour tag per state (`SYNTH`, `LEDGER`, `PLAN`, `MEASURED`, `UNKNOWN`, `REJECTED`); uncertainty prints `± Ns`; synthetic keep-outs carry the words "not official". Pixel-level legend↔canvas agreement was not machine-tested (see below), but no legend/map disagreement was found at the text/structural level. | `src/map/layers.ts`, `shell/shell.ts` | `test_legend_second_channels`, `artifacts/legend-map.png` |
| F-12 | info | Structural claims confirmed: `tsc --noEmit` and `eslint .` clean; build OK; all 7 routes (there is **no `#/exceptions` route** — exceptions is a panel in `map.context`; "8 panels in 7 slots" is accurate) render both `.safety-banner`s with exact text, mount all 8 `[data-panel-root]` values, leak no "No panel is registered", and produce **zero uncaught JS errors** in both transports. Builder's "no console errors" claim is true for JS errors; note that 403s still appear as browser resource-log lines on live (normal, not app errors). | all | `test_built_preview_route_matrix`, `test_dev_synthetic_route_matrix`, `artifacts/live-*.png`, `artifacts/synthetic-*.png` |

## Verdict buckets

**Verified independently:** build/typecheck/eslint-clean, route matrix + banner + panel roots + zero JS errors (preview and dev), designed denial states, honesty labelling under synthetic data, no-command and no-scheduling boundaries (bundle + DOM), focus trap/Escape-restore/focus ring on the drawer, reduced-motion removal of play, arrow-steppable range scrubber, legend second channels and counts.

**Failed / corrected:** F-1 (13 stops, not 8; refusal not a tab stop), F-3 (serious axe `dlitem`, 30 nodes on comparison), F-4 (landmark duplication on all routes), F-5 (0.000001s ≠ 0s), F-6 (no such keyword as `?transport=fixtures`), F-8 (repo `lint` script fails on prettier), F-9 (9 sub-44px targets at mobile width).

**Could not test, and why:**
- Pixel-level identity of legend colours/dashes vs deck.gl canvas output — WebGL readback is not reliable in headless; covered by text/structure checks plus screenshots for human eyes.
- Rejection path on **live** data — the database has no orders; only dev+synthetic can demonstrate it (F-2).
- Live-region *spoken* announcements during replay stepping — two live regions (`role=status`) exist and code announces on step; TTS/AT output was not observed.
- The preview/API servers were already running at audit start (strictPort means I could not re-bind them); I verified the preview serves exactly my rebuilt `dist` (matching asset hash) and the API via `/health` + `/version`, so the artifact under test is current.

## Enforcement notes

- `tests/e2e/test_console_audit.py` — skips cleanly when servers are down (same gate as the builder's smoke tests). The axe test carries an explicit `AXE_KNOWN` allowlist (F-3/F-4) so those defects may not silently *grow*; any new serious/critical violation fails.
- `apps/console/tests/console-audit.spec.ts` + `playwright.config.ts` — pins the shipped artifact's guarantees (banner, roots, inert fixtures, reduced motion, visible focus in nav). `tests/` was added to `eslint.config.js` ignores and `.prettierignore`; `tsconfig.json` never included it, so the build gate is unaffected.
