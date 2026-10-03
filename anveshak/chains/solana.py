"""Solana via standard JSON-RPC (any provider: public mainnet-beta, Helius, Triton ...).

Wallet history is assembled from:
  * getTokenAccountsByOwner(wallet, mint)  for each verified SPL mint (USDT, USDC) — incoming
    token transfers hit the wallet's *token account*, not the wallet, so without this step
    incoming USDT would be silently missed;
  * getSignaturesForAddress(wallet and each token account), finalized, successful only;
  * getTransaction(signature, jsonParsed) for each signature.

Transfers are read from parsed instructions (top-level and inner):
  system  transfer / transferWithSeed          → native SOL, wallet to wallet
  spl-token(-2022) transfer / transferChecked   → token, mapped from token accounts to their
                                                 owners via pre/postTokenBalances

Verification re-fetches the transaction and checks, besides slot and time, that for every
token account involved the net of the parsed instruction amounts equals the change in its
recorded balance — the instruction view and the state view of the ledger must agree.

Formats checked against live mainnet-beta responses on 2026-09-30.
"""

from __future__ import annotations

import time
from collections import defaultdict

from ..addresses import AddressError, normalize, solana_is_program_derived
from ..assets import AssetRegistry
from ..chain import Chain
from ..domain import AddressHistory, Asset, Transfer, TransferKind, utc_from_timestamp
from ..errors import SourceError
from ..evidence import Fetcher
from .base import Balance, ChainSource, Verification, VerificationStatus, _Pending, assign_occurrence_positions, dedupe_sorted, mismatch_detail

TOKEN_PROGRAMS = ("spl-token", "spl-token-2022")


class SolanaRpcSource(ChainSource):
    chain = Chain.SOLANA

    def __init__(self, fetcher: Fetcher, registry: AssetRegistry, rpc_url: str = "https://api.mainnet-beta.solana.com", max_signatures: int = 300):
        self.fetcher = fetcher
        self.registry = registry
        self.rpc_url = rpc_url
        self.max_signatures = max_signatures
        self._mints = [t.asset.contract for t in registry.tokens() if t.asset.chain is Chain.SOLANA]

    # ------------------------------------------------------------------ transport

    def _rpc(self, method: str, params: list) -> tuple[object, str]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(5):
            fetched = self.fetcher.post(self.rpc_url, body=body)
            data = fetched.data
            if not isinstance(data, dict):
                raise SourceError(f"Solana RPC {method}: unexpected payload")
            error = data.get("error")
            if error:
                if isinstance(error, dict) and error.get("code") in (429, -32005, -32429):
                    time.sleep(1.0 + attempt)
                    continue
                raise SourceError(f"Solana RPC {method} error: {error}")
            return data.get("result"), fetched.evidence_id
        raise SourceError(f"Solana RPC {method}: rate limit persisted")

    # ------------------------------------------------------------------ parsing

    def _address(self, raw: str) -> str:
        try:
            return normalize(Chain.SOLANA, raw)
        except AddressError as exc:
            raise SourceError(f"malformed Solana address {raw!r}") from exc

    def parse(self, tx: dict, evidence_id: str) -> tuple[list[_Pending], list[tuple[str, str, str, int]]]:
        """Return pending transfers and raw token movements (mint, src_acct, dst_acct, amount)."""
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return [], []
        message = (tx.get("transaction") or {}).get("message") or {}
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in message.get("accountKeys") or []]
        token_accounts: dict[str, tuple[str | None, str, int]] = {}
        for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
            idx = b.get("accountIndex")
            if isinstance(idx, int) and idx < len(keys):
                token_accounts[keys[idx]] = (b.get("owner"), b["mint"], int((b.get("uiTokenAmount") or {}).get("decimals") or 0))
        inner = defaultdict(list)
        for group in meta.get("innerInstructions") or []:
            inner[group.get("index")].extend(group.get("instructions") or [])
        ordered = []
        for i, ix in enumerate(message.get("instructions") or []):
            ordered.append(ix)
            ordered.extend(inner.get(i, []))

        signature = (tx.get("transaction") or {}).get("signatures", [""])[0]
        slot, block_time = tx.get("slot"), tx.get("blockTime")
        if block_time is None:
            return [], []
        ts = utc_from_timestamp(int(block_time))
        pending: list[_Pending] = []
        movements: list[tuple[str, str, str, int]] = []
        native = self.registry.native(Chain.SOLANA)
        for ix in ordered:
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            kind, info = parsed.get("type"), parsed.get("info") or {}
            program = ix.get("program")
            if program == "system" and kind in ("transfer", "transferWithSeed"):
                lamports = int(info.get("lamports") or 0)
                if lamports > 0:
                    pending.append(_Pending(tx_hash=signature, kind=TransferKind.NATIVE, sender=self._address(info["source"]), receiver=self._address(info["destination"]),
                                            asset=native, amount=lamports, timestamp=ts, block_number=slot, evidence_id=evidence_id))
            elif program in TOKEN_PROGRAMS and kind in ("transfer", "transferChecked"):
                src, dst = info.get("source"), info.get("destination")
                amount = int(info["amount"]) if kind == "transfer" else int((info.get("tokenAmount") or {}).get("amount") or 0)
                s_owner, s_mint, dec = token_accounts.get(src, (None, None, 0))
                d_owner, d_mint, d_dec = token_accounts.get(dst, (None, None, 0))
                mint = info.get("mint") or s_mint or d_mint
                if not (mint and s_owner and d_owner) or amount <= 0:
                    continue
                movements.append((mint, src, dst, amount))
                asset = self.registry.token(Chain.SOLANA, mint, "", dec or d_dec)
                pending.append(_Pending(tx_hash=signature, kind=TransferKind.TOKEN, sender=self._address(s_owner), receiver=self._address(d_owner),
                                        asset=asset, amount=amount, timestamp=ts, block_number=slot, evidence_id=evidence_id))
        return pending, movements

    def _get_tx(self, signature: str) -> tuple[dict | None, str]:
        tx, evidence_id = self._rpc("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "finalized"}])
        return (tx if isinstance(tx, dict) else None), evidence_id

    # ------------------------------------------------------------------ history

    def _token_accounts(self, owner: str) -> list[str]:
        accounts = []
        for mint in self._mints:
            result, _ = self._rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
            for entry in (result or {}).get("value") or []:
                accounts.append(entry["pubkey"])
        return sorted(set(accounts))

    def history(self, address: str) -> AddressHistory:
        address = normalize(Chain.SOLANA, address)
        signatures: dict[str, dict] = {}
        complete = True
        for account in [address, *self._token_accounts(address)]:
            before = None
            while True:
                options: dict = {"limit": min(1000, self.max_signatures + 1), "commitment": "finalized"}
                if before:
                    options["before"] = before
                page, _ = self._rpc("getSignaturesForAddress", [account, options])
                page = page or []
                for entry in page:
                    if entry.get("err") is None:
                        signatures[entry["signature"]] = entry
                if len(signatures) > self.max_signatures:
                    complete = False
                    break
                if len(page) < options["limit"]:
                    break
                before = page[-1]["signature"]
        newest = sorted(signatures.values(), key=lambda e: (e.get("slot") or 0, e["signature"]), reverse=True)[: self.max_signatures]
        pending: list[_Pending] = []
        for entry in sorted(newest, key=lambda e: (e.get("slot") or 0, e["signature"])):
            tx, evidence_id = self._get_tx(entry["signature"])
            if tx is not None:
                pending.extend(self.parse(tx, evidence_id)[0])
        transfers = [t for t in assign_occurrence_positions(pending) if address in (t.sender, t.receiver)]
        note = None if complete else f"more than {self.max_signatures} signatures; newest {self.max_signatures} parsed"
        return AddressHistory(chain=Chain.SOLANA, address=address, transfers=dedupe_sorted(transfers), complete=complete, note=note)

    # ------------------------------------------------------------------ verification

    def verify(self, transfer: Transfer) -> Verification:
        method = "solana-getTransaction+balance-delta"
        try:
            tx, evidence_id = self._get_tx(transfer.tx_hash)
            if tx is None:
                return Verification(transfer_id=transfer.id, status=VerificationStatus.MISMATCH, method=method, detail="transaction not found (finalized)")
            pending, movements = self.parse(tx, evidence_id)
            reparsed = {t.id for t in assign_occurrence_positions(pending)}
            problems: list[tuple[str, object, object]] = [
                ("present", True, transfer.id in reparsed),
                ("slot", transfer.block_number, tx.get("slot")),
                ("block_time", int(transfer.timestamp.timestamp()), tx.get("blockTime")),
            ]
            if transfer.kind is TransferKind.TOKEN:
                meta = tx.get("meta") or {}
                keys = [k["pubkey"] if isinstance(k, dict) else k for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]

                def balances(field: str) -> dict[str, int]:
                    out = {}
                    for b in meta.get(field) or []:
                        out[keys[b["accountIndex"]]] = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                    return out

                pre, post = balances("preTokenBalances"), balances("postTokenBalances")
                net: dict[str, int] = defaultdict(int)
                for mint, src, dst, amount in movements:
                    if mint == transfer.asset.contract:
                        net[src] -= amount
                        net[dst] += amount
                for account, delta in sorted(net.items()):
                    actual = post.get(account, 0) - pre.get(account, 0)
                    if actual != delta:
                        problems.append((f"balance change of token account {account}", delta, actual))
            detail = mismatch_detail(problems)
            status = VerificationStatus.MISMATCH if detail else VerificationStatus.VERIFIED
            return Verification(transfer_id=transfer.id, status=status, method=method, detail=detail, block_number=tx.get("slot"), evidence_ids=(evidence_id,))
        except SourceError as exc:
            return Verification(transfer_id=transfer.id, status=VerificationStatus.ERROR, method=method, detail=str(exc))

    # ------------------------------------------------------------------ extras

    def tx_transfers(self, tx_hash: str) -> list[Transfer] | None:
        tx, evidence_id = self._get_tx(tx_hash)
        if tx is None:
            return []
        return assign_occurrence_positions(self.parse(tx, evidence_id)[0])

    def is_contract(self, address: str) -> bool | None:
        """Program-derived (off-curve) addresses are program-controlled: pools, vaults, escrows."""
        try:
            return solana_is_program_derived(address)
        except AddressError:
            return None

    def balance(self, address: str, asset: Asset) -> Balance | None:
        address = normalize(Chain.SOLANA, address)
        if asset.contract is None:
            result, evidence_id = self._rpc("getBalance", [address, {"commitment": "finalized"}])
            return Balance(amount=int((result or {}).get("value") or 0), evidence_id=evidence_id, as_of=f"slot {(result or {}).get('context', {}).get('slot')}")
        result, evidence_id = self._rpc("getTokenAccountsByOwner", [address, {"mint": asset.contract}, {"encoding": "jsonParsed", "commitment": "finalized"}])
        total = 0
        for entry in (result or {}).get("value") or []:
            info = (((entry.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            total += int((info.get("tokenAmount") or {}).get("amount") or 0)
        return Balance(amount=total, evidence_id=evidence_id, as_of=f"slot {(result or {}).get('context', {}).get('slot')}")
