"""EVM chains through any standard JSON-RPC endpoint — no paid indexer needed (ADR-0021).

Plain JSON-RPC has no "history of an address" call, so this source scans token Transfer
logs (`eth_getLogs` filtered by the verified registry contracts and by the address as
sender or receiver) over a **bounded time window** that starts at the incident time:

  * the window is [since, since + window_hours], capped at the chain's latest block (read
    from the chain, never from the wall clock, so replays are exact);
  * block numbers for the window come from a binary search over block timestamps;
  * ranges are split into chunks the endpoint accepts (e.g. 5,000 blocks; 1,000 on the
    Polygon public endpoint), halving automatically when an endpoint rejects a range;
  * histories are complete *within* the window and say so; everything outside it is a
    declared coverage gap.

Not visible in logs: native-coin transfers (BNB, ETH, AVAX …) — reported in every history
note. Verification uses `eth_getTransactionReceipt` + `eth_getBlockByNumber`.

Public endpoints and their limits were checked on 2026-10-03 (see data defaults in config).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ..addresses import normalize
from ..assets import AssetRegistry
from ..chain import Chain, ChainFamily
from ..domain import AddressHistory, Asset, Transfer, TransferKind, utc_from_timestamp
from ..errors import SourceError
from ..evidence import Fetcher
from .base import TRANSFER_TOPIC, Balance, ChainSource, Verification, VerificationStatus, _Pending, assign_occurrence_positions, dedupe_sorted, mismatch_detail
from .evm_common import _addr, count_matching_events, receipt_transfers, topic_address

RANGE_ERRORS = ("range", "limit", "too many", "exceed", "-32005", "-32602")


def _pad(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


class RpcLogSource(ChainSource):
    def __init__(
        self,
        chain: Chain,
        fetcher: Fetcher,
        registry: AssetRegistry,
        rpc_url: str,
        window_start: datetime,
        window_hours: int = 72,
        max_span: int = 5000,
        max_requests: int = 600,
    ):
        if chain.family is not ChainFamily.EVM:
            raise ValueError(f"{chain} is not an EVM chain")
        if window_start.tzinfo is None:
            raise ValueError("window_start must be timezone-aware")
        self.chain = chain
        self.fetcher = fetcher
        self.registry = registry
        self.rpc_url = rpc_url
        self.window_start = window_start.astimezone(timezone.utc)
        self.window_hours = window_hours
        self.max_span = max_span
        self.max_requests = max_requests
        self._tokens = sorted(t.asset.contract for t in registry.tokens() if t.asset.chain is chain)
        self._block_ts: dict[int, int] = {}
        self._window_blocks: tuple[int, int, datetime] | None = None
        self._contract_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------ transport

    def _rpc(self, method: str, params: list) -> tuple[object, str]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(4):
            fetched = self.fetcher.post(self.rpc_url, body=body)
            data = fetched.data
            if not isinstance(data, dict):
                raise SourceError(f"{self.chain} RPC {method}: unexpected payload")
            error = data.get("error")
            if error:
                text = str(error).lower()
                if "rate" in text or (isinstance(error, dict) and error.get("code") == 429):
                    time.sleep(1.0 + attempt)
                    continue
                if "pruned" in text or "missing trie node" in text:
                    raise SourceError(
                        f"{self.chain} RPC {method}: the endpoint no longer serves this part of history ({error}); "
                        f"configure an archive endpoint with ANVESHAK_RPC_{self.chain.value.upper()}"
                    )
                raise SourceError(f"{self.chain} RPC {method} error: {error}")
            return data.get("result"), fetched.evidence_id
        raise SourceError(f"{self.chain} RPC {method}: rate limit persisted")

    def _block_timestamp(self, number: int) -> int:
        if number not in self._block_ts:
            block, _ = self._rpc("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(block, dict) or "timestamp" not in block:
                raise SourceError(f"{self.chain} block {number} not returned")
            self._block_ts[number] = int(block["timestamp"], 16)
        return self._block_ts[number]

    def _first_block_at_or_after(self, ts: int, latest: int) -> int:
        """First block with timestamp >= ts. Starts from an estimate based on recent block times
        and brackets outward, because public endpoints often prune old blocks (a search from
        block 0 would fail on them). Block timestamps are non-decreasing, so this is exact."""
        latest_ts = self._block_timestamp(latest)
        if ts >= latest_ts:
            return latest
        sample = max(0, latest - 1000)
        avg = max((latest_ts - self._block_timestamp(sample)) / max(1, latest - sample), 0.05)
        guess = min(latest, max(0, int(latest - (latest_ts - ts) / avg)))
        step = max(1, int(600 / avg))  # ~10 minutes of blocks, doubling
        lo = guess
        while lo > 0 and self._block_timestamp(lo) >= ts:
            lo, step = max(0, lo - step), step * 2
        hi, step = max(lo, guess), max(1, int(600 / avg))
        while hi < latest and self._block_timestamp(hi) < ts:
            hi, step = min(latest, hi + step), step * 2
        while lo < hi:  # invariant: ts(lo) < ts <= ts(hi), or lo == 0
            mid = (lo + hi) // 2
            if self._block_timestamp(mid) < ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _window(self) -> tuple[int, int, datetime]:
        if self._window_blocks is None:
            latest_hex, _ = self._rpc("eth_blockNumber", [])
            latest = int(str(latest_hex), 16)
            latest_ts = self._block_timestamp(latest)
            start_ts = int(self.window_start.timestamp())
            end_ts = min(start_ts + self.window_hours * 3600, latest_ts)
            if start_ts > latest_ts:
                raise SourceError(f"window start {self.window_start.isoformat()} is after the latest {self.chain} block")
            first = self._first_block_at_or_after(start_ts, latest)
            last = self._first_block_at_or_after(end_ts, latest) if end_ts < latest_ts else latest
            self._window_blocks = (first, last, utc_from_timestamp(end_ts))
        return self._window_blocks

    # ------------------------------------------------------------------ history

    def _logs(self, topics: list, first: int, last: int, budget: list[int]) -> tuple[list[tuple[dict, str]], bool]:
        out: list[tuple[dict, str]] = []
        span = self.max_span
        start = first
        while start <= last:
            if budget[0] <= 0:
                return out, False
            end = min(start + span - 1, last)
            try:
                logs, evidence_id = self._rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": self._tokens, "topics": topics}])
            except SourceError as exc:
                if any(k in str(exc).lower() for k in RANGE_ERRORS) and span > 50:
                    span //= 2
                    continue
                raise
            budget[0] -= 1
            if not isinstance(logs, list):
                raise SourceError(f"{self.chain} eth_getLogs returned a non-list")
            out.extend((log, evidence_id) for log in logs)
            start = end + 1
        return out, True

    def history(self, address: str) -> AddressHistory:
        address = normalize(self.chain, address)
        first, last, end_time = self._window()
        budget = [self.max_requests]
        complete = True
        rows: list[tuple[dict, str]] = []
        for topics in ([TRANSFER_TOPIC_0X, _pad(address)], [TRANSFER_TOPIC_0X, None, _pad(address)]):
            got, done = self._logs(topics, first, last, budget)
            rows.extend(got)
            complete &= done
        pending: list[_Pending] = []
        seen: set[tuple[str, str]] = set()
        for log, evidence_id in sorted(rows, key=lambda r: (int(r[0]["blockNumber"], 16), int(r[0].get("logIndex") or "0x0", 16))):
            key = (log["transactionHash"].lower(), str(log.get("logIndex")))
            if key in seen:
                continue  # a self-transfer appears in both queries
            seen.add(key)
            topics = log.get("topics") or []
            amount = int(log.get("data") or "0x0", 16)
            if len(topics) != 3 or amount == 0:
                continue
            block = int(log["blockNumber"], 16)
            ts = int(log["blockTimestamp"], 16) if log.get("blockTimestamp") else self._block_timestamp(block)
            pending.append(
                _Pending(
                    tx_hash=log["transactionHash"].lower(),
                    kind=TransferKind.TOKEN,
                    sender=_addr(self.chain, topic_address(topics[1])),
                    receiver=_addr(self.chain, topic_address(topics[2])),
                    asset=self.registry.token(self.chain, _addr(self.chain, log["address"]), "", 0),
                    amount=amount,
                    timestamp=utc_from_timestamp(ts),
                    block_number=block,
                    evidence_id=evidence_id,
                )
            )
        transfers = [t for t in assign_occurrence_positions(pending) if address in (t.sender, t.receiver)]
        note = (
            f"window-limited RPC log scan {self.window_start:%Y-%m-%d %H:%M}–{end_time:%Y-%m-%d %H:%M} UTC "
            f"(blocks {first}–{last}); verified-registry tokens only; native-coin transfers not visible in logs"
        )
        if not complete:
            note += f"; request budget of {self.max_requests} exhausted"
        return AddressHistory(
            chain=self.chain, address=address, transfers=dedupe_sorted(transfers), complete=complete, note=note,
            window_start=self.window_start, window_end=end_time,
        )

    # ------------------------------------------------------------------ verification and extras

    def _receipt(self, tx_hash: str) -> tuple[dict | None, str]:
        receipt, evidence_id = self._rpc("eth_getTransactionReceipt", [tx_hash])
        return (receipt if isinstance(receipt, dict) else None), evidence_id

    def verify(self, transfer: Transfer) -> Verification:
        method = f"json-rpc-receipt:{self.chain}"
        try:
            receipt, evidence_id = self._receipt(transfer.tx_hash)
            if receipt is None:
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction receipt not found", evidence_ids=(evidence_id,))
            block = int(receipt["blockNumber"], 16)
            problems: list[tuple[str, object, object]] = [
                ("status", "success", "success" if receipt.get("status") == "0x1" else receipt.get("status")),
                ("block_number", transfer.block_number, block),
                ("timestamp", int(transfer.timestamp.timestamp()), self._block_timestamp(block)),
            ]
            matches = count_matching_events(transfer, receipt)
            if matches <= transfer.position:
                problems.append(("transfer_event", f"occurrence #{transfer.position}", f"{matches} matching Transfer events"))
            detail = mismatch_detail(problems)
            return Verification(
                transfer_id=transfer.id, status=VerificationStatus.MISMATCH if detail else VerificationStatus.VERIFIED,
                method=method, detail=detail, block_number=block, evidence_ids=(evidence_id,),
            )
        except SourceError as exc:
            return Verification(transfer_id=transfer.id, status=VerificationStatus.ERROR, method=method, detail=str(exc))

    def tx_transfers(self, tx_hash: str) -> list[Transfer] | None:
        receipt, evidence_id = self._receipt(tx_hash)
        if receipt is None:
            return []
        return receipt_transfers(self.chain, self.registry, receipt, self._block_timestamp(int(receipt["blockNumber"], 16)), evidence_id)

    def has_activity(self, address: str) -> bool:
        """Cheap probe without a history index: a sent transaction (nonce > 0), a native
        balance, or a balance of any verified token. Only used to choose chains to trace."""
        address = normalize(self.chain, address)
        nonce, _ = self._rpc("eth_getTransactionCount", [address, "latest"])
        if int(str(nonce), 16) > 0:
            return True
        native = self.balance(address, self.registry.native(self.chain))
        if native and native.amount > 0:
            return True
        for token in (t.asset for t in self.registry.tokens() if t.asset.chain is self.chain):
            bal = self.balance(address, token)
            if bal and bal.amount > 0:
                return True
        return False

    def tx_count(self, address: str) -> int | None:
        """Transactions sent from the address so far (its nonce at the latest block): a
        lifetime figure, unlike the window-limited history (R-BUSY-ACCOUNT, ADR-0026)."""
        nonce, _ = self._rpc("eth_getTransactionCount", [normalize(self.chain, address), "latest"])
        try:
            return int(str(nonce), 16)
        except ValueError as exc:
            raise SourceError(f"{self.chain} eth_getTransactionCount returned {nonce!r}") from exc

    def is_contract(self, address: str) -> bool | None:
        address = normalize(self.chain, address)
        if address not in self._contract_cache:
            code, _ = self._rpc("eth_getCode", [address, "latest"])
            if not isinstance(code, str):
                return None
            self._contract_cache[address] = code not in ("0x", "0x0", "")
        return self._contract_cache[address]

    def balance(self, address: str, asset: Asset) -> Balance | None:
        address = normalize(self.chain, address)
        if asset.contract is None:
            result, evidence_id = self._rpc("eth_getBalance", [address, "latest"])
        else:
            data = "0x70a08231" + "0" * 24 + address[2:]  # balanceOf(address)
            result, evidence_id = self._rpc("eth_call", [{"to": asset.contract, "data": data}, "latest"])
        try:
            return Balance(amount=int(str(result), 16), evidence_id=evidence_id, as_of="latest block at fetch time")
        except (TypeError, ValueError) as exc:
            raise SourceError(f"{self.chain} balance result is not hex") from exc


TRANSFER_TOPIC_0X = "0x" + TRANSFER_TOPIC
