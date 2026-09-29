"""Supported chains and their fixed properties."""

from __future__ import annotations

from enum import StrEnum


class ChainFamily(StrEnum):
    UTXO = "utxo"
    EVM = "evm"
    TRON = "tron"
    SOLANA = "solana"


class Chain(StrEnum):
    BITCOIN = "bitcoin"
    ETHEREUM = "ethereum"
    BSC = "bsc"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"
    BASE = "base"
    OPTIMISM = "optimism"
    AVALANCHE = "avalanche"
    TRON = "tron"
    SOLANA = "solana"

    @property
    def family(self) -> ChainFamily:
        return _FAMILY[self]

    @property
    def native_symbol(self) -> str:
        return _NATIVE[self][0]

    @property
    def native_decimals(self) -> int:
        return _NATIVE[self][1]

    @property
    def display_name(self) -> str:
        return _DISPLAY[self]

    def tx_url(self, tx_hash: str) -> str:
        return _EXPLORER[self][0].format(tx_hash)

    def address_url(self, address: str) -> str:
        return _EXPLORER[self][1].format(address)


_FAMILY = {
    Chain.BITCOIN: ChainFamily.UTXO,
    Chain.ETHEREUM: ChainFamily.EVM,
    Chain.BSC: ChainFamily.EVM,
    Chain.POLYGON: ChainFamily.EVM,
    Chain.ARBITRUM: ChainFamily.EVM,
    Chain.BASE: ChainFamily.EVM,
    Chain.OPTIMISM: ChainFamily.EVM,
    Chain.AVALANCHE: ChainFamily.EVM,
    Chain.TRON: ChainFamily.TRON,
    Chain.SOLANA: ChainFamily.SOLANA,
}

# Native coin symbol and base-unit decimals (satoshi, wei, sun, lamport).
_NATIVE = {
    Chain.BITCOIN: ("BTC", 8),
    Chain.ETHEREUM: ("ETH", 18),
    Chain.BSC: ("BNB", 18),
    Chain.POLYGON: ("POL", 18),
    Chain.ARBITRUM: ("ETH", 18),
    Chain.BASE: ("ETH", 18),
    Chain.OPTIMISM: ("ETH", 18),
    Chain.AVALANCHE: ("AVAX", 18),
    Chain.TRON: ("TRX", 6),
    Chain.SOLANA: ("SOL", 9),
}

_DISPLAY = {
    Chain.BITCOIN: "Bitcoin",
    Chain.ETHEREUM: "Ethereum",
    Chain.BSC: "BNB Smart Chain",
    Chain.POLYGON: "Polygon PoS",
    Chain.ARBITRUM: "Arbitrum One",
    Chain.BASE: "Base",
    Chain.OPTIMISM: "OP Mainnet",
    Chain.AVALANCHE: "Avalanche C-Chain",
    Chain.TRON: "Tron",
    Chain.SOLANA: "Solana",
}

# Etherscan API V2 chain ids.
EVM_CHAIN_IDS = {
    Chain.ETHEREUM: 1,
    Chain.BSC: 56,
    Chain.POLYGON: 137,
    Chain.ARBITRUM: 42161,
    Chain.BASE: 8453,
    Chain.OPTIMISM: 10,
    Chain.AVALANCHE: 43114,
}

# Chains on Etherscan's free API tier, per https://docs.etherscan.io/supported-chains (checked 2026-09-30).
ETHERSCAN_FREE_TIER = frozenset({Chain.ETHEREUM, Chain.POLYGON, Chain.ARBITRUM})

_EXPLORER = {
    Chain.BITCOIN: ("https://blockstream.info/tx/{}", "https://blockstream.info/address/{}"),
    Chain.ETHEREUM: ("https://etherscan.io/tx/{}", "https://etherscan.io/address/{}"),
    Chain.BSC: ("https://bscscan.com/tx/{}", "https://bscscan.com/address/{}"),
    Chain.POLYGON: ("https://polygonscan.com/tx/{}", "https://polygonscan.com/address/{}"),
    Chain.ARBITRUM: ("https://arbiscan.io/tx/{}", "https://arbiscan.io/address/{}"),
    Chain.BASE: ("https://basescan.org/tx/{}", "https://basescan.org/address/{}"),
    Chain.OPTIMISM: ("https://optimistic.etherscan.io/tx/{}", "https://optimistic.etherscan.io/address/{}"),
    Chain.AVALANCHE: ("https://snowtrace.io/tx/{}", "https://snowtrace.io/address/{}"),
    Chain.TRON: ("https://tronscan.org/#/transaction/{}", "https://tronscan.org/#/address/{}"),
    Chain.SOLANA: ("https://solscan.io/tx/{}", "https://solscan.io/account/{}"),
}
