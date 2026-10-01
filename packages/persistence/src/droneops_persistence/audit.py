"""Deterministic canonicalisation for the audit chain.

The chain only proves anything if an independent verifier, in another language, can
reproduce ``record_hash`` from the stored row. That rules out: Python dict order, locale,
``repr``, ``str(datetime)`` (which is precision-dependent), and jsonb round-tripping
(Postgres reorders object keys and normalises numbers). So the rule is written once, here,
and the audit table comment repeats it.

Canonical form (see also the ``audit_log`` table comment):

1. build a dict of the hashed fields, dropping ``id``, ``prev_hash`` and ``record_hash``;
2. render every value as a JSON scalar: ``None`` -> ``null``, datetimes -> ISO-8601 UTC
   with microseconds and a literal ``Z``, text stays text, integers stay integers;
3. serialise with sorted keys, no insignificant whitespace, non-ASCII kept as UTF-8 and
   no escaping that would change bytes on disk;
4. ``record_hash = sha256(prev_hash + "\\n" + canonical_json)`` in lowercase hex.

Daily head-hash anchors go to immutable object storage (spec section 4); ``head_hash_for``
is the value that is anchored.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

GENESIS_HASH = "0" * 64
EXCLUDED_FIELDS: frozenset[str] = frozenset({"id", "prev_hash", "record_hash"})


def _scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware UTC")
        offset = value.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("audit timestamps must be UTC")
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}Z"
    if isinstance(value, (float,)):
        raise TypeError("float is not allowed in a canonical audit payload")
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(fields: Mapping[str, object]) -> str:
    """The one canonical serialisation used by the chain.

    ``sort_keys`` is the deterministic ordering, ``separators`` removes every space that
    an implementation could otherwise add or drop, and ``ensure_ascii=False`` keeps
    non-ASCII bytes identical to the input instead of inventing ``\\uXXXX`` forms that a
    verifier in another language may or may not produce.
    """

    payload = {
        key: _scalar(value)
        for key, value in sorted(fields.items())
        if key not in EXCLUDED_FIELDS
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def chain_hash(prev_hash: str, fields: Mapping[str, object]) -> str:
    canonical = canonical_json(fields)
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(b"\n")
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def head_hash_for(record_hashes: Sequence[str]) -> str:
    """Daily anchor: an ordered, newline-joined digest of the day's record hashes."""

    digest = hashlib.sha256()
    for record_hash in record_hashes:
        digest.update(record_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class ChainLink:
    ordinal: int
    tenant_id: uuid.UUID
    prev_hash: str
    record_hash: str
    fields: Mapping[str, object]


def verify_chain(links: Iterable[ChainLink]) -> list[str]:
    """Return one description per broken link. Empty means the chain is intact.

    A verifier that only checks "each ``record_hash`` matches its own payload" would not
    notice a deleted record, so both the recomputation and the ``prev_hash`` continuity are
    checked, per tenant, in ordinal order.
    """

    problems: list[str] = []
    by_tenant: dict[uuid.UUID, list[ChainLink]] = {}
    for link in links:
        by_tenant.setdefault(link.tenant_id, []).append(link)

    for tenant_id, chain in sorted(by_tenant.items(), key=lambda item: str(item[0])):
        ordered = sorted(chain, key=lambda link: link.ordinal)
        expected_prev = GENESIS_HASH
        for position, link in enumerate(ordered, start=1):
            if link.ordinal != position:
                problems.append(
                    f"tenant {tenant_id}: ordinal gap at {link.ordinal}"
                )
            if link.prev_hash != expected_prev:
                problems.append(
                    f"tenant {tenant_id}: record {link.ordinal} prev_hash {link.prev_hash} "
                    f"does not chain to {expected_prev}"
                )
            recomputed = chain_hash(link.prev_hash, link.fields)
            if recomputed != link.record_hash:
                problems.append(
                    f"tenant {tenant_id}: record {link.ordinal} payload does not "
                    f"reproduce record_hash (expected {recomputed})"
                )
            expected_prev = link.record_hash
    return problems


def as_utc(moment: datetime) -> datetime:
    """Normalise to UTC so a chain built in one session verifies in another."""

    if moment.tzinfo is None:
        raise ValueError("naive datetime cannot enter the audit chain")
    return moment.astimezone(timezone.utc)


__all__ = [
    "EXCLUDED_FIELDS",
    "GENESIS_HASH",
    "ChainLink",
    "as_utc",
    "canonical_json",
    "chain_hash",
    "head_hash_for",
    "verify_chain",
]
