"""The SQL the eleven steps run.

Every statement is a named constant rather than a string built at the call site, so
the transaction the code executes is a transaction a reviewer can read. Three rules
hold throughout:

* every predicate carries ``tenant_id`` - row-level security is fail-closed and a
  missing tenant is a bug, not a convenience;
* every set the ledger writes, locks or reads is ordered by an explicit ``ORDER BY``
  on a canonical expression, never by physical or insertion order;
* identity lookups use ``split_part(resource_key, ':', n)`` against the *stored* key
  form, because a windowed key (``volume_cell:C-01:1700000..1700060``) is a different
  string from the same resource over another window. That is also why the ledger's
  step-8 recheck is not redundant with the database's exclusion constraint: the
  constraint compares ``resource_key`` for equality, so it can only ever catch two
  identical keys, and it indexes only ``('committed','activated')``.
"""

from __future__ import annotations

# ------------------------------------------------------------------ step 1: identity
# The advisory lock serialises the *identity* check for one commitment token without
# touching resource rows, so two simultaneous retries of the same request cannot each
# decide "not a duplicate". It is taken first and alone, so it cannot form a cycle with
# the canonical resource locks taken at step 7.
ADVISORY_LOCK = """
select pg_advisory_xact_lock(hashtextextended($1, 0))
"""

SELECT_ORDER = """
select id::text as row_id, status::text as status, config_release_sha,
       priority_class::text as priority_class
  from "order"
 where tenant_id = $1 and idempotency_key = $2
"""

SELECT_MISSION_BY_ORDER = """
select id::text as row_id, status::text as status, plan_version_id::text as plan_version_id
  from mission
 where tenant_id = $1 and order_id = $2
"""

SELECT_TOKEN_MISSIONS = """
select distinct mission_id::text as row_id
  from reservation
 where tenant_id = $1 and commitment_token = $2
"""

SELECT_TOKEN_RESERVATIONS = """
select id::text as reservation_id,
       mission_id::text as mission_id,
       resource_key,
       state::text as state,
       lower(valid_during) as valid_from,
       upper(valid_during) as valid_until
  from reservation
 where tenant_id = $1 and commitment_token = $2
 order by resource_key, lower(valid_during), id
"""

# ------------------------------------------------------------------ step 2: freeze
# The frozen world is the versioned configuration release plus the resource rows this
# plan touches, read inside this transaction. ``config_release`` is the row that makes
# "which policy numbers were in force" a fact rather than a recollection.
SELECT_CONFIG_RELEASE = """
select id::text as row_id, config_release_sha, policy_version,
       protected_volume_json::text as protected_volume
  from config_release
 where tenant_id = $1
   and config_release_sha = replace($2, 'sha256:', '')
   and policy_version = $3
"""

SELECT_WORLD_RESOURCES = """
select rc.resource_key,
       rc.kind::text as kind,
       rc.status::text as status,
       rc.capacity,
       coalesce(c.status::text, 'open') as corridor_status,
       rc.source_release
  from resource_catalog rc
  left join corridor c
    on c.tenant_id = rc.tenant_id
   and rc.resource_key = c.corridor_key
 where rc.tenant_id = $1
   and rc.resource_key = any($2::text[])
 order by rc.resource_key
"""

# ------------------------------------------------------------------ step 7: locks
# ``resource_catalog`` holds one row per *physical* resource (kind + identifier), so it
# is the natural serialisation point: two commits that touch the same corridor or the
# same volume cell queue here, in the one canonical order, and cannot interleave.
LOCK_RESOURCE = """
select resource_key, kind::text as kind, status::text as status, capacity
  from resource_catalog
 where tenant_id = $1 and resource_key = $2
 for update
"""

REGISTER_RESOURCE = """
insert into resource_catalog (resource_key, kind, tenant_id, capacity, status, source_release, label)
values ($1, $2::resource_kind, $3, 1, 'available', $4, $5)
on conflict (resource_key) do nothing
"""

MISSING_LOCKED_RESOURCE = """
select $1::text as resource_key
 where not exists (
     select 1 from resource_catalog where tenant_id = $2 and resource_key = $1
 )
"""

# ------------------------------------------------------------------ step 8: recheck
# ``state = any(...)`` includes ``unresolved`` deliberately. The database's own
# ``no_committed_overlap`` exclusion indexes only ``('committed','activated')``, so an
# unresolved row is protected here and nowhere else - this is invariant I3 executing.
SELECT_CLAIM_HOLDERS = """
select r.id::text as reservation_id,
       r.mission_id::text as mission_id,
       r.resource_key,
       r.state::text as state,
       lower(r.valid_during) as valid_from,
       upper(r.valid_during) as valid_until,
       p.profile_json::text as profile_json,
       p.route_hash,
       p.config_release_sha
  from reservation r
  left join plan_version p on p.mission_id = r.mission_id and p.tenant_id = r.tenant_id
 where r.tenant_id = $1
   and r.state::text = any($2::text[])
   and split_part(r.resource_key, ':', 1) = $3
   and split_part(r.resource_key, ':', 2) = $4
   and r.valid_during && tstzrange($5, $6, '[)')
 order by r.resource_key, lower(r.valid_during), r.id
"""

SELECT_CLAIM_VERSION = """
select coalesce(max(xmin::text::bigint), 0) as version, count(*) as holder_count
  from reservation
 where tenant_id = $1
   and state::text = any($2::text[])
   and split_part(resource_key, ':', 1) = any($3::text[])
   and split_part(resource_key, ':', 2) = any($4::text[])
"""

SELECT_COMMITTED_OVERLAPPING = """
select r.id::text as reservation_id,
       r.mission_id::text as mission_id,
       r.resource_key,
       r.state::text as state,
       lower(r.valid_during) as valid_from,
       upper(valid_during) as valid_until,
       r.commitment_token
  from reservation r
 where r.tenant_id = $1
   and r.state::text = any($2::text[])
   and split_part(r.resource_key, ':', 1) = split_part($3, ':', 1)
   and split_part(r.resource_key, ':', 2) = split_part($3, ':', 2)
   and r.valid_during && tstzrange($4, $5, '[)')
 order by r.resource_key, lower(r.valid_during), r.id
"""

# ------------------------------------------------------------------ step 9: records
INSERT_MISSION = """
insert into mission (id, order_id, tenant_id, aircraft_id, status, plan_version_id,
                     priority_class, policy_version)
values ($1, $2, $3, $4, $5::mission_status, $6, $7::priority_class, $8)
"""

UPDATE_MISSION_STATUS = """
update mission
   set status = $4::mission_status,
       plan_version_id = $5
 where id = $1 and tenant_id = $2 and status::text = $3
"""

INSERT_PLAN_VERSION = """
insert into plan_version (id, mission_id, tenant_id, route_hash, profile_json,
                          energy_margin_wh, config_release_sha, authoritative)
values ($1, $2, $3, $4, $5::jsonb, $6, $7, false)
"""

INSERT_RESERVATION = """
insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind, valid_during,
                         state, commitment_token, config_release_sha)
values ($1, $2, $3, $4, $5::resource_kind, tstzrange($6, $7, '[)'),
        'tentative'::reservation_state, $8, $9)
"""

INSERT_DECISION_RECORD = """
insert into decision_record (id, mission_id, tenant_id, input_hash, eligible_candidates_json,
                             rejected_candidates_json, selected_plan_id, reason_codes,
                             deciding_actor, config_release_sha)
values ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8::text[], $9, $10)
"""

# Promotion is a state-machine hop, and the ``and state = 'tentative'`` predicate is
# what makes it one: a row someone else already moved is not silently overwritten.
PROMOTE_RESERVATIONS = """
update reservation
   set state = 'committed'::reservation_state
 where tenant_id = $1 and id = any($2::uuid[]) and state = 'tentative'::reservation_state
"""

DEMOTE_TO_UNRESOLVED = """
update reservation
   set state = 'unresolved'::reservation_state
 where tenant_id = $1 and id = any($2::uuid[]) and state::text = any($3::text[])
"""

CANCEL_RESERVATIONS = """
update reservation
   set state = 'cancelled'::reservation_state,
       cancelled_reason_code = $3
 where tenant_id = $1 and mission_id = $2 and state::text = any($4::text[])
"""

SELECT_MISSION_STATUS = """
select id::text as row_id, status::text as status, order_id::text as order_id
  from mission
 where tenant_id = $1 and id = $2
"""

# A cancellation that found nothing committed still has to be *recorded*, and the mission
# row is where it is recorded: status ``cancelled`` is what makes a commit that arrives
# afterwards refuse, instead of handing airspace to a mission nobody is flying.
CANCEL_MISSION = """
update mission
   set status = 'cancelled'::mission_status
 where id = $1 and tenant_id = $2 and status::text = any($3::text[])
"""

SELECT_MISSION_ROWS = """
select id::text as reservation_id,
       resource_key,
       state::text as state,
       lower(valid_during) as valid_from,
       upper(valid_during) as valid_until
  from reservation
 where tenant_id = $1 and mission_id = $2
 order by resource_key, lower(valid_during), id
"""

# ------------------------------------------------------------------ step 10: outbox
SELECT_NEXT_EVENT_SEQ = """
select coalesce(max(event_seq), 0) + 1 as next_seq
  from domain_event
 where stream_type = $2::event_stream_type and stream_id = $3
"""

INSERT_DOMAIN_EVENT = """
insert into domain_event (id, tenant_id, stream_type, stream_id, event_seq, event_type,
                          schema_version, payload_json, occurred_at, received_at, actor,
                          correlation_id, causation_id, config_release_sha)
values ($1, $2, $3::event_stream_type, $4, $5, $6, 1, $7::jsonb, $8, $9, $10, $11, $12, $13)
"""

INSERT_OUTBOX = """
insert into outbox (event_id, tenant_id, attempts)
values ($1, $2, 0)
"""

# ------------------------------------------------------------------ read-back proofs
# The Phase 2 exit criterion, stated as the database sees it: no two protected rows
# share a resource identity and overlap in time.
SELECT_POLICY_VIOLATIONS = """
select a.resource_key as left_key, b.resource_key as right_key,
       a.mission_id::text as left_mission, b.mission_id::text as right_mission,
       lower(a.valid_during) as left_from, upper(a.valid_during) as left_to
  from reservation a
  join reservation b
    on a.id < b.id
   and a.state::text = any($1::text[])
   and b.state::text = any($1::text[])
   and split_part(a.resource_key, ':', 1) = split_part(b.resource_key, ':', 1)
   and split_part(a.resource_key, ':', 2) = split_part(b.resource_key, ':', 2)
   and a.valid_during && b.valid_during
 where a.tenant_id = $2
 order by a.resource_key, b.resource_key, a.id, b.id
"""

SELECT_DECISION_FOR_MISSION = """
select id::text as decision_id, input_hash, reason_codes, deciding_actor, config_release_sha,
       eligible_candidates_json::text as eligible_json,
       rejected_candidates_json::text as rejected_json,
       selected_plan_id::text as selected_plan_id, created_at
  from decision_record
 where tenant_id = $1 and mission_id = $2
 order by created_at, id
"""

SELECT_EVENTS_FOR_MISSION = """
select id::text as event_id, event_type, event_seq, payload_json::text as payload_json
  from domain_event
 where tenant_id = $1 and stream_id = $2
 order by event_seq
"""

SELECT_OUTBOX_FOR_EVENTS = """
select event_id::text as event_id, published_at, attempts
  from outbox
 where tenant_id = $1 and event_id = any($2::uuid[])
 order by event_id
"""

COUNT_ROWS_FOR_MISSIONS = """
select (select count(*) from reservation where tenant_id = $1 and mission_id = any($2::uuid[])) as reservations,
       (select count(*) from mission where tenant_id = $1 and id = any($2::uuid[])) as missions,
       (select count(*) from plan_version where tenant_id = $1 and mission_id = any($2::uuid[])) as plans,
       (select count(*) from decision_record where tenant_id = $1 and mission_id = any($2::uuid[])) as decisions,
       (select count(*) from domain_event where tenant_id = $1 and stream_id = any($2::uuid[])) as events,
       (select count(*) from outbox o
          join domain_event e on e.id = o.event_id
         where o.tenant_id = $1 and e.stream_id = any($2::uuid[])) as outbox
"""

COUNT_BY_COMMITMENT_TOKEN = """
select count(*) as rows, count(distinct mission_id) as missions
  from reservation
 where tenant_id = $1 and commitment_token = $2
"""

__all__ = [
    "ADVISORY_LOCK",
    "CANCEL_RESERVATIONS",
    "COUNT_BY_COMMITMENT_TOKEN",
    "COUNT_ROWS_FOR_MISSIONS",
    "DEMOTE_TO_UNRESOLVED",
    "INSERT_DECISION_RECORD",
    "INSERT_DOMAIN_EVENT",
    "INSERT_MISSION",
    "INSERT_OUTBOX",
    "INSERT_PLAN_VERSION",
    "INSERT_RESERVATION",
    "LOCK_RESOURCE",
    "MISSING_LOCKED_RESOURCE",
    "PROMOTE_RESERVATIONS",
    "REGISTER_RESOURCE",
    "SELECT_CLAIM_HOLDERS",
    "SELECT_CLAIM_VERSION",
    "SELECT_COMMITTED_OVERLAPPING",
    "SELECT_CONFIG_RELEASE",
    "SELECT_DECISION_FOR_MISSION",
    "SELECT_EVENTS_FOR_MISSION",
    "SELECT_MISSION_BY_ORDER",
    "SELECT_MISSION_ROWS",
    "SELECT_NEXT_EVENT_SEQ",
    "SELECT_ORDER",
    "SELECT_OUTBOX_FOR_EVENTS",
    "SELECT_POLICY_VIOLATIONS",
    "SELECT_TOKEN_MISSIONS",
    "SELECT_TOKEN_RESERVATIONS",
    "SELECT_WORLD_RESOURCES",
    "UPDATE_MISSION_STATUS",
]
