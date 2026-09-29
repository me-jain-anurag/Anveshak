"""Verified asset registry (data/assets.yaml). See ADR-0007."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .addresses import normalize
from .chain import Chain
from .domain import Asset


@dataclass(frozen=True)
class TokenInfo:
    asset: Asset
    issuer: str | None
    min_amount: int
    sources: tuple[str, ...]


class AssetRegistry:
    def __init__(self, native_min: dict[Chain, int], tokens: list[TokenInfo]):
        self._native_min = native_min
        self._tokens = {(t.asset.chain, t.asset.contract): t for t in tokens}

    @classmethod
    def load(cls, path: Path) -> AssetRegistry:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        native_min = {Chain(k): int(v["min_amount"]) for k, v in doc["native"].items()}
        tokens = []
        for entry in doc.get("tokens", []):
            chain = Chain(entry["chain"])
            contract = normalize(chain, entry["contract"])
            if not entry.get("sources"):
                raise ValueError(f"asset {chain}:{contract} has no verification source")
            asset = Asset(chain=chain, contract=contract, symbol=entry["symbol"], decimals=int(entry["decimals"]), verified=True)
            tokens.append(TokenInfo(asset, entry.get("issuer"), int(entry["min_amount"]), tuple(entry["sources"])))
        return cls(native_min, tokens)

    def native(self, chain: Chain) -> Asset:
        return Asset(chain=chain, contract=None, symbol=chain.native_symbol, decimals=chain.native_decimals, verified=True)

    def token(self, chain: Chain, contract: str, reported_symbol: str, reported_decimals: int) -> Asset:
        """Resolve a token seen on-chain. Unknown contracts come back `verified=False`,
        keeping the symbol the chain reported but marked with '?' so it can't pass for the real thing."""
        info = self._tokens.get((chain, contract))
        if info is not None:
            return info.asset
        decimals = reported_decimals if 0 <= reported_decimals <= 36 else 0
        symbol = (reported_symbol or "UNKNOWN")[:16]
        return Asset(chain=chain, contract=contract, symbol=f"{symbol}?", decimals=decimals, verified=False)

    def min_amount(self, asset: Asset) -> int:
        if asset.contract is None:
            return self._native_min.get(asset.chain, 0)
        info = self._tokens.get((asset.chain, asset.contract))
        return info.min_amount if info else 0

    def issuer(self, asset: Asset) -> str | None:
        info = self._tokens.get((asset.chain, asset.contract)) if asset.contract else None
        return info.issuer if info else None

    def tokens(self) -> list[TokenInfo]:
        return list(self._tokens.values())
