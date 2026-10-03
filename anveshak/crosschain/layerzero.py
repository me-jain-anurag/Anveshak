"""LayerZero messages via LayerZero Scan (`/v1/messages/tx/{hash}`) — rule X-LAYERZERO.

LayerZero Scan indexes each message by its source transaction and records the destination
transaction once delivered (`destination.tx.txHash`), the pathway (source/destination
endpoint ids and chain names) and the sending/receiving application contracts (OApps).

The message payload is application-specific bytes, so this resolver does **not** decode it.
The recipient is taken from the destination transaction itself: the unique receiver of
tokens there, excluding the zero address (OFT mints) and the receiving OApp contract
(`recipient_from_destination`, run by the engine). If that is ambiguous the link is kept
with the recipient unresolved.

Checked against live responses on 2026-10-03 (e.g. a USDT0 OFT message Arbitrum → Celo;
an unknown transaction returns HTTP 404 "Message not found"). Chain names are LayerZero
Scan's `pathway.*.chain` values observed in the live message feed on that date.
"""

from __future__ import annotations

from ..addresses import AddressError, normalize
from ..chain import Chain
from ..errors import SourceError
from ..evidence import Fetcher
from .base import CrossChainLink, native_txid, same_tx

DEFAULT_LZ_SCAN = "https://scan.layerzero-api.com"

LZ_CHAINS = {
    "ethereum": Chain.ETHEREUM,
    "bsc": Chain.BSC,
    "polygon": Chain.POLYGON,
    "arbitrum": Chain.ARBITRUM,
    "optimism": Chain.OPTIMISM,
    "base": Chain.BASE,
    "avalanche": Chain.AVALANCHE,
    "tron": Chain.TRON,
    "solana": Chain.SOLANA,
}


class LayerZeroResolver:
    name = "layerzero"
    chains = frozenset(LZ_CHAINS.values())

    def __init__(self, fetcher: Fetcher, base_url: str = DEFAULT_LZ_SCAN):
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]:
        fetched = self.fetcher.get(f"{self.base_url}/v1/messages/tx/{tx_hash}", accept=(404,))
        if fetched.status == 404:
            return []
        data = fetched.data
        if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
            raise SourceError("LayerZero Scan returned an unexpected payload")
        links: list[CrossChainLink] = []
        for message in data.get("data") or []:
            source_tx = str(((message.get("source") or {}).get("tx") or {}).get("txHash") or "")
            if not source_tx or not same_tx(chain, source_tx, tx_hash):
                continue
            pathway = message.get("pathway") or {}
            receiver = pathway.get("receiver") or {}
            sender = pathway.get("sender") or {}
            code = str(receiver.get("chain") or pathway.get("dstEid") or "")
            to_chain = LZ_CHAINS.get(code)
            exclude: list[str] = []
            if to_chain and receiver.get("address"):
                try:
                    exclude.append(normalize(to_chain, str(receiver["address"])))
                except AddressError:
                    pass
            out_tx = str(((message.get("destination") or {}).get("tx") or {}).get("txHash") or "")
            app = str(receiver.get("name") or receiver.get("id") or sender.get("name") or "")
            links.append(
                CrossChainLink(
                    protocol="layerzero",
                    rule="X-LAYERZERO",
                    from_chain=chain,
                    from_tx=tx_hash,
                    from_address=from_address,
                    endpoint_id=endpoint_id,
                    to_chain=to_chain,
                    to_chain_code=code or "unknown",
                    to_address="",
                    to_tx=native_txid(to_chain, out_tx) if out_tx else None,
                    asset_in=app,
                    asset_out=app,
                    amount_out="",
                    memo=str(message.get("guid") or ""),
                    status=str((message.get("status") or {}).get("name") or ""),
                    evidence_id=fetched.evidence_id,
                    recipient_exclude=tuple(exclude),
                )
            )
        return links
