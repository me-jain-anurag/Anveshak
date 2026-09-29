"""Supported chains and their fixed properties."""

from __future__ import annotations

from enum import StrEnum


class ChainFamily(StrEnum):
    UTXO = "utxo"
    EVM = "evm"
    TRON = "tron"


class Chain(StrEnum):
    BITCOIN = "bitcoin"
    ETHEREUM = "ethereum"
    BSC = "bsc"
    POLYGON = "polygon"
    TRON = "tron"

    @property
    def family(self) -> ChainFamily:
        return _FAMILY[self]

    @property
    def native_symbol(self) -> str:
        return _NATIVE[self][0]

    @property
    def native_decimals(self) -> int:
        return _NATIVE[self][1]

    def tx_url(self, tx_hash: str) -> str:
        return _EXPLORER[self][0].format(tx_hash)

    def address_url(self, address: str) -> str:
        return _EXPLORER[self][1].format(address)


_FAMILY = {
    Chain.BITCOIN: ChainFamily.UTXO,
    Chain.ETHEREUM: ChainFamily.EVM,
    Chain.BSC: ChainFamily.EVM,
    Chain.POLYGON: ChainFamily.EVM,
    Chain.TRON: ChainFamily.TRON,
}

# Native coin symbol and base-unit decimals (satoshi, wei, sun).
_NATIVE = {
    Chain.BITCOIN: ("BTC", 8),
    Chain.ETHEREUM: ("ETH", 18),
    Chain.BSC: ("BNB", 18),
    Chain.POLYGON: ("POL", 18),
    Chain.TRON: ("TRX", 6),
}

# Etherscan API V2 chain ids.
EVM_CHAIN_IDS = {Chain.ETHEREUM: 1, Chain.BSC: 56, Chain.POLYGON: 137}

_EXPLORER = {
    Chain.BITCOIN: ("https://blockstream.info/tx/{}", "https://blockstream.info/address/{}"),
    Chain.ETHEREUM: ("https://etherscan.io/tx/{}", "https://etherscan.io/address/{}"),
    Chain.BSC: ("https://bscscan.com/tx/{}", "https://bscscan.com/address/{}"),
    Chain.POLYGON: ("https://polygonscan.com/tx/{}", "https://polygonscan.com/address/{}"),
    Chain.TRON: ("https://tronscan.org/#/transaction/{}", "https://tronscan.org/#/address/{}"),
}
