"""Where restriction data may come from. There is exactly one place, and it is a file.

``technical-build/01`` section 16 describes a DigitalSky/UTM adapter that would use
authorised credentials, cache with expiry, record source/fetch time/version and fail
conservatively when stale. Phase 0 has none of the prerequisites: no agreement, no
credentials, no licence to store or redistribute, and no verified endpoint. So this module
implements the honest subset and nothing more:

* :class:`LocalFileRestrictionSource` reads one path an operator already has rights to.
* :class:`NoAuthorisedFeedConfigured` is the default, and it says so.

There is deliberately no URL scheme, no client, no endpoint constant and no "would-be"
request builder anywhere in this package. Inventing a DigitalSky route to look complete
would be worse than admitting the gap: it would read as integration knowledge we do not
have, and a wrong endpoint that returns something plausible is exactly how a false "clear"
gets manufactured.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import NoAuthorisedFeedError

NO_FEED_MESSAGE = (
    "No authorised DigitalSky/UTM restriction feed is configured for this deployment. "
    "A future adapter may fetch one only under authorised credentials, with caching, "
    "expiry, source and version recording; nothing here reaches the network, and no "
    "endpoint is assumed or invented in the meantime."
)


class RawRestrictionDocument(Protocol):
    """A parsed, untrusted restriction document: bytes plus what the file claims."""

    @property
    def name(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def raw_bytes(self) -> bytes: ...


@dataclass(frozen=True)
class FileRestrictionDocument:
    """The content of one local file, with the path kept as part of the provenance."""

    path: Path
    raw_bytes: bytes

    @property
    def name(self) -> str:
        return str(self.path)

    @property
    def text(self) -> str:
        return self.raw_bytes.decode("utf-8")


class RestrictionSource(Protocol):
    """The narrow interface :func:`import_snapshot` consumes."""

    @property
    def availability_label(self) -> str:
        """``manual_operator_file`` or ``authoritative_feed``; recorded, never inferred."""

    def read(self) -> FileRestrictionDocument:
        """Return the document. Must not block on a network or a wall clock."""


@dataclass(frozen=True)
class LocalFileRestrictionSource:
    """A restriction snapshot file supplied by an operator who has rights to it."""

    path: Path
    availability_label: str = "manual_operator_file"

    def read(self) -> FileRestrictionDocument:
        if not self.path.is_file():
            raise NoAuthorisedFeedError(f"restriction file {self.path} does not exist")
        return FileRestrictionDocument(path=self.path, raw_bytes=self.path.read_bytes())


@dataclass(frozen=True)
class NoAuthorisedFeedConfigured:
    """The default source. Reading it is an error, not an empty answer."""

    availability_label: str = "no_feed_configured"

    def read(self) -> FileRestrictionDocument:
        raise NoAuthorisedFeedError(NO_FEED_MESSAGE)


def default_source() -> RestrictionSource:
    """What a deployment gets when nobody has configured anything: the honest nothing."""

    return NoAuthorisedFeedConfigured()
