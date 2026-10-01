import pathlib


def patch(path, pairs):
    p = pathlib.Path(path)
    s = p.read_text(encoding="utf-8")
    for old, new in pairs:
        assert s.count(old) == 1, (path, s.count(old), old[:70])
        s = s.replace(old, new)
    p.write_text(s, encoding="utf-8")


patch(
    "packages/reservations/src/droneops_reservations/ledger/sql.py",
    [
        (
            'SELECT_MISSION_ROWS = """',
            '''# The final set the checker votes over: every protected row overlapping the window, on any
# resource key. A view keyed only on the requested rows cannot see a pair that holds two
# adjacent lanes over the same minutes - that is the planner's view, not the checker's.
SELECT_PROTECTED_IN_WINDOW = """
select id as reservation_id, mission_id, resource_key, state::text as state,
       lower(valid_during) as valid_from, upper(valid_during) as valid_until,
       commitment_token, config_release_sha
  from reservation
 where tenant_id = $1
   and state = any($2::text[])
   and valid_during && tstzrange($3, $4, '[)')
 order by resource_key, lower(valid_during), id
"""

SELECT_MISSION_ROWS = """''',
        ),
        ('    "SELECT_MISSION_ROWS",', '    "SELECT_MISSION_ROWS",\n    "SELECT_PROTECTED_IN_WINDOW",'),
    ],
)

patch(
    "packages/reservations/src/droneops_reservations/ledger/store.py",
    [
        (
            "    async def mission_rows(self, tx: Transaction, mission_id: UUID) -> tuple[ReservationRow, ...]:",
            '''    async def protected_in_window(
        self, tx: Transaction, states: Sequence[str], valid_from: datetime, valid_until: datetime
    ) -> tuple[ReservationRow, ...]:
        """Every protected reservation overlapping the window, on any resource key."""
        rows = await tx.fetch(sql.SELECT_PROTECTED_IN_WINDOW, tx.tenant_id, list(states), valid_from, valid_until)
        return tuple(_reservation(row) for row in rows)

    async def mission_rows(self, tx: Transaction, mission_id: UUID) -> tuple[ReservationRow, ...]:''',
        ),
    ],
)

patch(
    "packages/reservations/src/droneops_reservations/ledger/service.py",
    [
        (
            """            final = FinalSet(
                requested=claims,""",
            """            final = FinalSet(
                requested=claims,
                holders=await self._final_set(tx, claims, holders),""",
        ),
        (
            "    async def _holders(self, tx: Transaction, claims: Sequence[ResourceClaim]) -> tuple[ProtectedHolding, ...]:",
            '''    async def _final_set(
        self,
        tx: Transaction,
        claims: Sequence[ResourceClaim],
        keyed: tuple[ProtectedHolding, ...],
    ) -> tuple[ProtectedHolding, ...]:
        """The set the checker votes over: this request plus everything protected in the window.

        Keyed holders alone would let a pair holding two adjacent lanes over the same minutes
        read as an empty final set, which is the planner's view and not the checker's
        (conventions section 6).
        """
        if not claims:
            return keyed
        valid_from = min(claim.valid_from for claim in claims)
        valid_until = max(claim.valid_until for claim in claims)
        rows = await self._store.protected_in_window(tx, protected_states(), valid_from, valid_until)
        merged = list(keyed)
        seen = {(holder.resource_key, holder.mission_id) for holder in keyed}
        for row in rows:
            if (row.resource_key, row.mission_id) not in seen:
                seen.add((row.resource_key, row.mission_id))
                merged.append(_holding(row))
        return tuple(
            sorted(
                merged,
                key=lambda holder: (
                    identity_sort_key(identity_of(holder.resource_key)),
                    holder.valid_from,
                    holder.mission_id.hex,
                ),
            )
        )

    async def _holders(self, tx: Transaction, claims: Sequence[ResourceClaim]) -> tuple[ProtectedHolding, ...]:''',
        ),
    ],
)

patch(
    "packages/reservations/src/droneops_reservations/commit.py",
    [
        (
            "    claimed = AllowRule(named[0], named[1])",
            '''    if named is None:
        return Rejection(
            RejectionCode.ALLOW_RULE_UNMATCHED,
            f"validator granted {outcome.grant} to clear a conflict and this request named no rule",
        )
    claimed = AllowRule(named[0], named[1])''',
        ),
    ],
)

patch(
    "tests/concurrency/test_ledger_commit.py",
    [
        (
            '''    blocked = await ledger.commit(
        world.context(), command(world, "i3-second", claims=(corridor_claim(world.corridor, 14),))
    )''',
            '''    second_order, second_key = await add_order(store, world)
    blocked = await ledger.commit(
        world.context(),
        command(
            world,
            "i3-second",
            claims=(corridor_claim(world.corridor, 14),),
            order_id=second_order,
            idempotency_key=second_key,
        ),
    )''',
        ),
        (
            '''    later = await ledger.commit(
        world.context(), command(world, "i3-later", claims=(corridor_claim(world.corridor, 40),))
    )''',
            '''    later_order, later_key = await add_order(store, world)
    later = await ledger.commit(
        world.context(),
        command(
            world,
            "i3-later",
            claims=(corridor_claim(world.corridor, 40),),
            order_id=later_order,
            idempotency_key=later_key,
        ),
    )''',
        ),
    ],
)

patch(
    "tests/concurrency/test_ledger_rollback.py",
    [
        (
            """        await tx.execute(
            "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
            "valid_during, state, commitment_token, config_release_sha) "
            "values ($5, $3, $1, $6, 'corridor', tstzrange($7, $8, '[)'), 'committed', $9, $10)",
            planted,
            world.order_id,
            world.tenant_id,
            world.policy_version,
            uuid.uuid4(),
            f"corridor:{world.corridor}",
            *command(world, "planted", slot=9).candidates[0].ordered_claims()[0].window.as_datetimes(),
            "planted-token",
            world.config_release_sha,
        )""",
            """        await tx.execute(
            "insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, "
            "valid_during, state, commitment_token, config_release_sha) "
            "values ($1, $2, $3, $4, 'corridor', tstzrange($5, $6, '[)'), 'committed', $7, $8)",
            uuid.uuid4(),
            world.tenant_id,
            planted,
            f"corridor:{world.corridor}",
            *command(world, "planted", slot=9).candidates[0].ordered_claims()[0].window.as_datetimes(),
            "planted-token",
            world.config_release_sha,
        )""",
        ),
        (
            """    mission = uuid.uuid4()
    token = f"direct-{uuid.uuid4().hex[:8]}"

    async def insert(commitment: str) -> None:
        async with store.transaction(world.tenant_id) as tx:""",
            """    token = f"direct-{uuid.uuid4().hex[:8]}"

    async def insert(commitment: str) -> None:
        # Each out-of-band row is its own order's mission: unique (tenant_id, order_id) means
        # one order cannot carry two accepted missions.
        own_order, _ = await add_order(store, world)
        own_mission = uuid.uuid4()
        async with store.transaction(world.tenant_id) as tx:""",
        ),
        (
            """                "values ($1, $2, $3, 'accepted', 'routine', $4)",
                mission,
                world.order_id,""",
            """                "values ($1, $2, $3, 'accepted', 'routine', $4)",
                own_mission,
                own_order,""",
        ),
        (
            """                "values ($1, $2, $3, $4, 'corridor', tstzrange($5, $6, '[)'), 'committed', $7, $8)",
                uuid.uuid4(),
                world.tenant_id,
                mission,""",
            """                "values ($1, $2, $3, $4, 'corridor', tstzrange($5, $6, '[)'), 'committed', $7, $8)",
                uuid.uuid4(),
                world.tenant_id,
                own_mission,""",
        ),
    ],
)

patch(
    "tests/concurrency/test_ledger_stress.py",
    [
        (
            'LOCK = "select resource_key from resource_catalog where tenant_id = $1 and resource_key = $2 for update"',
            'LOCK = "update resource_catalog set label = label where tenant_id = $1 and resource_key = $2"',
        ),
    ],
)
print("patched")
