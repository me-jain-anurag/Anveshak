"""Across Protocol bridge deposits via the Across API (`/api/deposit?depositTxnRef=`) — rule X-ACROSS.

An Across deposit names its depositor, `recipient`, destination chain id, output token and
amount; once a relayer fills it the record carries `fillTx` on the destination chain. The
link therefore comes from the protocol's own deposit record naming both transactions.

Checked against live responses on 2026-10-03 (a deposit Soneium → Base, status "filled"; an
unknown transaction returns HTTP 404 "DepositNotFoundException"). One transaction can hold
several deposits: the response's `pagination.maxIndex` says how many, and each further one is
fetched with `index`. A refunded deposit went back to the depositor on the origin chain and
gives no outbound link.
"""

from __future__ import annotations

from ..addresses import AddressError, normalize
from ..chain import EVM_CHAIN_IDS, Chain
from ..errors import SourceError
from ..evidence import Fetcher
from .base import CrossChainLink, native_txid, same_tx

DEFAULT_ACROSS = "https://app.across.to/api"
# Across chain ids for non-EVM chains, from the Across API `/api/swap/chains` (checked 2026-10-03).
ACROSS_NON_EVM = {34268394551451: Chain.SOLANA, 728126428: Chain.TRON}

ACROSS_CHAINS: dict[int, Chain] = {cid: chain for chain, cid in EVM_CHAIN_IDS.items()} | ACROSS_NON_EVM
ACROSS_IDS: dict[Chain, int] = {chain: cid for cid, chain in ACROSS_CHAINS.items()}


class AcrossResolver:
    name = "across"
    chains = frozenset(EVM_CHAIN_IDS) | {Chain.TRON}  # origin chains whose deposit transactions are looked up

    def __init__(self, fetcher: Fetcher, base_url: str = DEFAULT_ACROSS, max_deposits: int = 20):
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")
        self.max_deposits = max_deposits

    def _deposit(self, tx_hash: str, index: int):
        params = {"depositTxnRef": tx_hash} | ({"index": index} if index else {})
        return self.fetcher.get(f"{self.base_url}/deposit", params=params, accept=(404,))

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]:
        links: list[CrossChainLink] = []
        index, max_index = 0, 0
        while index <= max_index and index < self.max_deposits:
            fetched = self._deposit(tx_hash, index)
            if fetched.status == 404:
                return links
            data = fetched.data
            if not isinstance(data, dict) or not isinstance(data.get("deposit"), dict):
                raise SourceError("Across API returned an unexpected payload")
            max_index = int((data.get("pagination") or {}).get("maxIndex") or 0)
            index += 1
            d = data["deposit"]
            if not same_tx(chain, str(d.get("depositTxHash") or d.get("depositTxnRef") or ""), tx_hash):
                continue
            if ACROSS_IDS.get(chain) != int(d.get("originChainId") or 0):
                continue  # same hash on another chain: not this deposit
            if str(d.get("status")) == "refunded":
                continue
            dest_id = int(d.get("destinationChainId") or 0)
            to_chain = ACROSS_CHAINS.get(dest_id)
            raw_recipient = str(d.get("recipient") or "")
            try:
                to_address = normalize(to_chain, raw_recipient) if to_chain else raw_recipient
            except AddressError:
                continue
            fill = str(d.get("fillTx") or d.get("fillTxnRef") or "")
            links.append(
                CrossChainLink(
                    protocol="across",
                    rule="X-ACROSS",
                    from_chain=chain,
                    from_tx=tx_hash,
                    from_address=from_address,
                    endpoint_id=endpoint_id,
                    to_chain=to_chain,
                    to_chain_code=to_chain.value if to_chain else f"evm-{dest_id}",
                    to_address=to_address,
                    to_tx=native_txid(to_chain, fill) if fill else None,
                    asset_in=f"{d.get('inputToken', '')}",
                    asset_out=f"{d.get('outputToken', '')}",
                    amount_out=str(d.get("outputAmount") or ""),
                    memo=f"depositId {d.get('depositId', '')}",
                    status=str(d.get("status") or ""),
                    evidence_id=fetched.evidence_id,
                    recipient_basis="Across deposit record: recipient"
                    + ("; the deposit also requested follow-on actions to " + str(d["actionsTargetRecipient"]) if d.get("actionsTargetRecipient") else ""),
                )
            )
        return links
