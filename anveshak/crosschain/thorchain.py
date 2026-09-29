"""THORChain cross-chain swaps via Midgard (`/v2/actions?txid=`).

THORChain records every swap with its inbound transaction id(s) and outbound transaction
id(s) on the respective chains, so a lookup by the inbound txid returns the destination
address and outbound txid directly — rule X-THORCHAIN (deterministic identifier match).

Format checked against live responses from the public gateway on 2026-09-30, e.g. an
inbound `BTC.BTC` swap with memo `=:TRON.USDT:<address>:...` and an outbound leg on TRON
with its txID. Internal legs (`THOR.RUNE`, trade/secured assets containing '~') are
skipped: they never leave THORChain.
"""

from __future__ import annotations

from ..addresses import AddressError, normalize
from ..chain import Chain
from ..errors import SourceError
from ..evidence import Fetcher
from .base import CrossChainLink

DEFAULT_MIDGARD = "https://gateway.liquify.com/chain/thorchain_midgard"

# THORChain chain codes → chains this system can trace.
THOR_CHAINS = {
    "BTC": Chain.BITCOIN,
    "ETH": Chain.ETHEREUM,
    "BSC": Chain.BSC,
    "TRON": Chain.TRON,
    "BASE": Chain.BASE,
    "AVAX": Chain.AVALANCHE,
}


def _to_native_txid(chain: Chain | None, txid: str) -> str:
    t = txid.lower()
    if chain is not None and chain.family.value == "evm":
        return t if t.startswith("0x") else "0x" + t
    return t.removeprefix("0x")


class ThorchainResolver:
    name = "thorchain"

    def __init__(self, fetcher: Fetcher, base_url: str = DEFAULT_MIDGARD):
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str) -> list[CrossChainLink]:
        txid = tx_hash.removeprefix("0x").upper()
        fetched = self.fetcher.get(f"{self.base_url}/v2/actions", params={"txid": txid})
        data = fetched.data
        if not isinstance(data, dict) or not isinstance(data.get("actions", []), list):
            raise SourceError("Midgard returned an unexpected payload")
        links: list[CrossChainLink] = []
        for action in data.get("actions") or []:
            if action.get("type") != "swap":
                continue
            inbound = [i for i in action.get("in") or [] if str(i.get("txID", "")).upper() == txid]
            if not inbound:
                continue
            asset_in = ",".join(c.get("asset", "") for c in inbound[0].get("coins") or [])
            memo = ((action.get("metadata") or {}).get("swap") or {}).get("memo", "")
            for out in action.get("out") or []:
                coins = out.get("coins") or []
                asset = coins[0].get("asset", "") if coins else ""
                code = asset.split(".", 1)[0]
                if not asset or "~" in asset or code == "THOR":
                    continue  # internal leg — never leaves THORChain
                to_chain = THOR_CHAINS.get(code)
                raw_address = str(out.get("address", ""))
                try:
                    to_address = normalize(to_chain, raw_address) if to_chain else raw_address
                except AddressError:
                    continue  # address not valid for the stated chain: not a usable link
                out_tx = str(out.get("txID") or "")
                links.append(
                    CrossChainLink(
                        protocol="thorchain",
                        rule="X-THORCHAIN",
                        from_chain=chain,
                        from_tx=tx_hash,
                        from_address=from_address,
                        endpoint_id=endpoint_id,
                        to_chain=to_chain,
                        to_chain_code=code,
                        to_address=to_address,
                        to_tx=_to_native_txid(to_chain, out_tx) if out_tx else None,
                        asset_in=asset_in,
                        asset_out=asset,
                        amount_out=str(coins[0].get("amount", "")),
                        memo=str(memo),
                        status=str(action.get("status", "")),
                        evidence_id=fetched.evidence_id,
                    )
                )
        return links
