# ADR-0003: Keep facts, claims and inferences apart

- Status: accepted
- Date: 2026-09-30

## Context

Blockchain analytics tools commonly show "Wallet X → Binance" as if it were one fact. In
reality it combines a ledger fact (a transfer happened), a third-party claim (some dataset
says the receiving address is Binance's) and an inference (the path makes Binance the nearest
VASP). When these are merged, a reader cannot tell which part to challenge.

## Decision

The domain model (`anveshak/domain.py`) has three separate kinds of statement:

| Kind | Type | Property |
|---|---|---|
| Fact | `Transfer` | what a ledger records; carries `evidence_id` (sha256 of the raw response) and is re-verified (ADR-0009) |
| Claim | `Label` | who says an address belongs to X or is risky; carries `source_id`, `source_class`, `primary_source`, `dataset_ref` |
| Inference | `Attribution`, `Endpoint`, `ConfidenceScore`, `TypologyHit`, `RoleTag`, `Cluster`, `CrossChainLink` | a named rule applied to facts and claims; carries the rule id and its inputs |

Models are immutable (pydantic `frozen=True`, `extra="forbid"`). An `Attribution` must rest on
labels or on a derivation (validator), and grade X must list the conflicting entities.

## Consequences

- Reports present the three kinds separately, so each part can be verified or challenged on its own.
- Amounts are integers in base units (satoshi, wei, sun, lamport). Floats never touch value arithmetic.
