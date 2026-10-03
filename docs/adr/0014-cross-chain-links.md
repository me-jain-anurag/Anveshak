# ADR-0014: Cross-chain links only from deterministic identifier matching

- Status: accepted (extended by ADR-0022)
- Date: 2026-09-30

## Context

Launderers move value across chains (chain hopping) through bridges and swap services. The
literature (SoK: Cross-Chain Transaction Identification and Matching, 2026) distinguishes three
approaches: deterministic identifier matching, field-constraint heuristics, and model-assisted matching.

## Decision

- A `CrossChainLink` is created **only** from a protocol's own record that names both the inbound
  and outbound transaction. Nothing is matched by amount or time.
- First resolver: **THORChain via Midgard** (`/v2/actions?txid=`), rule `X-THORCHAIN`. Internal legs
  (THOR.RUNE, trade/secured assets with `~`) are skipped. Destination chains outside our coverage are
  reported but not followed.
- Every link is then **confirmed on the destination chain**: the outbound transaction must appear
  paying the stated address. `destination_confirmed` is true, false, or null with a reason. A link
  that is confirmed false is never followed.
- Resolvers are consulted for paths ending at a service, an unlabelled contract, or a high-activity
  address (THORChain vaults and routers look like these).
- Confirmed links start a **continuation trace** on the destination chain (`since` = time of the
  inbound transfer, remaining hop budget, depth limit `cross_chain_depth`, default 1). The drafts it
  produces say how value got there (`RoutingDecision.via`).
- Verified live on 2026-09-30: a real BTC → TRON.USDT swap resolved, and its 2,209.939306 USDT outbound confirmed via TronGrid.

## More resolvers (same interface)

Implemented in ADR-0022 (2026-10-04): Wormhole (Wormholescan), LayerZero (LayerZero Scan; recipient
resolved from the destination transaction) and Across (Across API), plus dated THORChain vault labels.
Bridges without a public lookup API remain future work (event-log mining in the style of ABCTRACER).
Every format is verified live before shipping, as was done for Midgard.

## Consequences

- Cross-chain coverage grows one protocol at a time, but every link is a verifiable fact pair.
