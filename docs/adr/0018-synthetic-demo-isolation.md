# ADR-0018: Synthetic demo data is isolated and watermarked

- Status: accepted
- Date: 2026-09-30

## Context

A demo must show every rule firing, without real investigations and without fabricating
anything that could pass as evidence.

## Decision

- `anveshak/demo.py` generates fictional entities ("Demo Exchange Alpha"), addresses derived from
  hashes of made-up names (valid in format, so the whole pipeline runs unchanged), labels, and
  transactions. It covers every rule: R-SWEEP, grades A/B/C/X, D-COSPEND, C-MULTI-INPUT, CoinJoin,
  T-PEEL, T-PASS, mixer, dormant funds with an issuer-freeze draft, a funding-side withdrawal, and a
  THORChain Tron→BTC continuation.
- Isolation:
  - synthetic labels carry `synthetic: true` and are refused by the live label loader;
  - demo cases run in `DataMode.SYNTHETIC`, are watermarked in reports and bannered in the dashboard;
  - they can never be approved or submitted;
  - they never feed the watchlist or cross-case sightings.

## Consequences

- The demo is safe to show, share and record.
