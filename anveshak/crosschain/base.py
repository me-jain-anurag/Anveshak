"""Cross-chain links (ADR-0014, ADR-0022).

A link says: transaction `from_tx` on `from_chain` was an input to a cross-chain swap or
bridge transfer whose output went to `to_address` on `to_chain` in `to_tx`. Links come only
from *deterministic identifier matching* — the protocol's own record that names both
transactions — never from amount/time guessing. Each link keeps the evidence id of the
protocol response, says how the recipient was determined (`recipient_basis`), and whether
the outbound transaction was then confirmed on the destination chain.

When the protocol record names the destination transaction but not the recipient (LayerZero
messages are app-specific bytes), the recipient is left empty and resolved from the
destination transaction itself (`recipient_from_destination`): it must be the *unique*
receiver of tokens in that transaction once mint/burn and protocol contracts are excluded,
otherwise the link stays "recipient unresolved".
"""

from __future__ import annotations

from typing import Callable, Protocol

from pydantic import computed_field, model_validator

from ..chain import Chain
from ..domain import Frozen, Transfer, drop_derived_id

ZERO_EVM = "0x" + "0" * 40


class CrossChainLink(Frozen):
    protocol: str
    rule: str
    from_chain: Chain
    from_tx: str
    from_address: str
    endpoint_id: str
    to_chain: Chain | None  # None: the destination chain is not supported by this system
    to_chain_code: str
    to_address: str  # "" while the recipient is unresolved
    to_tx: str | None  # None: outbound not yet sent (swap pending / bridge in flight)
    asset_in: str
    asset_out: str
    amount_out: str  # as reported by the protocol, in its own units
    memo: str
    status: str
    evidence_id: str
    recipient_basis: str = ""  # how to_address was determined (protocol field, or destination tx)
    recipient_exclude: tuple[str, ...] = ()  # protocol contracts that are never the recipient
    destination_confirmed: bool | None = None  # outbound tx found paying to_address on to_chain
    destination_detail: str | None = None

    _drop_id = model_validator(mode="before")(classmethod(lambda cls, data: drop_derived_id(data)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"{self.protocol}:{self.from_chain}:{self.from_tx}:{self.to_chain_code}:{self.to_tx or 'pending'}:{self.to_address or 'unresolved'}"


class CrossChainResolver(Protocol):
    name: str
    chains: frozenset[Chain]  # source chains this resolver can look up

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]: ...


def native_txid(chain: Chain | None, txid: str) -> str:
    t = txid.strip()
    if chain is None:
        return t
    if chain.family.value == "evm":
        t = t.lower()
        return t if t.startswith("0x") else "0x" + t
    if chain.family.value in ("tron", "utxo"):
        return t.lower().removeprefix("0x")
    return t  # Solana signatures are base58, case-sensitive


def same_tx(chain: Chain, a: str, b: str) -> bool:
    return native_txid(chain, a) == native_txid(chain, b)


def recipient_from_destination(link: CrossChainLink, moved: list[Transfer]) -> tuple[str | None, str]:
    """The unique token receiver in the destination transaction, excluding the zero address
    (mints/burns) and the protocol's own contracts. Returns (address or None, explanation)."""
    excluded = {a.lower() for a in link.recipient_exclude} | {ZERO_EVM}
    receivers = sorted({t.receiver for t in moved if t.receiver.lower() not in excluded})
    if len(receivers) == 1:
        return receivers[0], "unique token receiver in the destination transaction (mint/burn and protocol contracts excluded)"
    if not receivers:
        return None, "no token transfer to a non-protocol address in the destination transaction"
    return None, f"{len(receivers)} candidate receivers in the destination transaction ({', '.join(receivers[:4])}) — not resolved"


DestinationLookup = Callable[[Chain, str], "list[Transfer] | None"]
