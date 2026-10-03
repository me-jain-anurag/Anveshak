# ADR-0022: Wormhole, LayerZero and Across resolvers; THORChain vault labels

- Status: accepted
- Date: 2026-10-04

## Context

PS #4 asks that cross-chain movements through DeFi bridges be handled. ADR-0014 set the rule:
links come only from deterministic identifier matching. THORChain was the first resolver.

## Decision

Three more resolvers implement the same interface. Each declares the source chains it can
look up. Response formats were captured live on 2026-10-03/04 and the tests use those shapes.

| Rule | Protocol | Lookup | Recipient | Not found |
|---|---|---|---|---|
| X-WORMHOLE | Wormhole (Portal, NTT, …) | Wormholescan `/api/v1/operations?txHash=` | `standarizedProperties.toAddress` (decoded from the guardian-signed VAA) | `{"operations": []}` |
| X-LAYERZERO | LayerZero v2 messages (OFTs such as USDT0) | LayerZero Scan `/v1/messages/tx/{hash}` | **from the destination transaction** (see below) | HTTP 404 |
| X-ACROSS | Across Protocol | Across API `/api/deposit?depositTxnRef=` | deposit record `recipient` | HTTP 404 |

**Finding the recipient for LayerZero.** LayerZero payloads are app-specific bytes and are not
decoded.

* The engine fetches the destination transaction (`tx_transfers`). The recipient is the
  **unique** token receiver left after excluding the zero address (mints) and the receiving
  OApp contract.
* Several candidates leave the link "recipient unresolved", with the candidates listed.
* `recipient_basis` on every link says how the recipient was determined.

**Other rules:**

* A 404 "not found" from these APIs is recorded as evidence, like any response
  (`Fetcher.get(..., accept=(404,))`). A replay therefore reproduces "no link" exactly.
* Every link is then checked on the destination chain (ADR-0014).
  * Solana is a special case: a protocol record may name the token account rather than the
    wallet. A mismatch there is reported as unconfirmed, not as false.
* Chain-id maps come from the protocols' own sources:
  * Wormhole: `ChainID` constants in wormhole-foundation/wormhole `sdk/vaa/structs.go`;
  * LayerZero: chain names observed in LayerZero Scan's live message feed;
  * Across: the Across API `/api/swap/chains`, e.g. Solana 34268394551451, Tron 728126428.
* Across: one transaction can hold several deposits. `pagination.maxIndex` says how many, and
  `index` fetches each one. A refunded deposit gives no outbound link.
* THORChain is tightened: the inbound leg must be a layer-1 deposit of the queried chain's asset
  (`BTC.BTC`, `ETH.USDT-…`), never a trade or synth asset. A test on captured Midgard data
  found this gap.
* **THORChain vault labels:**
  * `anveshak labels import thorchain` reads `thorchain/inbound_addresses` from THORNode and
    writes CURATED BRIDGE labels for each vault and router.
  * Vaults rotate on churn, so each label is dated and earlier vaults are kept: a historical
    transfer went to the vault of its day.
  * These labels make paths end at the bridge, where the resolvers take over.
* Resolvers are enabled with `ANVESHAK_RESOLVERS` (default: all four).

Live checks (2026-10-04):

* Wormhole: Solana → BSC Portal transfer resolved with recipient and destination transaction.
* LayerZero: USDT0 Arbitrum → Celo message resolved to its destination transaction. Celo is not
  traced, so the recipient is reported as unresolved.
* Across: an Arbitrum → Base fill matched the API's recipient and fill transaction.
* THORChain: BTC → TRON.USDT swap.
* Unknown hashes return no link from Wormholescan, LayerZero Scan and the Across API.

Not done: Allbridge, whose API host did not resolve on 2026-10-03. Bridges without a public
lookup API need event-log mining (ABCTRACER-style), which is left for later.

## Consequences

- Four protocols cover a large share of retail bridge traffic. Each link is a pair of
  verifiable facts with the protocol's response kept as evidence.
- Three more public APIs are dependencies. If one is unavailable, the case notes the error;
  the trace itself continues.
