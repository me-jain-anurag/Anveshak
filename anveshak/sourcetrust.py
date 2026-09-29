"""Checks that a label's claimed source class matches who actually published it (ADR-0005).

Datasets assert a class for each record ("service_data", "authority_data" ...). That
assertion is not taken on trust:

  entity_attested  counts only if the primary source is on an official channel of that
                   entity (VASP directory `official_sources`), or the label is an
                   investigator record of a formal document (e.g. a VASP's Sahyog reply);
  authority        counts only if the primary source is on an allow-listed authority
                   domain (data/authorities.yaml).

Anything else is treated as `curated`. Example: a GraphSense pack marked `service_data`
whose source is a news article does not make the attribution grade A.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .domain import Label, SourceClass


def _host_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parts.path or "/"


def matches(url: str, allowed: str) -> bool:
    """`allowed` is a domain ("binance.com") or domain+path prefix ("twitter.com/okx")."""
    host, path = _host_path(url)
    a_host, _, a_path = allowed.lower().partition("/")
    if not (host == a_host or host.endswith("." + a_host)):
        return False
    if a_path:
        return path.lower().startswith("/" + a_path.rstrip("/") + "/") or path.lower() == "/" + a_path.rstrip("/")
    return True


class SourceTrust:
    def __init__(self, official_sources: dict[str, tuple[str, ...]], authority_domains: tuple[str, ...]):
        self.official = official_sources
        self.authority_domains = authority_domains

    @classmethod
    def load(cls, authorities_path: Path, official_sources: dict[str, tuple[str, ...]]) -> SourceTrust:
        doc = yaml.safe_load(Path(authorities_path).read_text(encoding="utf-8"))
        return cls(official_sources, tuple(d["domain"] for d in doc["domains"]))

    def effective(self, label: Label, canonical_entity: str | None) -> tuple[SourceClass, str | None]:
        """Return the class the label actually earns, and a note if it was lowered."""
        claimed = label.source_class
        if label.source_id == "investigator":
            return claimed, None  # formal document recorded by an officer, with audit trail
        if claimed is SourceClass.ENTITY_ATTESTED:
            allowed = self.official.get(canonical_entity or "", ())
            if any(matches(label.primary_source, a) for a in allowed):
                return claimed, None
            host = _host_path(label.primary_source)[0] or label.primary_source
            return SourceClass.CURATED, (
                f"{label.source_id} marks this as entity-attested, but its primary source ({host}) is not a recorded "
                f"official channel of {canonical_entity or 'the entity'} — treated as curated"
            )
        if claimed is SourceClass.AUTHORITY:
            if any(matches(label.primary_source, d) for d in self.authority_domains):
                return claimed, None
            host = _host_path(label.primary_source)[0] or label.primary_source
            return SourceClass.CURATED, f"{label.source_id} marks this as authority data, but {host} is not an allow-listed authority domain — treated as curated"
        return claimed, None
