"""Bitcoin via an Esplora API (default https://blockstream.info/api).

UTXO model (ADR-0008). From the perspective of address A:
  * a transaction spending an output held by A yields one transfer A -> X per output
    X != A, carrying that output's exact value;
  * a transaction paying A yields one transfer B -> A per distinct input address B != A,
    each carrying the value of the output to A (an upper bound on B's contribution).
No change-address guessing: every output is followed, so change simply appears as a hop.
Only confirmed transactions are used. Outputs without a standard address (OP_RETURN,
bare multisig, P2PK) cannot be followed and are skipped.
"""

from __future__ import annotations

from collections import Counter

from ..addresses import AddressError, normalize
from ..assets import AssetRegistry
from ..chain import Chain
from ..domain import AddressHistory, Transfer, TransferKind, UtxoContext, utc_from_timestamp
from ..errors import SourceError
from ..evidence import Fetcher
from .base import ChainSource, Verification, VerificationStatus, dedupe_sorted, mismatch_detail

PAGE = 25  # Esplora returns 25 confirmed transactions per page


def is_coinjoin_like(input_addresses: list[str], output_values: list[int]) -> bool:
    """Structural CoinJoin test (ADR-0008): at least 3 distinct input addresses and at least
    3 outputs of identical value. Deliberately broad — a false positive only stops the
    trace and flags the transaction for manual review; it never creates an attribution."""
    if len(set(input_addresses)) < 3:
        return False
    counts = Counter(output_values)
    return bool(counts) and max(counts.values()) >= 3


class EsploraSource(ChainSource):
    chain = Chain.BITCOIN

    def __init__(
        self,
        fetcher: Fetcher,
        registry: AssetRegistry,
        base_url: str = "https://blockstream.info/api",
        max_txs: int = 500,
    ):
        self.fetcher = fetcher
        self.registry = registry
        self.base_url = base_url.rstrip("/")
        self.max_txs = max_txs

    def _get(self, path: str) -> tuple[object, str]:
        fetched = self.fetcher.get(f"{self.base_url}{path}")
        return fetched.data, fetched.evidence_id

    def _addr(self, raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            return normalize(Chain.BITCOIN, raw)
        except AddressError:
            return None  # testnet/non-standard rendering — cannot be followed

    def _parse_tx(self, tx: dict) -> tuple[list[str], list[tuple[int, str | None, int]], int, int] | None:
        status = tx.get("status") or {}
        if not status.get("confirmed"):
            return None
        inputs = []
        for vin in tx.get("vin") or []:
            if vin.get("is_coinbase"):
                continue
            addr = self._addr((vin.get("prevout") or {}).get("scriptpubkey_address"))
            if addr:
                inputs.append(addr)
        outputs = [(i, self._addr(v.get("scriptpubkey_address")), int(v.get("value") or 0)) for i, v in enumerate(tx.get("vout") or [])]
        return inputs, outputs, int(status["block_height"]), int(status["block_time"])

    def history(self, address: str) -> AddressHistory:
        address = normalize(Chain.BITCOIN, address)
        stats, _ = self._get(f"/address/{address}")
        if not isinstance(stats, dict):
            raise SourceError("Esplora returned an unexpected address payload")
        tx_count = int((stats.get("chain_stats") or {}).get("tx_count") or 0)

        txs: list[tuple[dict, str]] = []
        path = f"/address/{address}/txs/chain"
        while True:
            page, evidence_id = self._get(path)
            if not isinstance(page, list):
                raise SourceError("Esplora returned an unexpected transaction list")
            txs.extend((tx, evidence_id) for tx in page)
            if len(page) < PAGE or len(txs) >= self.max_txs:
                break
            path = f"/address/{address}/txs/chain/{page[-1]['txid']}"

        native = self.registry.native(Chain.BITCOIN)
        transfers: list[Transfer] = []
        for tx, evidence_id in txs:
            parsed = self._parse_tx(tx)
            if parsed is None:
                continue
            inputs, outputs, height, block_time = parsed
            ctx = UtxoContext(
                input_addresses=tuple(sorted(set(inputs))),
                output_count=len(outputs),
                coinjoin_like=is_coinjoin_like(inputs, [v for _, _, v in outputs]),
            )
            common = dict(
                chain=Chain.BITCOIN,
                tx_hash=tx["txid"],
                kind=TransferKind.UTXO_OUTPUT,
                asset=native,
                block_number=height,
                timestamp=utc_from_timestamp(block_time),
                evidence_id=evidence_id,
                utxo=ctx,
            )
            if address in inputs:
                for index, receiver, value in outputs:
                    if receiver and receiver != address and value > 0:
                        transfers.append(Transfer(position=index, sender=address, receiver=receiver, amount=value, **common))
            for index, receiver, value in outputs:
                if receiver == address and value > 0:
                    for sender in sorted(set(inputs) - {address}):
                        transfers.append(Transfer(position=index, sender=sender, receiver=address, amount=value, **common))

        complete = len(txs) >= tx_count
        note = None if complete else f"{tx_count} confirmed transactions, newest {len(txs)} fetched"
        return AddressHistory(chain=Chain.BITCOIN, address=address, transfers=dedupe_sorted(transfers), complete=complete, note=note)

    def verify(self, transfer: Transfer) -> Verification:
        method = "esplora-tx"
        try:
            tx, evidence_id = self._get(f"/tx/{transfer.tx_hash}")
            if not isinstance(tx, dict):
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction not found")
            parsed = self._parse_tx(tx)
            if parsed is None:
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction is not confirmed", evidence_ids=(evidence_id,))
            inputs, outputs, height, block_time = parsed
            out = outputs[transfer.position] if transfer.position < len(outputs) else (None, None, None)
            problems = [
                ("block_number", transfer.block_number, height),
                ("timestamp", int(transfer.timestamp.timestamp()), block_time),
                ("sender_in_inputs", True, transfer.sender in inputs),
                ("output_address", transfer.receiver, out[1]),
                ("output_value", transfer.amount, out[2]),
            ]
            detail = mismatch_detail(problems)
            status = VerificationStatus.MISMATCH if detail else VerificationStatus.VERIFIED
            return Verification(transfer_id=transfer.id, status=status, method=method, detail=detail, block_number=height, evidence_ids=(evidence_id,))
        except SourceError as exc:
            return Verification(transfer_id=transfer.id, status=VerificationStatus.ERROR, method=method, detail=str(exc))
