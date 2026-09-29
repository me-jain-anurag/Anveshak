# ADR-0017: Chain coverage and data sources — public APIs first, behind one interface

- Status: accepted
- Date: 2026-09-30

## Context

The PS names Bitcoin, Ethereum, Tron, BNB Chain, Solana, Polygon "and other major chains". Running
full nodes for all of them is out of scope for a first deployment.

## Decision

- One interface, `ChainSource`: `history`, `verify`, `is_contract`, `balance`.
- Adapters and sources (formats verified against live responses on 2026-09-30):

| Chains | Adapter | Source | Note |
|---|---|---|---|
| Ethereum, Polygon, Arbitrum | `EtherscanSource` | Etherscan API V2, one key | free tier |
| BNB Chain, Base, OP Mainnet, Avalanche | `EtherscanSource` | Etherscan API V2 | paid plan or compatible provider (`ETHERSCAN_BASE_URL`) |
| Tron | `TronGridSource` | TronGrid v1 + full-node API | key optional |
| Bitcoin | `EsploraSource` | blockstream.info Esplora | no key |
| Solana | `SolanaRpcSource` | any JSON-RPC endpoint | public endpoint rate-limited; provider URL recommended |

- **Address validation is strict and checksum-based**: Base58Check, bech32/bech32m, EIP-55. Solana
  addresses have no checksum, so a 32-byte decode is the only check. This is documented and the UI
  asks for confirmation. `detect_chains()` decides candidate chains by format alone. Formats cannot collide.
- Solana specifics: incoming SPL tokens arrive at token accounts, so the adapter enumerates the
  wallet's token accounts. Program-derived (off-curve) addresses are treated like contracts.
- Not covered yet (documented): TRC-10 tokens, TRX moved by contract internal calls, EVM internal
  transfers are listed but marked unverifiable, and chains not listed above (Litecoin, TON, XRP …).
  THORChain links to unsupported chains are reported but not followed.

## Consequences

- Coverage and rate limits depend on third-party APIs. Swapping to self-hosted sources is an adapter change (ADR-0015).
