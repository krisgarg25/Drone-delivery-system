# geom-4d

The swept 4D protected-volume kernel: `RouteProfile`, continuous swept prisms,
conservative discretisation, climb/descent envelopes with transition buffers,
merge and crossing volumes, exact intersection returning
`SAFE | CONFLICT | UNPROVEN`, and a `geom_core` cell-and-time-window pre-filter.

Every margin this package applies is a versioned input read from
`contracts/configuration/protected_volume_policy.yaml` - horizontal margin,
vertical margin, position uncertainty and timing uncertainty stay four separate
numbers, plus the transition buffer. Those values are **experiment parameters**,
not certified aviation rules; the safety requirement is the invariant suite, not
any single margin.

Simulation only. No aircraft command authority, and no planner dependency:
`lint-imports` enforces that `droneops_geom_4d` cannot reach `droneops_planner`,
and `droneops_conflict_checker` cannot reach this package - the checker is meant
to be a deliberately different implementation, so two answers can be evidence
rather than one answer being an assertion.

Start with the module docstrings: `intersect.py` states the exact-arithmetic
decision procedure and what an independent checker should do differently;
`sweep.py` states why the continuous sweep and the conservative discretisation
are two paths over the same profile, and why per-frame sampling is not a conflict
basis.
