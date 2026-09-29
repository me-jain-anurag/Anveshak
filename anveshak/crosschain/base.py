"""Cross-chain links (ADR-0014).

A link says: transaction `from_tx` on `from_chain` was an input to a cross-chain swap or
bridge transfer whose output went to `to_address` on `to_chain` in `to_tx`. Links come only
from *deterministic identifier matching* — the protocol's own record that names both
transactions — never from amount/time guessing. Each link keeps the evidence id of the
protocol response, and whether the outbound transaction was then confirmed on the
destination chain.
"""

from __future__ import annotations

from typing import Protocol

from ..chain import Chain
from ..domain import Frozen


class CrossChainLink(Frozen):
    protocol: str
    rule: str
    from_chain: Chain
    from_tx: str
    from_address: str
    endpoint_id: str
    to_chain: Chain | None  # None: the destination chain is not supported by this system
    to_chain_code: str
    to_address: str
    to_tx: str | None  # None: outbound not yet sent (swap pending) — funds in flight
    asset_in: str
    asset_out: str
    amount_out: str  # as reported by the protocol, in its own units
    memo: str
    status: str
    evidence_id: str
    destination_confirmed: bool | None = None  # outbound tx found paying to_address on to_chain
    destination_detail: str | None = None

    @property
    def id(self) -> str:
        return f"{self.protocol}:{self.from_chain}:{self.from_tx}:{self.to_chain_code}:{self.to_tx or 'pending'}:{self.to_address}"


class CrossChainResolver(Protocol):
    name: str

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]: ...
