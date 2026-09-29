"""The interface every chain adapter implements, plus shared helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from ..chain import Chain
from ..domain import AddressHistory, Asset, Frozen, Transfer, TransferKind

# keccak256("Transfer(address,address,uint256)") — the ERC-20/TRC-20 Transfer event topic.
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"  # re-fetched from a transaction-level endpoint; every field matches
    MISMATCH = "mismatch"  # transaction-level data disagrees with the listing — the fact is not trusted
    UNVERIFIABLE = "unverifiable"  # this kind of fact cannot be re-checked with the configured source
    ERROR = "error"  # the check could not be completed (source failure)


class Verification(Frozen):
    transfer_id: str
    status: VerificationStatus
    method: str
    detail: str = ""
    block_number: int | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Balance:
    amount: int
    evidence_id: str
    as_of: str  # block tag / timestamp description from the source


class ChainSource(ABC):
    chain: Chain

    @abstractmethod
    def history(self, address: str) -> AddressHistory:
        """All value transfers in which `address` is sender or receiver (up to the adapter's cap)."""

    @abstractmethod
    def verify(self, transfer: Transfer) -> Verification:
        """Re-check one transfer against a transaction-level endpoint."""

    def is_contract(self, address: str) -> bool | None:
        """True/False if known, None if this source cannot tell."""
        return None

    def balance(self, address: str, asset: Asset) -> Balance | None:
        """Current balance of `asset`, or None if unsupported."""
        return None


@dataclass
class _Pending:
    tx_hash: str
    kind: TransferKind
    sender: str
    receiver: str
    asset: Asset
    amount: int
    timestamp: object
    evidence_id: str
    block_number: int | None = None
    position: int | None = None
    extra: dict = field(default_factory=dict)


def assign_occurrence_positions(pending: list[_Pending]) -> list[Transfer]:
    """Account chains: number identical (tx, kind, asset, sender, receiver, amount) tuples
    0, 1, 2 ... in listing order. See `Transfer.position`."""
    counts: Counter = Counter()
    out = []
    for p in pending:
        key = (p.tx_hash, p.kind, p.asset.key, p.sender, p.receiver, p.amount)
        position = counts[key] if p.position is None else p.position
        counts[key] += 1
        out.append(
            Transfer(
                chain=p.asset.chain,
                tx_hash=p.tx_hash,
                kind=p.kind,
                position=position,
                sender=p.sender,
                receiver=p.receiver,
                asset=p.asset,
                amount=p.amount,
                block_number=p.block_number,
                timestamp=p.timestamp,
                evidence_id=p.evidence_id,
                **p.extra,
            )
        )
    return out


def dedupe_sorted(transfers: list[Transfer]) -> tuple[Transfer, ...]:
    by_id: dict[str, Transfer] = {}
    for t in transfers:
        by_id.setdefault(t.id, t)
    return tuple(sorted(by_id.values(), key=lambda t: t.order_key))


def mismatch_detail(pairs: list[tuple[str, object, object]]) -> str:
    return "; ".join(f"{name}: listed {listed!r}, transaction shows {actual!r}" for name, listed, actual in pairs if listed != actual)


class CachingSource(ChainSource):
    """Per-case cache of address histories, so an address reached twice (two subjects, a
    cross-chain destination check and its continuation trace) is fetched once."""

    def __init__(self, inner: ChainSource):
        self.inner = inner
        self.chain = inner.chain
        self._histories: dict[str, AddressHistory] = {}

    def history(self, address: str) -> AddressHistory:
        if address not in self._histories:
            self._histories[address] = self.inner.history(address)
        return self._histories[address]

    def verify(self, transfer: Transfer) -> Verification:
        return self.inner.verify(transfer)

    def is_contract(self, address: str) -> bool | None:
        return self.inner.is_contract(address)

    def balance(self, address: str, asset: Asset) -> Balance | None:
        return self.inner.balance(address, asset)
