"""Receipt parsing shared by the EVM sources (Etherscan proxy and plain JSON-RPC)."""

from __future__ import annotations

from ..addresses import AddressError, normalize
from ..assets import AssetRegistry
from ..chain import Chain
from ..domain import Transfer, TransferKind, utc_from_timestamp
from ..errors import SourceError
from .base import TRANSFER_TOPIC, _Pending, assign_occurrence_positions


def _addr(chain: Chain, raw: str) -> str:
    try:
        return normalize(chain, raw)
    except AddressError as exc:
        raise SourceError(f"malformed address {raw!r} in {chain} data") from exc


def topic_address(topic: str) -> str:
    """Last 20 bytes of a 32-byte topic, as 0x-address."""
    return "0x" + topic.lower().removeprefix("0x")[-40:]


def receipt_transfers(chain: Chain, registry: AssetRegistry, receipt: dict, block_timestamp: int, evidence_id: str) -> list[Transfer]:
    """Every ERC-20 Transfer event in a successful receipt, as Transfers (verified or not)."""
    if receipt.get("status") != "0x1":
        return []
    tx_hash = str(receipt.get("transactionHash", "")).lower()
    block = int(receipt["blockNumber"], 16)
    pending: list[_Pending] = []
    for log in receipt.get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) != 3 or topics[0].removeprefix("0x") != TRANSFER_TOPIC:
            continue
        amount = int(log.get("data") or "0x0", 16)
        if amount == 0:
            continue
        contract = _addr(chain, log["address"])
        pending.append(
            _Pending(
                tx_hash=tx_hash,
                kind=TransferKind.TOKEN,
                sender=_addr(chain, topic_address(topics[1])),
                receiver=_addr(chain, topic_address(topics[2])),
                asset=registry.token(chain, contract, "", 0),
                amount=amount,
                timestamp=utc_from_timestamp(block_timestamp),
                block_number=block,
                evidence_id=evidence_id,
            )
        )
    return assign_occurrence_positions(pending)


def count_matching_events(transfer: Transfer, receipt: dict) -> int:
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
    return matches
