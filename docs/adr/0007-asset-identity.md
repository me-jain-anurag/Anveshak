# ADR-0007: Identify tokens by contract address, from a verified registry

- Status: accepted
- Date: 2026-09-30

## Context

Anyone can deploy a token called "USDT". Scammers use fake stablecoins and address-poisoning
dust to pollute histories. A live Tron address we tested had received tokens named
"unfreeze?" and "TG: jieuu?". Decimals also differ by chain: USDT on BNB Chain has 18, not 6.

## Decision

- `data/assets.yaml` lists verified (chain, contract) pairs with symbol, decimals, issuer, dust
  threshold, and the source each was checked against (CoinGecko coin API, 2026-09-30). Entries
  must not be added from memory.
- Only registry tokens are `verified` and followed by default. Any other token keeps its reported
  symbol with a trailing `?` (e.g. `USDT?`), so it can never pass for the real thing. Such
  transfers are counted as excluded unless `include_unverified_assets` is set.
- The issuer field drives issuer-freeze routing. USDT0 on Polygon/Arbitrum and Binance-bridged USDC
  have `issuer: null`, because we could not confirm who can freeze them.

## Consequences

- New tokens need a deliberate, sourced registry change.
- Unverified-token flows are visible in coverage but never become paths or evidence.
