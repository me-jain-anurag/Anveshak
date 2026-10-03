"""Tron via TronGrid.

Listing: `/v1/accounts/{addr}/transactions/trc20` (TRC-20 Transfer events) and
`/v1/accounts/{addr}/transactions` (native TRX TransferContract), confirmed only.
Verification: the full-node HTTP API — `/wallet/gettransactioninfobyid` (receipt, logs,
block) and `/wallet/gettransactionbyid` (native transfer parameters) — a different code
path from the v1 event index.

Response formats were checked against live TronGrid responses on 2026-09-30. The TRC-20
listing carries no block number; it is filled in during verification.
Not covered (documented in ADR-0012): TRC-10 tokens, TRX moved by contract internal calls.
"""

from __future__ import annotations

from ..addresses import AddressError, normalize, tron_from_hex, tron_to_hex
from ..assets import AssetRegistry
from ..chain import Chain
from ..domain import AddressHistory, Asset, Transfer, TransferKind, utc_from_timestamp
from ..errors import SourceError
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


class TronGridSource(ChainSource):
    chain = Chain.TRON

    def __init__(
        self,
        fetcher: Fetcher,
        registry: AssetRegistry,
        api_key: str = "",
        base_url: str = "https://api.trongrid.io",
        max_records_per_list: int = 5000,
        page_size: int = 200,
    ):
        self.fetcher = fetcher
        self.registry = registry
        self.base_url = base_url.rstrip("/")
        self.headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
        self.max_records = max_records_per_list
        self.page_size = page_size

    def _get(self, path: str, params: dict) -> tuple[dict, str]:
        fetched = self.fetcher.get(f"{self.base_url}{path}", params=params, headers=self.headers)
        data = fetched.data
        if not isinstance(data, dict) or data.get("success") is False:
            error = data.get("error") if isinstance(data, dict) else "unexpected payload"
            raise SourceError(f"TronGrid error on {path}: {error}")
        return data, fetched.evidence_id

    def _post(self, path: str, body: dict) -> tuple[dict, str]:
        fetched = self.fetcher.post(f"{self.base_url}{path}", body=body, headers=self.headers)
        if not isinstance(fetched.data, dict):
            raise SourceError(f"TronGrid returned an unexpected payload on {path}")
        return fetched.data, fetched.evidence_id

    def _address(self, raw: str) -> str:
        try:
            return tron_from_hex(raw) if not raw.startswith("T") else normalize(Chain.TRON, raw)
        except AddressError as exc:
            raise SourceError(f"malformed Tron address {raw!r} in TronGrid data") from exc

    def _list(self, path: str) -> tuple[list[tuple[dict, str]], bool]:
        rows: list[tuple[dict, str]] = []
        fingerprint = None
        while True:
            params = {"limit": str(self.page_size), "only_confirmed": "true"}
            if fingerprint:
                params["fingerprint"] = fingerprint
            data, evidence_id = self._get(path, params)
            batch = data.get("data") or []
            rows.extend((r, evidence_id) for r in batch)
            fingerprint = (data.get("meta") or {}).get("fingerprint")
            if not fingerprint or not batch:
                return rows, True
            if len(rows) >= self.max_records:
                return rows, False

    def history(self, address: str) -> AddressHistory:
        address = normalize(Chain.TRON, address)
        pending: list[_Pending] = []

        trc20_rows, trc20_done = self._list(f"/v1/accounts/{address}/transactions/trc20")
        for row, evidence_id in trc20_rows:
            if row.get("type") != "Transfer":
                continue  # e.g. Approval events
            value = int(row.get("value") or 0)
            if value == 0:
                continue
            info = row.get("token_info") or {}
            contract = self._address(info.get("address", ""))
            try:
                decimals = int(info.get("decimals") or 0)
            except (TypeError, ValueError):
                decimals = 0
            pending.append(
                _Pending(
                    tx_hash=row["transaction_id"].lower(),
                    kind=TransferKind.TOKEN,
                    sender=self._address(row["from"]),
                    receiver=self._address(row["to"]),
                    asset=self.registry.token(Chain.TRON, contract, info.get("symbol", ""), decimals),
                    amount=value,
                    timestamp=utc_from_timestamp(int(row["block_timestamp"]) / 1000),
                    evidence_id=evidence_id,
                )
            )

        native = self.registry.native(Chain.TRON)
        native_rows, native_done = self._list(f"/v1/accounts/{address}/transactions")
        for row, evidence_id in native_rows:
            ret = (row.get("ret") or [{}])[0]
            contracts = (row.get("raw_data") or {}).get("contract") or []
            if ret.get("contractRet") != "SUCCESS" or len(contracts) != 1:
                continue
            contract = contracts[0]
            if contract.get("type") != "TransferContract":
                continue
            value = (contract.get("parameter") or {}).get("value") or {}
            amount = int(value.get("amount") or 0)
            if amount == 0:
                continue
            pending.append(
                _Pending(
                    tx_hash=row["txID"].lower(),
                    kind=TransferKind.NATIVE,
                    sender=self._address(value["owner_address"]),
                    receiver=self._address(value["to_address"]),
                    asset=native,
                    amount=amount,
                    timestamp=utc_from_timestamp(int(row["block_timestamp"]) / 1000),
                    block_number=int(row["blockNumber"]) if row.get("blockNumber") is not None else None,
                    evidence_id=evidence_id,
                )
            )

        complete = trc20_done and native_done
        transfers = [t for t in assign_occurrence_positions(pending) if address in (t.sender, t.receiver)]
        note = None if complete else f"history capped at {self.max_records} records per list"
        return AddressHistory(chain=Chain.TRON, address=address, transfers=dedupe_sorted(transfers), complete=complete, note=note)

    def verify(self, transfer: Transfer) -> Verification:
        method = "trongrid-fullnode"
        try:
            info, info_ev = self._post("/wallet/gettransactioninfobyid", {"value": transfer.tx_hash})
            evidence = [info_ev]
            if not info.get("id"):
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction not found on full node", evidence_ids=tuple(evidence))
            block = info.get("blockNumber")
            problems: list[tuple[str, object, object]] = [
                ("timestamp_ms", int(transfer.timestamp.timestamp() * 1000), info.get("blockTimeStamp")),
            ]
            if transfer.block_number is not None:
                problems.append(("block_number", transfer.block_number, block))

            if transfer.kind is TransferKind.TOKEN:
                receipt = info.get("receipt") or {}
                if receipt.get("result") != "SUCCESS":
                    problems.append(("result", "SUCCESS", receipt.get("result")))
                contract_hex = tron_to_hex(transfer.asset.contract or "")[2:]
                sender_hex, receiver_hex = tron_to_hex(transfer.sender)[2:], tron_to_hex(transfer.receiver)[2:]
                matches = 0
                for log in info.get("log") or []:
                    topics = [t.lower() for t in log.get("topics") or []]
                    if (
                        (log.get("address") or "").lower() == contract_hex
                        and len(topics) == 3
                        and topics[0] == TRANSFER_TOPIC
                        and topics[1][-40:] == sender_hex
                        and topics[2][-40:] == receiver_hex
                        and int(log.get("data") or "0", 16) == transfer.amount
                    ):
                        matches += 1
                if matches <= transfer.position:
                    problems.append(("transfer_event", f"occurrence #{transfer.position}", f"{matches} matching Transfer events"))
            elif transfer.kind is TransferKind.NATIVE:
                tx, tx_ev = self._post("/wallet/gettransactionbyid", {"value": transfer.tx_hash})
                evidence.append(tx_ev)
                ret = (tx.get("ret") or [{}])[0].get("contractRet")
                contract = ((tx.get("raw_data") or {}).get("contract") or [{}])[0]
                value = (contract.get("parameter") or {}).get("value") or {}
                problems += [
                    ("result", "SUCCESS", ret),
                    ("type", "TransferContract", contract.get("type")),
                    ("sender", transfer.sender, self._address(value["owner_address"]) if value.get("owner_address") else None),
                    ("receiver", transfer.receiver, self._address(value["to_address"]) if value.get("to_address") else None),
                    ("amount", transfer.amount, int(value.get("amount") or 0)),
                ]
            else:
                return Verification(transfer_id=transfer.id, status=VerificationStatus.UNVERIFIABLE, method=method, detail=f"no verifier for {transfer.kind}")

            detail = mismatch_detail(problems)
            status = VerificationStatus.MISMATCH if detail else VerificationStatus.VERIFIED
            return Verification(transfer_id=transfer.id, status=status, method=method, detail=detail, block_number=block, evidence_ids=tuple(evidence))
        except (SourceError, AddressError) as exc:
            return Verification(transfer_id=transfer.id, status=VerificationStatus.ERROR, method=method, detail=str(exc))

    def tx_transfers(self, tx_hash: str) -> list[Transfer] | None:
        info, info_ev = self._post("/wallet/gettransactioninfobyid", {"value": tx_hash})
        if not info.get("id"):
            return []
        ts = utc_from_timestamp(int(info.get("blockTimeStamp") or 0) / 1000)
        block = info.get("blockNumber")
        pending: list[_Pending] = []
        for log in info.get("log") or []:
            topics = [t.lower() for t in log.get("topics") or []]
            if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
                continue
            amount = int(log.get("data") or "0", 16)
            if amount == 0:
                continue
            contract = self._address(log.get("address", ""))
            pending.append(_Pending(tx_hash=tx_hash.lower(), kind=TransferKind.TOKEN, sender=self._address(topics[1][-40:]), receiver=self._address(topics[2][-40:]),
                                    asset=self.registry.token(Chain.TRON, contract, "", 0), amount=amount, timestamp=ts, block_number=block, evidence_id=info_ev))
        tx, tx_ev = self._post("/wallet/gettransactionbyid", {"value": tx_hash})
        ret = (tx.get("ret") or [{}])[0].get("contractRet")
        contract = ((tx.get("raw_data") or {}).get("contract") or [{}])[0]
        value = (contract.get("parameter") or {}).get("value") or {}
        if ret == "SUCCESS" and contract.get("type") == "TransferContract" and int(value.get("amount") or 0) > 0:
            pending.append(_Pending(tx_hash=tx_hash.lower(), kind=TransferKind.NATIVE, sender=self._address(value["owner_address"]), receiver=self._address(value["to_address"]),
                                    asset=self.registry.native(Chain.TRON), amount=int(value["amount"]), timestamp=ts, block_number=block, evidence_id=tx_ev))
        return assign_occurrence_positions(pending)

    def balance(self, address: str, asset: Asset) -> Balance | None:
        address = normalize(Chain.TRON, address)
        data, evidence_id = self._get(f"/v1/accounts/{address}", {"only_confirmed": "true"})
        accounts = data.get("data") or []
        if not accounts:
            return Balance(amount=0, evidence_id=evidence_id, as_of="account not activated")
        account = accounts[0]
        if asset.contract is None:
            return Balance(amount=int(account.get("balance") or 0), evidence_id=evidence_id, as_of="latest confirmed block")
        for entry in account.get("trc20") or []:
            if asset.contract in entry:
                return Balance(amount=int(entry[asset.contract]), evidence_id=evidence_id, as_of="latest confirmed block")
        return Balance(amount=0, evidence_id=evidence_id, as_of="latest confirmed block")
