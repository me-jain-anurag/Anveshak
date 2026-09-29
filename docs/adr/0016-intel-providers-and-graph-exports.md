# ADR-0016: Blockchain-intelligence APIs as label providers; graph-engine exports

- Status: accepted
- Date: 2026-09-30

## Context

The PS asks to "integrate Sahyog with blockchain intelligence APIs and graph analytics engines".
Commercial intelligence (Chainalysis, TRM, Elliptic, Arkham) is powerful but closed, and its
answers must not bypass our grading.

## Decision

- **Providers** (`intel.py`) implement `lookup(chain, address) -> list[Label]`. They are called
  lazily by the attributor for each address the tracer touches, cached per case, and fetched through
  the evidence store (so answers are hashed, stored, replayable and cited as `evidence:<sha256>`).
- Provider labels are graded **exactly like any other label**: a vendor is a curated source unless
  it cites an allow-listed authority (source-trust check, ADR-0005).
- Parsing is **strict**: an unexpected response shape raises `SourceError`. It becomes a coverage
  gap, never a label.
- Shipped: Chainalysis free sanctions API (`CHAINALYSIS_API_KEY`) and Etherscan name tags
  (`ETHERSCAN_NAMETAGS=1`, Pro Plus). Chain data APIs (Etherscan V2, TronGrid, Esplora, Solana
  RPC, THORChain Midgard) are the other intelligence integrations.
- **Graph engines**: `GET /v1/cases/{id}/export/neo4j` (idempotent Cypher MERGE script, including
  cross-chain edges and tags) and `/export/graphml` (Gephi, Cytoscape desktop, yEd), plus
  `anveshak export`. The dashboard renders the same graph with Cytoscape.js.

## Consequences

- Adding a vendor means writing a small parser and a test. The scoring and grading code does not change.
