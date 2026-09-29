# ADR-0013: Typologies, address roles, clusters and tags as fixed rules

- Status: accepted
- Date: 2026-09-30

## Context

The PS asks for identification of exchange clusters, hot wallets, deposit wallets, mixers,
bridges, laundering typologies, and automated tagging.

## Decision

All of these are **rules with explicit thresholds** (from `data/scoring_policy.yaml` where tunable).
Each result lists the addresses and transactions that satisfy it.

- **Typologies** (`typologies.py`): T-PEEL, T-PASS, T-FANOUT, T-FANIN, T-MIXER, T-COINJOIN,
  T-CHAINHOP. References: FATF (2020) red-flag indicators; Kappos et al. (peel chains).
  Detectors see only the traced subgraph, so a hit is conclusive for the pattern while a miss means
  "not observed".
- **Roles** (`profiles.py`):
  - DEPOSIT_ADDRESS (R-SWEEP on account chains; R-CONSOLIDATION on Bitcoin)
  - HOT_WALLET (R-SWEEP-TARGET, or the label says so)
  - COLD/RESERVE_WALLET (label text)
  - SERVICE_ADDRESS, CONTRACT, TRANSIT (T-PASS), HOLDING, ORIGIN, HIGH_ACTIVITY
- **Clusters**:
  - `entity:<chain>:<id>`: addresses attributed to one entity, plus deposit addresses tied to it by rule.
  - `multi_input:<hash>`: Bitcoin co-spend clusters (non-CoinJoin). The one containing the subject
    reveals the suspect's other addresses.
- **Tags**: machine-readable strings per address (`entity:binance`, `grade:A`, `role:deposit_address`,
  `risk:sanctioned`, `typology:peel_chain`, `cluster:multi_input:subject`, …), exported to graph
  engines (ADR-0016).
- **Mixers**: tracing stops at a mixer and flags it (T-MIXER, alert, risk). Probabilistic
  deposit/withdrawal linking (Tutela-style) is out of scope for evidence (ADR-0002).

## Consequences

- Detection is transparent and cheap. It will miss patterns that need statistical models, and the report says only what was observed.
