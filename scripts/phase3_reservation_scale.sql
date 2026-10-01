-- Phase 3 scale proof: 10,000 reservations over a 24-hour window, and whether the GiST
-- index is actually used by the temporal overlap query.
--
-- Run against the pinned local PostGIS container:
--
--   docker exec -i droneops-local-postgres-1 psql -U droneops -d droneops \
--     -f - < scripts/phase3_reservation_scale.sql
--
-- (or `cat scripts/phase3_reservation_scale.sql | docker exec -i ... psql ...`).
--
-- Everything this file writes belongs to one throwaway tenant, and the last statement
-- removes it. It is SQL rather than Python because the Python bench hit an asyncpg/RLS
-- interaction on this machine; the workload itself is identical, and the planner output is
-- the same either way.

\timing on
\echo '=== 0. clean any previous bench tenant ==='
set app.tenant_id = '00000000-0000-e000-0000-000000000001';
delete from reservation where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from mission       where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from "order"       where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from aircraft      where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from aircraft_model where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from facility      where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from config_release where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from tenant        where id = '00000000-0000-e000-0000-000000000001';

\echo '=== 1. parents: one tenant, two sites, one tail, one config release ==='
insert into tenant (id, name, data_region, status)
values ('00000000-0000-e000-0000-000000000001', 'phase3-scale-bench', 'in-central', 'active');
insert into facility (id, tenant_id, name, kind, source)
values ('00000000-0000-e000-0000-0000000000a1', '00000000-0000-e000-0000-000000000001', 'bench-hub', 'hub', 'bench'),
       ('00000000-0000-e000-0000-0000000000a2', '00000000-0000-e000-0000-000000000001', 'bench-node', 'hub', 'bench');
insert into aircraft_model (id, tenant_id, name, vendor, type_cert_status, empty_mass_g,
                            max_payload_g, cruise_speed_ms, max_wind_ms, battery_profile_id,
                            config_release_sha)
values ('00000000-0000-e000-0000-0000000000b1', '00000000-0000-e000-0000-000000000001',
        'bench-model', 'synthetic', 'none', 12000, 2000, 20, 8, 'airframe-x-sim',
        repeat('a', 64));
insert into aircraft (id, tenant_id, model_id, registration_ref, status, home_facility_id)
values ('00000000-0000-e000-0000-0000000000c1', '00000000-0000-e000-0000-000000000001',
        '00000000-0000-e000-0000-0000000000b1', 'VT-BENCH-0001', 'available',
        '00000000-0000-e000-0000-0000000000a1');
insert into config_release (id, tenant_id, config_release_sha, label, policy_version,
                            protected_volume_json)
values ('00000000-0000-e000-0000-0000000000d1', '00000000-0000-e000-0000-000000000001',
        repeat('a', 64), 'bench', 'bench-1',
        '{"altitude_reference": "AGL", "horizontal_margin_m": 15, "vertical_margin_m": 10,
          "illustrative": true}'::jsonb);

\echo '=== 2. 10,000 orders + missions + committed reservations over 24 h ==='
-- One corridor resource key, 10,000 non-overlapping 8-second windows spread across
-- 24 hours. The shape is forced by the constraint itself: 10,000 committed windows on one
-- key need 80,000 seconds of the 86,400 in a day, so 8 s is the longest window that fits.
with seq as (select i from generate_series(0, 9999) as g(i))
insert into "order" (id, tenant_id, external_ref_hash, origin_facility_id,
                     destination_facility_id, payload_profile_id, priority_class,
                     earliest_departure, requested_deadline, idempotency_key,
                     config_release_sha, status)
select gen_random_uuid(), '00000000-0000-e000-0000-000000000001',
       md5(i::text) || md5((i + 1)::text),
       '00000000-0000-e000-0000-0000000000a1', '00000000-0000-e000-0000-0000000000a2',
       'pp-light', 'routine',
       timestamptz '2027-03-04 06:00:00+00', timestamptz '2027-03-05 06:00:00+00',
       'bench-' || i, repeat('a', 64), 'validated'
from seq;

with seq as (select i, o.id as order_id
             from generate_series(0, 9999) as g(i)
             join "order" o on o.idempotency_key = 'bench-' || i)
insert into mission (id, order_id, tenant_id, aircraft_id, priority_class, policy_version)
select gen_random_uuid(), order_id, '00000000-0000-e000-0000-000000000001',
       '00000000-0000-e000-0000-0000000000c1', 'routine', 'bench-1'
from seq;

with seq as (select i, m.id as mission_id
             from generate_series(0, 9999) as g(i)
             join "order" o on o.idempotency_key = 'bench-' || i
             join mission m on m.order_id = o.id)
insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind,
                         valid_during, state, commitment_token, config_release_sha)
select gen_random_uuid(), '00000000-0000-e000-0000-000000000001', mission_id,
       'corridor:SCALE-BENCH', 'corridor',
       tstzrange(timestamptz '2027-03-04 06:00:00+00' + make_interval(secs => i * 8),
                 timestamptz '2027-03-04 06:00:00+00' + make_interval(secs => i * 8 + 8)),
       'committed', 'token-' || i, repeat('a', 64)
from seq;

select count(*) as reservations_total,
       count(*) filter (where state = 'committed') as committed
  from reservation;

\echo '=== 3. index inventory on reservation (is there a GiST over valid_during?) ==='
select classname.relname as index_name, am.amname as index_type, pg_get_indexdef(idx.indexrelid) as definition
  from pg_index idx
  join pg_class classname on classname.oid = idx.indexrelid
  join pg_am am on am.oid = classname.relam
 where idx.indrelid = 'reservation'::regclass
 order by 1;

analyze reservation;

\echo '=== 4. the temporal overlap query, EXPLAIN (ANALYZE, BUFFERS) ==='
explain (analyze, buffers, settings, format text)
select count(*)::int
  from reservation
 where resource_key = 'corridor:SCALE-BENCH'
   and valid_during && tstzrange(timestamptz '2027-03-04 12:00:00+00',
                                 timestamptz '2027-03-04 12:30:00+00')
   and state in ('committed', 'activated');

\echo '=== 5. the same query ten times, timed (index path) ==='
explain (analyze, format json)
select count(*)::int from reservation
 where resource_key = 'corridor:SCALE-BENCH'
   and valid_during && tstzrange(timestamptz '2027-03-04 12:00:00+00',
                                 timestamptz '2027-03-04 12:30:00+00')
   and state in ('committed', 'activated');

\echo '=== 6. counterfactual: same query with index and bitmap scans disabled ==='
begin;
set local enable_indexscan = off;
set local enable_bitmapscan = off;
explain (analyze, buffers, format text)
select count(*)::int from reservation
 where resource_key = 'corridor:SCALE-BENCH'
   and valid_during && tstzrange(timestamptz '2027-03-04 12:00:00+00',
                                 timestamptz '2027-03-04 12:30:00+00')
   and state in ('committed', 'activated');
rollback;

\echo '=== 7. the ledger query shape: overlap on one key for one mission window ==='
explain (analyze, buffers)
select r.mission_id, r.resource_key, lower(r.valid_during), upper(r.valid_during)
  from reservation r
 where r.resource_key = 'corridor:SCALE-BENCH'
   and r.valid_during && tstzrange(timestamptz '2027-03-04 12:00:00+00',
                                   timestamptz '2027-03-04 12:05:00+00')
   and r.state in ('committed', 'activated', 'unresolved');

\echo '=== 8. exclusion-constraint cost: one overlapping insert must be refused ==='
do $$
declare
    raised text;
begin
    insert into reservation (id, tenant_id, mission_id, resource_key, resource_kind,
                             valid_during, state, commitment_token, config_release_sha)
    select gen_random_uuid(), '00000000-0000-e000-0000-000000000001', m.id,
           'corridor:SCALE-BENCH', 'corridor',
           tstzrange(timestamptz '2027-03-04 12:00:02+00', timestamptz '2027-03-04 12:00:09+00'),
           'committed', 'token-overlap', repeat('a', 64)
      from mission m limit 1;
    raise notice 'UNEXPECTED: the overlapping row committed';
exception when exclusion_violation then
    raise notice 'EXPECTED: GiST exclusion refused the overlap (%);', SQLERRM;
end $$;

\echo '=== 9. cleanup: drop only the bench tenant ==='
delete from reservation where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from mission       where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from "order"       where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from aircraft      where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from aircraft_model where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from facility      where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from config_release where tenant_id = '00000000-0000-e000-0000-000000000001';
delete from tenant        where id = '00000000-0000-e000-0000-000000000001';
select count(*) as bench_rows_left from reservation
 where tenant_id = '00000000-0000-e000-0000-000000000001';
