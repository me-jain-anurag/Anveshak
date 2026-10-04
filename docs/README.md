# Documentation

| Document | What it covers |
|---|---|
| [usage.md](usage.md) | user guide: install, concepts, CLI, dashboard, API, deployment, configuration, troubleshooting |
| [requirements-traceability.md](requirements-traceability.md) | every PS 26182 clause → implementation → test → status |
| [architecture.md](architecture.md) | pipeline, modules, security, running, known limitations |
| [sahyog-integration.md](sahyog-integration.md) | API contract for the Sahyog portal (Sahyog is the client): reports, recommendations, outcomes, replies, screening, keys and scope |
| [benchmark.md](benchmark.md) | ground-truth benchmark from public court filings: cases, scoring, results, limits |
| [references.md](references.md) | every paper, dataset, API, standard and source used, where it is used, and what was rejected |
| [adr/](adr/) | architecture decision records |

## Decision records

| ADR | Decision |
|---|---|
| [0001](adr/0001-record-architecture-decisions.md) | Record architecture decisions |
| [0002](adr/0002-no-probabilistic-scores.md) | No ML / LLM in the evidence path; grades and points rubrics, not probabilities |
| [0003](adr/0003-facts-claims-inferences.md) | Keep facts, claims and inferences apart |
| [0004](adr/0004-evidence-store-and-replay.md) | Content-addressed evidence store; replay reproduces the findings hash |
| [0005](adr/0005-label-sources-and-trust.md) | Label sources, source classes and the source-trust check |
| [0006](adr/0006-tracing-algorithm.md) | Time-respecting, hop-limited BFS; declared limits; bottleneck instead of taint |
| [0007](adr/0007-asset-identity.md) | Tokens identified by contract from a verified registry |
| [0008](adr/0008-bitcoin-utxo-model.md) | Bitcoin: follow every output; co-spend only; stop at CoinJoin |
| [0009](adr/0009-independent-verification.md) | Re-verify every transfer a reported path rests on |
| [0010](adr/0010-human-in-the-loop-routing.md) | Drafts with officer approval; Sahyog gateway adapter |
| [0011](adr/0011-template-only-reports.md) | Template-only reports; hashes; BSA s.63 certificate template |
| [0012](adr/0012-scoring-policy.md) | Confidence and risk scores as versioned points rubrics |
| [0013](adr/0013-typologies-roles-clusters.md) | Typologies, roles, clusters, tags as fixed rules |
| [0014](adr/0014-cross-chain-links.md) | Cross-chain links only from deterministic identifier matching |
| [0015](adr/0015-scaling.md) | Queue, workers, monitor; path beyond one host |
| [0016](adr/0016-intel-providers-and-graph-exports.md) | Intelligence APIs as label providers; Neo4j / GraphML exports |
| [0017](adr/0017-chain-coverage-and-data-sources.md) | Chain coverage and data sources |
| [0018](adr/0018-synthetic-demo-isolation.md) | Synthetic demo isolated and watermarked |
| [0019](adr/0019-sahyog-is-the-client.md) | Sahyog is the client: recommendations out, outcomes and replies back in |
| [0020](adr/0020-formal-denials.md) | A VASP's formal denial overrides other labels (G-N1) |
| [0021](adr/0021-keyless-evm-rpc-log-scan.md) | EVM chains without a paid indexer: window-limited JSON-RPC log scan |
| [0022](adr/0022-bridge-resolvers.md) | Wormhole, LayerZero and Across resolvers; THORChain vault labels |
| [0023](adr/0023-api-clients-scoping-audit.md) | Per-client keys, roles, agency scoping, limits, append-only audit log |
| [0024](adr/0024-cospend-with-busy-wallet-stops-trace.md) | Co-spend with a high-activity address stops the trace (R-COSPEND-SERVICE; from benchmark BM-10) |
| [0025](adr/0025-replay-uses-recorded-source-config.md) | Replay uses the recorded data-source configuration; endpoint credentials are redacted |
| [0026](adr/0026-busy-account-stops-trace.md) | A busy account stops the trace on EVM chains (R-BUSY-ACCOUNT; from benchmark BM-04/BM-07) |
