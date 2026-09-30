# airspace-import

The only channel through which airspace restriction data may enter DroneOps. Simulation only.
**Not authorised for flight**, and not a statement about real Indian airspace.

## No authorised feed is configured

That sentence is the state of this repository, not a TODO. There is **no DigitalSky or UTM
client here, and no endpoint has been invented for one**: an assumed URL would read as
integration knowledge we do not have, and a plausible-looking wrong endpoint is how a false
"clear airspace" gets manufactured. What exists instead:

| Type | Behaviour |
| --- | --- |
| `NoAuthorisedFeedConfigured` | the default; `read()` raises `NoAuthorisedFeedError` - never an empty list |
| `LocalFileRestrictionSource(path)` | reads one local file an operator already has rights to; offline only |

`import_snapshot(default_source())` returns an explicit `no_feed_configured` snapshot whose note
says the empty set is *ignorance, not clear airspace*, so `droneops_airspace` fails closed on it.

## What it records

`import_snapshot` / `import_from_file` normalise a JSON or YAML restriction document into a
`droneops_airspace.RestrictionSnapshot` and record:

* `source` - from the document;
* `fetched_at` - **this run's own observation instant**, never a timestamp copied from the file,
  so an old file cannot make a snapshot look fresh;
* `feed_version` - the declared version, or `sha256:<digest>` when the file does not carry one;
* `content_sha256` - digest of the exact bytes read, so a decision record can prove which file
  was in front of the operator;
* per-restriction `provenance` - source, retrieval time, expiry and declared freshness.

Gaps stay gaps. An entry with no declared `freshness` is imported as `unknown` (with a warning),
which the validity layer reads as stale and fails closed on. An open polygon ring is closed
automatically and the warning says so. A missing `altitude_reference` or a naive timestamp is
refused outright (I9/I10). A document cannot promote itself: its `availability` field must match
the configured source label, and no source can satisfy `authoritative_feed` while the
authoritative registry is empty.

## What it never does

* No live fetch, no credentials, no caching of third-party data.
* No decision: it does not import `droneops_reservations`, `droneops_planner` or
  `droneops_dispatcher` (AST test plus the `adapters-cannot-decide-reservations` contract).
  It writes observations; the core evaluates them.
* No evaluation, ranking or filtering of restrictions - validity is `packages/airspace`'s job.

## Document shape

See `tests/data/manual-restrictions.yaml` for a worked example: a polygon red sector, a circular
airport-vicinity buffer built on the OpenStreetMap aerodrome point from
`data/processed/chandigarh-synthetic-v1` (labelled synthetic: a buffer we drew is not an official
boundary), and a stale closure that demonstrates fail-closed ingestion.

```
uv run --no-sync pytest adapters/airspace-import -q --no-cov
```
