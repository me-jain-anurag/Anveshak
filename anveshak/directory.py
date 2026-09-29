"""VASP / issuer directory used for routing requests (data/vasp_directory.yaml).

Every fact in the directory carries the URL it was checked against and the date. Entities
without a verified contact channel are still listed (so attributions resolve), but routing
to them always requires analyst review.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from .domain import Frozen
from .evidence import canonical_json, sha256_hex


class SourcedFact(Frozen):
    value: bool | str | None
    source: str | None = None
    checked: date | None = None
    note: str | None = None


class Channel(Frozen):
    name: str
    url: str
    source: str
    checked: date


class DirectoryEntry(Frozen):
    entity_id: str
    display_name: str
    role: str  # "vasp" | "stablecoin_issuer"
    aliases: tuple[str, ...] = ()
    # Official publication channels ("binance.com", "twitter.com/okx"). A label claiming to be
    # entity-attested counts as such only if its primary source is on one of these.
    official_sources: tuple[str, ...] = ()
    jurisdiction: SourcedFact = SourcedFact(value=None)
    channels: tuple[Channel, ...] = ()
    fiu_ind_registered: SourcedFact = SourcedFact(value=None)
    sahyog_onboarded: SourcedFact = SourcedFact(value=None)
    synthetic: bool = False


class VaspDirectory:
    def __init__(self, entries: list[DirectoryEntry]):
        self._entries: dict[str, DirectoryEntry] = {}
        self._aliases: dict[str, str] = {}
        for entry in entries:
            if entry.entity_id in self._entries:
                raise ValueError(f"duplicate directory entry {entry.entity_id}")
            for channel in entry.channels:
                if not channel.source:
                    raise ValueError(f"{entry.entity_id}: channel {channel.name} has no source")
            self._entries[entry.entity_id] = entry
            for alias in entry.aliases:
                self._aliases[alias] = entry.entity_id

    @classmethod
    def load(cls, path: Path, include_synthetic: bool = False) -> VaspDirectory:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries = [DirectoryEntry.model_validate(e) for e in doc.get("entities", [])]
        if not include_synthetic:
            entries = [e for e in entries if not e.synthetic]
        return cls(entries)

    def canonical(self, entity_id: str | None) -> str | None:
        if entity_id is None:
            return None
        return self._aliases.get(entity_id, entity_id)

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def get(self, entity_id: str | None) -> DirectoryEntry | None:
        return self._entries.get(self.canonical(entity_id) or "")

    def official_sources(self) -> dict[str, tuple[str, ...]]:
        return {e.entity_id: e.official_sources for e in self._entries.values() if e.official_sources}

    def entries(self) -> list[DirectoryEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def snapshot_hash(self) -> str:
        return sha256_hex(canonical_json([e.model_dump(mode="json") for e in self.entries()]))
