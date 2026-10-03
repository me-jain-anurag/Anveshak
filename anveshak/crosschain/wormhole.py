"""Wormhole transfers via Wormholescan (`/api/v1/operations?txHash=`) — rule X-WORMHOLE.

A Wormhole operation is the guardian-signed message (VAA) emitted by the source transaction
plus, once redeemed, the target-chain transaction. Wormholescan indexes both and decodes
token-bridge payloads into `standarizedProperties` (sic): `toChain` (Wormhole chain id) and
`toAddress`. The link therefore comes from the protocol's own record naming both
transactions; the recipient is the decoded `toAddress` of the signed message.

Checked against live responses on 2026-10-03 (e.g. a Portal token-bridge transfer from
Solana to BNB Smart Chain with `targetChain.transaction.txHash`). Operations without a
decoded destination (generic messages, governance) are not value transfers and are skipped.
An unknown transaction returns `{"operations": []}`.

Chain ids: wormhole-foundation/wormhole sdk/vaa/structs.go (ChainID constants).
"""

from __future__ import annotations

from ..addresses import AddressError, normalize
from ..chain import Chain
from ..errors import SourceError
from ..evidence import Fetcher
from .base import CrossChainLink, native_txid, same_tx

DEFAULT_WORMHOLESCAN = "https://api.wormholescan.io"

WORMHOLE_CHAINS = {
    1: Chain.SOLANA,
    2: Chain.ETHEREUM,
    4: Chain.BSC,
    5: Chain.POLYGON,
    6: Chain.AVALANCHE,
    23: Chain.ARBITRUM,
    24: Chain.OPTIMISM,
    30: Chain.BASE,
}
# Names for destination chains this system cannot trace (shown, never followed), from the ChainID
# constants in sdk/vaa/structs.go on the main branch (checked 2026-10-04).
WORMHOLE_OTHER = {
    8: "algorand", 13: "klaytn", 14: "celo", 15: "near", 16: "moonbeam", 18: "terra2", 19: "injective", 20: "osmosis",
    21: "sui", 22: "aptos", 25: "gnosis", 26: "pythnet", 29: "btc", 31: "filecoin", 32: "sei", 33: "rootstock", 38: "linea",
    39: "berachain", 40: "seievm",
}


def chain_code(wormhole_id: int) -> str:
    chain = WORMHOLE_CHAINS.get(wormhole_id)
    return chain.value if chain else WORMHOLE_OTHER.get(wormhole_id, f"wormhole-chain-{wormhole_id}")


class WormholeResolver:
    name = "wormhole"
    chains = frozenset(WORMHOLE_CHAINS.values())

    def __init__(self, fetcher: Fetcher, base_url: str = DEFAULT_WORMHOLESCAN):
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]:
        fetched = self.fetcher.get(f"{self.base_url}/api/v1/operations", params={"txHash": tx_hash}, accept=(404,))
        if fetched.status == 404:
            return []
        data = fetched.data
        if not isinstance(data, dict) or not isinstance(data.get("operations", []), list):
            raise SourceError("Wormholescan returned an unexpected payload")
        links: list[CrossChainLink] = []
        for op in data.get("operations") or []:
            source = op.get("sourceChain") or {}
            src_tx = str((source.get("transaction") or {}).get("txHash") or "")
            if not src_tx or not same_tx(chain, src_tx, tx_hash):
                continue
            props = (op.get("content") or {}).get("standarizedProperties") or {}
            to_id = int(props.get("toChain") or 0)
            raw_to = str(props.get("toAddress") or "")
            if not to_id or not raw_to:
                continue  # not a decoded value transfer
            to_chain = WORMHOLE_CHAINS.get(to_id)
            try:
                to_address = normalize(to_chain, raw_to) if to_chain else raw_to
            except AddressError:
                continue  # address not valid for the stated chain: not a usable link
            target = op.get("targetChain") or {}
            out_tx = str((target.get("transaction") or {}).get("txHash") or "")
            token = f"{props.get('tokenAddress', '')}@{chain_code(int(props.get('tokenChain') or 0))}" if props.get("tokenAddress") else ""
            data_block = op.get("data") or {}
            amount = (
                f"{data_block['tokenAmount']} {data_block.get('symbol', '')}".strip()
                if data_block.get("tokenAmount")
                else str(props.get("amount") or "")
            )
            links.append(
                CrossChainLink(
                    protocol="wormhole",
                    rule="X-WORMHOLE",
                    from_chain=chain,
                    from_tx=tx_hash,
                    from_address=from_address,
                    endpoint_id=endpoint_id,
                    to_chain=to_chain,
                    to_chain_code=chain_code(to_id),
                    to_address=to_address,
                    to_tx=native_txid(to_chain, out_tx) if out_tx else None,
                    asset_in=token,
                    asset_out=token,
                    amount_out=amount,
                    memo=",".join(props.get("appIds") or []),
                    status=str(target.get("status") or source.get("status") or ""),
                    evidence_id=fetched.evidence_id,
                    recipient_basis="Wormholescan standarizedProperties.toAddress (decoded from the signed VAA)",
                )
            )
        return links
