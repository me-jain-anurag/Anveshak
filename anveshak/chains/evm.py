"""Ethereum / BNB Smart Chain / Polygon via the Etherscan API V2 (or any compatible endpoint).

Listing endpoints (txlist, txlistinternal, tokentx) come from Etherscan's indexer.
Verification re-reads each transfer through the JSON-RPC proxy (eth_getTransactionByHash,
eth_getTransactionReceipt, eth_getBlockByNumber) — node data, a different code path from
the indexer — and checks every field.

Free-tier note (checked 2026-09-30, https://docs.etherscan.io/supported-chains): Ethereum
and Polygon are on the free tier; BNB Smart Chain requires a paid plan or another
Etherscan-compatible provider (set ETHERSCAN_BASE_URL).
"""

from __future__ import annotations

import time

from ..addresses import AddressError, normalize
from ..assets import AssetRegistry
from ..chain import EVM_CHAIN_IDS, Chain, ChainFamily
from ..domain import AddressHistory, Asset, Transfer, TransferKind, utc_from_timestamp
from ..errors import ConfigError, SourceError
from ..evidence import Fetcher
from .base import (
    TRANSFER_TOPIC,
    Balance,
    ChainSource,
    Verification,
    VerificationStatus,
    _Pending,
    assign_occurrence_positions,
    dedupe_sorted,
    mismatch_detail,
)


class EtherscanSource(ChainSource):
    def __init__(
        self,
        chain: Chain,
        fetcher: Fetcher,
        registry: AssetRegistry,
        api_key: str,
        base_url: str = "https://api.etherscan.io/v2/api",
        max_records_per_list: int = 5000,
        page_size: int = 1000,
        require_key: bool = True,
    ):
        if chain.family is not ChainFamily.EVM:
            raise ValueError(f"{chain} is not an EVM chain")
        if require_key and not api_key:
            raise ConfigError(f"ETHERSCAN_API_KEY is required to query {chain}")
        if max_records_per_list > 10000:
            raise ValueError("Etherscan caps page*offset at 10000")
        self.chain = chain
        self.fetcher = fetcher
        self.registry = registry
        self.api_key = api_key
        self.base_url = base_url
        self.max_records = max_records_per_list
        self.page_size = page_size
        self._chainid = str(EVM_CHAIN_IDS[chain])
        self._contract_cache: dict[str, bool] = {}
        self._block_time_cache: dict[int, tuple[int, str]] = {}

    # ------------------------------------------------------------------ transport

    def _call(self, params: dict) -> tuple[object, str]:
        full = {"chainid": self._chainid, **params, "apikey": self.api_key}
        for attempt in range(5):
            fetched = self.fetcher.get(self.base_url, params=full)
            data = fetched.data
            if not isinstance(data, dict):
                raise SourceError("Etherscan returned an unexpected payload")
            if "jsonrpc" in data:
                if data.get("error"):
                    raise SourceError(f"Etherscan proxy error: {data['error']}")
                return data.get("result"), fetched.evidence_id
            status, message, result = data.get("status"), data.get("message", ""), data.get("result")
            if status == "1":
                return result, fetched.evidence_id
            if status == "0" and result == [] and "no " in str(message).lower():
                return [], fetched.evidence_id  # "No transactions found" / "No records found"
            if isinstance(result, str) and "rate limit" in result.lower():
                time.sleep(1.0 + attempt)
                continue
            raise SourceError(f"Etherscan error ({params.get('action')}): {message}: {result}")
        raise SourceError("Etherscan rate limit persisted after retries")

    def _address(self, raw: str) -> str:
        try:
            return normalize(self.chain, raw)
        except AddressError as exc:
            raise SourceError(f"malformed address {raw!r} in Etherscan data") from exc

    # ------------------------------------------------------------------ listing

    def _list(self, action: str, address: str) -> tuple[list[tuple[dict, str]], bool]:
        rows: list[tuple[dict, str]] = []
        page = 1
        while True:
            result, evidence_id = self._call(
                {
                    "module": "account",
                    "action": action,
                    "address": address,
                    "startblock": "0",
                    "endblock": "999999999",
                    "page": str(page),
                    "offset": str(self.page_size),
                    "sort": "asc",
                }
            )
            if not isinstance(result, list):
                raise SourceError(f"Etherscan {action} returned a non-list result")
            rows.extend((r, evidence_id) for r in result)
            if len(result) < self.page_size:
                return rows, True
            if len(rows) >= self.max_records:
                return rows, False
            page += 1

    def history(self, address: str) -> AddressHistory:
        address = normalize(self.chain, address)
        pending: list[_Pending] = []
        complete = True
        native = self.registry.native(self.chain)

        for action, kind in (("txlist", TransferKind.NATIVE), ("txlistinternal", TransferKind.INTERNAL)):
            rows, done = self._list(action, address)
            complete &= done
            for row, evidence_id in rows:
                if row.get("isError") == "1" or row.get("txreceipt_status") == "0":
                    continue  # failed transactions move no value
                value = int(row.get("value") or 0)
                if value == 0 or not row.get("to"):
                    continue  # pure contract call, or contract creation
                pending.append(
                    _Pending(
                        tx_hash=row["hash"].lower(),
                        kind=kind,
                        sender=self._address(row["from"]),
                        receiver=self._address(row["to"]),
                        asset=native,
                        amount=value,
                        timestamp=utc_from_timestamp(int(row["timeStamp"])),
                        block_number=int(row["blockNumber"]),
                        evidence_id=evidence_id,
                    )
                )

        rows, done = self._list("tokentx", address)
        complete &= done
        for row, evidence_id in rows:
            value = int(row.get("value") or 0)
            if value == 0:
                continue
            contract = self._address(row["contractAddress"])
            try:
                decimals = int(row.get("tokenDecimal") or 0)
            except ValueError:
                decimals = 0
            pending.append(
                _Pending(
                    tx_hash=row["hash"].lower(),
                    kind=TransferKind.TOKEN,
                    sender=self._address(row["from"]),
                    receiver=self._address(row["to"]),
                    asset=self.registry.token(self.chain, contract, row.get("tokenSymbol", ""), decimals),
                    amount=value,
                    timestamp=utc_from_timestamp(int(row["timeStamp"])),
                    block_number=int(row["blockNumber"]),
                    evidence_id=evidence_id,
                )
            )

        transfers = [t for t in assign_occurrence_positions(pending) if address in (t.sender, t.receiver)]
        note = None if complete else f"history capped at {self.max_records} records per list"
        return AddressHistory(chain=self.chain, address=address, transfers=dedupe_sorted(transfers), complete=complete, note=note)

    # ------------------------------------------------------------------ verification

    def _block_timestamp(self, block_number: int) -> tuple[int, str]:
        if block_number not in self._block_time_cache:
            block, evidence_id = self._call(
                {"module": "proxy", "action": "eth_getBlockByNumber", "tag": hex(block_number), "boolean": "false"}
            )
            if not isinstance(block, dict) or "timestamp" not in block:
                raise SourceError(f"block {block_number} not returned by proxy")
            self._block_time_cache[block_number] = (int(block["timestamp"], 16), evidence_id)
        return self._block_time_cache[block_number]

    def verify(self, transfer: Transfer) -> Verification:
        method = f"etherscan-proxy:{self.chain}"
        if transfer.kind is TransferKind.INTERNAL:
            return Verification(
                transfer_id=transfer.id,
                status=VerificationStatus.UNVERIFIABLE,
                method=method,
                detail="internal value transfers do not appear in receipts; confirming them needs a tracing node (debug_traceTransaction)",
            )
        try:
            receipt, receipt_ev = self._call(
                {"module": "proxy", "action": "eth_getTransactionReceipt", "txhash": transfer.tx_hash}
            )
            if not isinstance(receipt, dict):
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction receipt not found", evidence_ids=(receipt_ev,))
            evidence = [receipt_ev]
            problems: list[tuple[str, object, object]] = []
            if receipt.get("status") != "0x1":
                problems.append(("status", "success", receipt.get("status")))
            block = int(receipt["blockNumber"], 16)
            problems.append(("block_number", transfer.block_number, block))

            if transfer.kind is TransferKind.NATIVE:
                tx, tx_ev = self._call({"module": "proxy", "action": "eth_getTransactionByHash", "txhash": transfer.tx_hash})
                evidence.append(tx_ev)
                if not isinstance(tx, dict):
                    return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction not found", evidence_ids=tuple(evidence))
                problems += [
                    ("sender", transfer.sender, (tx.get("from") or "").lower()),
                    ("receiver", transfer.receiver, (tx.get("to") or "").lower()),
                    ("amount", transfer.amount, int(tx.get("value") or "0x0", 16)),
                ]
            else:
                contract = transfer.asset.contract or ""
                matches = 0
                for log in receipt.get("logs") or []:
                    topics = [t.lower().removeprefix("0x") for t in log.get("topics") or []]
                    if (
                        (log.get("address") or "").lower() == contract
                        and len(topics) == 3
                        and topics[0] == TRANSFER_TOPIC
                        and topics[1][-40:] == transfer.sender[2:]
                        and topics[2][-40:] == transfer.receiver[2:]
                        and int(log.get("data") or "0x0", 16) == transfer.amount
                    ):
                        matches += 1
                if matches <= transfer.position:
                    problems.append(("transfer_event", f"occurrence #{transfer.position}", f"{matches} matching Transfer events"))

            block_ts, block_ev = self._block_timestamp(block)
            evidence.append(block_ev)
            problems.append(("timestamp", int(transfer.timestamp.timestamp()), block_ts))
            detail = mismatch_detail(problems)
            status = VerificationStatus.MISMATCH if detail else VerificationStatus.VERIFIED
            return Verification(transfer_id=transfer.id, status=status, method=method, detail=detail, block_number=block, evidence_ids=tuple(evidence))
        except SourceError as exc:
            return Verification(transfer_id=transfer.id, status=VerificationStatus.ERROR, method=method, detail=str(exc))

    # ------------------------------------------------------------------ extras

    def is_contract(self, address: str) -> bool | None:
        address = normalize(self.chain, address)
        if address not in self._contract_cache:
            code, _ = self._call({"module": "proxy", "action": "eth_getCode", "address": address, "tag": "latest"})
            if not isinstance(code, str):
                return None
            self._contract_cache[address] = code not in ("0x", "0x0", "")
        return self._contract_cache[address]

    def balance(self, address: str, asset: Asset) -> Balance | None:
        address = normalize(self.chain, address)
        if asset.contract is None:
            result, evidence_id = self._call({"module": "account", "action": "balance", "address": address, "tag": "latest"})
        else:
            result, evidence_id = self._call(
                {"module": "account", "action": "tokenbalance", "contractaddress": asset.contract, "address": address, "tag": "latest"}
            )
        try:
            return Balance(amount=int(result), evidence_id=evidence_id, as_of="latest block at fetch time")
        except (TypeError, ValueError) as exc:
            raise SourceError("Etherscan balance result is not an integer") from exc
