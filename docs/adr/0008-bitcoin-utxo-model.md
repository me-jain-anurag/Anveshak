# ADR-0008: Bitcoin — follow every output, cluster by co-spend only, stop at CoinJoin

- Status: accepted
- Date: 2026-09-30

## Context

Bitcoin transactions have many inputs and outputs. Common heuristics guess which output is
"change". Those guesses are often wrong (Kappos et al. 2022), and a wrong guess silently
redirects an investigation.

## Decision

- **No change-address heuristic.** From address A, every output of a transaction A spends becomes
  a transfer A → output, carrying that output's exact value. Change simply appears as one more hop.
- Incoming: each distinct input address B pays A the value of the output to A. This is an upper
  bound on B's contribution, stated as such.
- **Co-spend (common-input-ownership) is used in only two places:**
  - `D-COSPEND`: an unlabelled address spent in the same transaction as an A/B-attributed address
    shares its controller. The grade is lowered one step, never derived from C or X, never chained.
  - `C-MULTI-INPUT` clusters reveal other addresses of the suspect's own wallet.
- **CoinJoin-like transactions** (≥3 distinct input addresses and ≥3 equal-value outputs) disable
  co-spend and end the path as a `coinjoin_like` endpoint. The test is deliberately broad: a false
  positive only stops the trace for manual review, it never creates an attribution.
- Only confirmed transactions are used. Outputs without a standard address are skipped and noted.

## Consequences

- Traces are wider than with change heuristics, but never wrong because of a guessed change output.
- An exchange's consolidation transaction ties a deposit address to the exchange deterministically.
