"""Adapter errors. Every failure mode here is a *refusal to invent data*.

The rule this module exists to enforce: an unreachable network, an implausible
endpoint or a missing local extract ends in an exception with a readable message,
never in an empty geometry silently becoming a "clean" area on the map.
"""

from __future__ import annotations


class GisEtlError(Exception):
    """Base class for gis-etl failures."""


class ConfigError(GisEtlError):
    """The versioned config is missing, unreadable or incomplete."""


class QueryError(GisEtlError):
    """A query definition is malformed."""


class CacheError(GisEtlError):
    """The raw cache could not be written or read."""


class EndpointUnavailableError(GisEtlError):
    """No Overpass endpoint answered. Nothing was fabricated in its place.

    Attributes:
        attempts: per-endpoint attempt log, so the operator can show *why* it failed.
    """

    def __init__(self, message: str, attempts: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


class CacheMissError(GisEtlError):
    """A cached response was requested for a query that was never retrieved."""


class ImplausibleEndpointError(GisEtlError):
    """An endpoint answered 2xx with a body that cannot be trusted.

    A silent zero from a broken mirror is more dangerous than a timeout: it looks
    like a measurement. See the guard in ``configs/gis/chandigarh-synthetic-v1.yaml``.
    """


class ExtractionLimitError(GisEtlError):
    """A layer exceeded its configured feature cap and truncating it was disallowed."""


class SyntheticProvenanceError(GisEtlError):
    """A generated obstacle is missing one of the mandatory synthetic flags."""
