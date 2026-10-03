# Requirements traceability: SIH 2026 PS 26182

For every clause of the problem statement, this page lists where it is implemented, how it is
verified, and what is still partial.

Status: ✅ done · 🟡 done with a stated limitation · ⏭ next step.

Updated 2026-10-04. The test suite has 150+ tests; run it with `pytest`.

## "The system should"

| # | PS requirement | Implementation | Verified by | Status |
|---|---|---|---|---|
| 1 | Automatically analyse suspect wallet addresses reported on the Sahyog platform | **Sahyog is the client** (ADR-0019). `POST /v1/sahyog/reports`: chain auto-detection by format and checksum, EVM activity probe, queued case, workers, then a signed callback with `anveshak.recommendation/v1` recommendations. `POST /v1/screen` gives an instant label/risk screen | `tests/test_sahyog.py::test_sahyog_end_to_end` (mock Sahyog portal, `tools/mock_sahyog`), `test_service.py::test_ingestion_detects_chains_and_rejects_invalid`, `test_sahyog.py::test_screen_is_synchronous_and_offline` | ✅ |
| 2 | Trace transaction paths to the nearest centralised exchange / custodial wallet service / VASP receiving direct deposits from the suspect wallet | Time-respecting BFS (`tracer.py`, ADR-0006). VASP categories: exchange, custodial wallet, payment processor, ATM. `nearest_vasps` ranking (fewest hops, then confidence). Deposit addresses identified by R-SWEEP / R-CONSOLIDATION | `tests/test_tracer.py`, `test_case.py::test_demo_analysis_outputs`, and the **ground-truth benchmark** of 15 cases from court filings ([benchmark.md](benchmark.md)) | ✅. Benchmark results and their gaps are stated in benchmark.md |
| 3 | Map deposit addresses and flows across Bitcoin, Ethereum, Tron, BNB Chain, Solana, Polygon and other major chains | 10 chains (ADR-0017). BNB Chain, Base, OP and Avalanche need no paid key: a window-limited JSON-RPC log scan (ADR-0021). Any EVM chain can be added with `ANVESHAK_RPC_<CHAIN>` | `test_adapters.py`, `test_solana.py`, `test_rpc.py` (fake node with pruning and range limits). Live: Tron, Bitcoin, Solana; BSC/Base/Avalanche window search; BSC log scan (72 USDT transfers) | 🟡 Without a paid indexer, BNB-chain-family histories are window-limited and cover verified tokens only. Both limits are stated in every report |
| 4a | Identify exchange clusters | `entity:<chain>:<id>` clusters plus Bitcoin `multi_input` clusters (`profiles.py`, ADR-0013) | `test_case.py::test_demo_analysis_outputs` | ✅ |
| 4b | Identify hot wallets | R-SWEEP-TARGET, label roles (R-LABEL-ROLE) | same | ✅ |
| 4c | Identify deposit wallets | R-SWEEP (account chains), R-CONSOLIDATION / D-COSPEND (Bitcoin) | `test_tracer.py::test_service_endpoint_stops_path_and_carries_attribution`, `::test_sweep_role_not_claimed_when_address_also_pays_elsewhere`; benchmark BM-01/03/04/06/08 (OKX deposit addresses) | ✅ |
| 4d | Identify mixers / tumblers | Labels (Tornado Cash, Blender, Sinbad, Wasabi, CoinJoin collectors, research datasets), structural CoinJoin detection, T-MIXER / T-COINJOIN | `test_tracer.py::test_mixer_is_a_service_endpoint`, demo | ✅ |
| 4e | Identify DeFi bridges | BRIDGE labels: THORChain vaults and routers imported from THORNode, dated, history kept (`anveshak labels import thorchain`), plus GraphSense / intel providers. Bridge protocols resolved from their own records: Wormhole, LayerZero, Across, THORChain (ADR-0022) | `test_crosschain.py` (captured live shapes), `test_crosschain.py::test_thorchain_inbound_labels`; live: Wormhole Solana→BSC, LayerZero Arbitrum→Celo, Across Arbitrum→Base | ✅ |
| 4f | Identify cross-chain swap services | X-THORCHAIN, X-WORMHOLE, X-LAYERZERO, X-ACROSS links, each confirmed on the destination chain, then a continuation trace | `test_case.py::test_cross_chain_continuation`, `test_crosschain.py::test_engine_resolves_layerzero_recipient_from_destination_tx` | ✅ |
| 5 | Integrate Sahyog with blockchain-intelligence APIs and graph-analytics engines | Chain APIs (Etherscan V2, TronGrid, Esplora, Solana RPC, JSON-RPC), bridge APIs (Midgard, Wormholescan, LayerZero Scan, Across), intel providers (Chainalysis sanctions, Etherscan name tags), Neo4j Cypher and GraphML exports | `test_units.py::test_chainalysis_provider_parses_and_is_strict`, `::test_etherscan_nametag_provider`, `::test_exports_cover_transfers_and_cross_chain` | ✅ |
| 6 | Automated tagging and confidence scoring for suspected VASPs | Tags per address. Grades A/B/C/X (incl. G-N1 denials, ADR-0020). **Confidence 0–100** points rubric (`scoring.py`, `data/scoring_policy.yaml`, ADR-0012), itemised | `test_units.py::test_confidence_points_are_itemised_and_sum`, `test_attribution.py` (G-N1 cases); benchmark compares confidence of correct and other attributions | ✅ |
| 7 | Generate investigation-ready reports for LEAs | Template-only HTML report (sealed with SHA-256): summary, nearest VASPs, confidence breakdowns, endpoints grouped by address, paths with per-transfer verification, attribution evidence, typologies, clusters, risk, coverage (incl. window-limited histories), drafts, evidence manifest, BSA s.63 certificate template | `test_case.py::test_report_is_watermarked_and_has_no_probabilities`, `test_api.py` | ✅ (PDF by printing the HTML) |
| 8 | Assist routing lawful disclosure or freezing requests to the correct VASP through Sahyog | DISCLOSURE / FREEZE / ISSUER_FREEZE recommendations. Each carries Sahyog's intermediary id (exact match, never guessed) or verified alternative channels. The officer approves inside Sahyog; outcomes are reported back append-only; replies become grade-A confirmations or G-N1 denials; re-runs | `test_sahyog.py::test_sahyog_end_to_end`, `::test_denial_reply_removes_attribution`, `test_case.py::test_ready_requires_directory_channel` | ✅. Sahyog's own intermediary list must be uploaded by its operator |

## "The system may additionally support"

| PS item | Implementation | Status |
|---|---|---|
| Visualisation of fund movement | Dashboard graph (Cytoscape.js, vendored so it works offline) with cross-chain edges; Neo4j/GraphML exports | ✅ |
| Cross-chain transaction mapping | Four protocols with deterministic identifier matching (ADR-0014, ADR-0022) | ✅. Bridges without a public lookup API are future work |
| Risk scoring | Wallet and flow risk, points rubric (`risk.py`) | ✅ |
| Identification of laundering typologies | T-PEEL, T-PASS, T-FANOUT, T-FANIN, T-MIXER, T-COINJOIN, T-CHAINHOP (`typologies.py`) | ✅ |
| Alerting for high-risk wallets (ransomware, darknet, terrorism financing, fraud) | Case alerts: A-SANCTIONS, A-RANSOMWARE, A-DARKWEB, A-TERRORISM, A-FRAUD, A-HIGH-RISK-WALLET, A-FREEZE-OPPORTUNITY, A-FUNDS-HELD. Watchlist alert A-WATCH-MOVEMENT. Cross-case alert A-CROSS-CASE, anonymised across agencies | 🟡 Terrorism-financing flags come only from labels that carry them. No dedicated TF dataset is shipped |

## "Expected solution"

| PS item | Implementation | Status |
|---|---|---|
| Automated identification of nearest VASP/exchange linked to unknown wallets | `nearest_vasps` per subject and direction, in the API, recommendations, dashboard, report and callbacks; benchmark | ✅ |
| API-driven blockchain tracing and attribution support | REST API (`api.py`) with per-client keys, roles and agency scope (ADR-0023); OpenAPI at `/docs`; guide in [sahyog-integration.md](sahyog-integration.md) | ✅ |
| Multi-chain transaction analysis and visualisation | 10 chains, four cross-chain protocols, continuation traces, graph | ✅ |
| Real-time generation of investigative intelligence | Queue and workers; watchlist monitor with movement alerts and automatic follow-up traces; cross-case alerts; signed callbacks; synchronous `/v1/screen` | 🟡 The watchlist polls (default 10 min). Push monitoring from node websockets is deferred (ADR-0015): it needs self-hosted nodes per chain |
| Risk classification of wallets and transaction flows | `subject_risks`, `flow_risks` | ✅ |
| Dashboard for LEAs with case-based analytics and reporting | Dashboard tabs: Cases (recommendations with Sahyog ids, outcome history, re-run), Sahyog intake, Alerts, Watchlist, Analytics (incl. outcomes per intermediary), Lookup and bulk screen. Works with per-client keys | ✅ |
| Scalable architecture for large-volume analysis | Persistent queue, horizontally scalable workers, Docker image and compose, CI (tests and Docker build). Path to PostgreSQL, object storage and self-hosted nodes documented (ADR-0015) | 🟡 Single-host SQLite queue today. PostgreSQL is a documented swap, not built |

## Aims

| Aim | How the system serves it |
|---|---|
| Reduce investigation time | One call from Sahyog returns traced, verified, graded recommendations with draft text. Chain auto-detection. Instant screening. |
| Improve asset-freezing efficiency | Dormant-funds detection with live balance; issuer-freeze recommendations (Tether / T3 FCU); VASP freeze drafts; watchlist alerts the moment funds move |
| Enhance attribution | Source-trust-checked grades, itemised confidence, deposit-address detection, co-spend and EVM key derivations, and a feedback loop where replies confirm (G-A1) or deny (G-N1) attributions |
| Strengthen cross-border investigations | Directory of foreign VASP law-enforcement channels as alternatives when an intermediary is not on Sahyog; cross-chain links; FIU-IND registration facts (secondary sources marked) |

## Robustness requirements (team)

| Requirement | Where |
|---|---|
| No ML / LLM in the evidence path; no probabilities | ADR-0002, ADR-0011, ADR-0012 |
| Every fact re-verifiable; every claim sourced; every inference rule-named | ADR-0003, ADR-0009; benchmark cases carry document hashes and verbatim quotes |
| Reproducible results | Findings hash plus replay (ADR-0004), including recorded 404 "not found" answers. Tests: `test_live_run_replays_to_identical_findings_hash`, `test_rpc.py::test_engine_requires_since_and_replays_exactly`, `test_crosschain.py::test_layerzero_404_is_no_link_and_replays` |
| Fail closed on bad input or bad data | Checksum validation, strict provider parsing, EvidenceCorrupted, MISMATCH gives 0 and BLOCKED, body-size cap |
| Accountable access | Per-client keys (hash only), roles, agency scoping, IP allowlists, rate limits, hash-chained append-only audit log (ADR-0023) |
| Honest about gaps | Coverage section per trace; window-limited histories declared; benchmark lists skipped and missed cases; this matrix |

## Next steps (ordered)

1. Get I4C's Sahyog client specification (mTLS/OAuth details, callback format preferences) and
   adapt the proxy configuration.
2. Add Tron USDT benchmark cases from documents that name a deposit address at a specific exchange.
3. Grow Indian VASP coverage:
   * official domains and law-enforcement channels, checked on primary sources;
   * labels, through Sahyog reply attestations.
4. PostgreSQL queue for multi-host deployments (ADR-0015).
5. Self-hosted nodes and indexers for full BNB-chain histories and push monitoring.
