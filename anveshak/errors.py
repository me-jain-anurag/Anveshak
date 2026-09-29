class AnveshakError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(AnveshakError):
    """Missing or invalid configuration (e.g. an API key needed for a chain)."""


class SourceError(AnveshakError):
    """A data source failed or returned something we could not interpret.

    Never swallowed silently: callers either surface it as a coverage gap in the case
    result or abort.
    """


class EvidenceMissing(SourceError):
    """Replay mode asked for a request that was never recorded."""


class EvidenceCorrupted(SourceError):
    """A stored evidence object no longer matches its sha256 identifier."""
