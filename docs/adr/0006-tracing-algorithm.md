# ADR-0006: Time-respecting, hop-limited breadth-first tracing with declared limits

- Status: accepted
- Date: 2026-09-30

## Context

"Nearest VASP" means the fewest hops from the suspect wallet to an address controlled by a
VASP. The search space explodes quickly (busy wallets, exchange hot wallets), and value on a
blockchain is fungible, so no method can say *which* coins moved without assuming a convention.

## Decision

- **Breadth-first by hops**, so the first VASPs found are the nearest.
- **Time order is a hard constraint.** Forward traces follow only transfers at or after the moment
  value arrived at an address. Backward (funding) traces follow only transfers at or before value
  left it. Excluded transfers are counted (`excluded_time_order`).
- **A path stops at a service** (VASP, mixer, bridge, DeFi, gambling, market, issuer), because
  following a service's internal movements would attribute other people's money. It also stops at
  an unlabelled contract / Solana program-derived address, a CoinJoin-like transaction, an address
  too busy to expand (`high_activity`), where value stops moving (`dormant`, with a live balance
  check), at the hop limit, or when the budget runs out. Each case is an explicit `Endpoint` with its full path.
- **Filters are counted, never silent**: dust below a per-asset threshold, tokens not in the verified
  registry (ADR-0007), other assets than the one that arrived, and branches beyond `max_branch`
  (largest kept, recorded in `coverage.pruned`).
- **No taint shares.** Each path reports a *bottleneck*: its smallest transfer, an upper bound on
  what could have moved along it. Haircut/FIFO/poison percentages are not computed, because they
  depend on an arbitrary convention.
- The search is deterministic (ordered by `Transfer.order_key`). The same inputs give the same result.
- Both directions are traced by default. The funding side often reaches the suspect's own KYC'd
  exchange withdrawal, which is frequently the most useful disclosure target.

## Consequences

- A "not found" result comes with a precise statement of what was not examined.
- Busy intermediate addresses end paths early. Analysts can raise budgets, or label the service
  and re-run.
