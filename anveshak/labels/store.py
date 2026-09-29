"""Label store: every attribution claim the system knows, indexed by (chain, address).

Labels live as JSON Lines files (one `Label` per line). Loading is strict — a record that
fails validation (bad address checksum, missing primary source, unknown chain) aborts the
load with the file and line number, rather than being skipped.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ..addresses import normalize
from ..chain import Chain, ChainFamily
from ..domain import Label
from ..evidence import canonical_json, sha256_hex


class LabelLoadError(ValueError):
    pass


class LabelStore:
    def __init__(self, labels: list[Label] | None = None):
        self._index: dict[tuple[Chain, str], list[Label]] = defaultdict(list)
        self._all: list[Label] = []
        for label in labels or []:
            self.add(label)

    def add(self, label: Label) -> None:
        canonical = normalize(label.chain, label.address)
        if canonical != label.address:
            label = label.model_copy(update={"address": canonical})
        self._index[(label.chain, label.address)].append(label)
        self._all.append(label)

    @classmethod
    def load(cls, paths: list[Path], include_synthetic: bool = False) -> LabelStore:
        store = cls()
        for path in sorted(paths):
            for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    label = Label.model_validate_json(line)
                    normalize(label.chain, label.address)
                except Exception as exc:  # noqa: BLE001 - re-raised with location
                    raise LabelLoadError(f"{path}:{number}: {exc}") from exc
                if label.synthetic and not include_synthetic:
                    raise LabelLoadError(f"{path}:{number}: synthetic label in a live label set")
                store.add(label)
        return store

    @classmethod
    def load_dir(cls, root: Path, include_synthetic: bool = False, extra: list[Path] | None = None) -> LabelStore:
        paths = [p for p in Path(root).rglob("*.jsonl")] if Path(root).exists() else []
        return cls.load(paths + list(extra or []), include_synthetic=include_synthetic)

    def get(self, chain: Chain, address: str) -> tuple[Label, ...]:
        return tuple(self._index.get((chain, address), ()))

    def on_other_evm_chains(self, chain: Chain, address: str) -> tuple[Label, ...]:
        if chain.family is not ChainFamily.EVM:
            return ()
        out: list[Label] = []
        for other in Chain:
            if other is not chain and other.family is ChainFamily.EVM:
                out.extend(self._index.get((other, address), ()))
        return tuple(out)

    def all(self) -> tuple[Label, ...]:
        return tuple(self._all)

    def __len__(self) -> int:
        return len(self._all)

    def snapshot_hash(self) -> str:
        """Hash of the exact label set used — recorded in every case for reproducibility."""
        lines = sorted(canonical_json(label.model_dump(mode="json")) for label in self._all)
        return sha256_hex(b"\n".join(lines))

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for label in self._all:
            counts[f"{label.chain}/{label.source_class}"] += 1
        return dict(sorted(counts.items()))


def write_jsonl(path: Path, labels: list[Label]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(labels, key=lambda l: (l.chain, l.address, l.source_id, l.text))
    path.write_text("".join(json.dumps(l.model_dump(mode="json"), sort_keys=True) + "\n" for l in ordered), encoding="utf-8")
