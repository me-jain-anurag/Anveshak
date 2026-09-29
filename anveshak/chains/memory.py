"""In-memory chain source for tests and the synthetic demo. Never used for live cases."""

from __future__ import annotations

from ..chain import Chain
from ..domain import AddressHistory, Asset, Transfer
from .base import Balance, ChainSource, Verification, VerificationStatus, dedupe_sorted


class MemorySource(ChainSource):
    def __init__(
        self,
        chain: Chain,
        transfers: list[Transfer],
        contracts: set[str] | None = None,
        balances: dict[tuple[str, str], int] | None = None,
        incomplete: set[str] | None = None,
        tampered: dict[str, Transfer] | None = None,
    ):
        self.chain = chain
        self._transfers = list(transfers)
        self._contracts = contracts or set()
        self._balances = balances or {}
        self._incomplete = incomplete or set()
        # transfer id -> what the "transaction-level endpoint" returns instead (to test MISMATCH)
        self._tampered = tampered or {}
        self.history_calls: list[str] = []

    def history(self, address: str) -> AddressHistory:
        self.history_calls.append(address)
        mine = [t for t in self._transfers if address in (t.sender, t.receiver)]
        complete = address not in self._incomplete
        return AddressHistory(
            chain=self.chain,
            address=address,
            transfers=dedupe_sorted(mine),
            complete=complete,
            note=None if complete else "synthetic: history marked incomplete",
        )

    def verify(self, transfer: Transfer) -> Verification:
        actual = self._tampered.get(transfer.id)
        if actual is not None:
            return Verification(
                transfer_id=transfer.id,
                status=VerificationStatus.MISMATCH,
                method="memory",
                detail=f"amount: listed {transfer.amount!r}, transaction shows {actual.amount!r}",
            )
        known = {t.id for t in self._transfers}
        status = VerificationStatus.VERIFIED if transfer.id in known else VerificationStatus.MISMATCH
        return Verification(transfer_id=transfer.id, status=status, method="memory", block_number=transfer.block_number)

    def is_contract(self, address: str) -> bool | None:
        return address in self._contracts

    def balance(self, address: str, asset: Asset) -> Balance | None:
        amount = self._balances.get((address, asset.key))
        if amount is None:
            return None
        return Balance(amount=amount, evidence_id="synthetic", as_of="synthetic")
