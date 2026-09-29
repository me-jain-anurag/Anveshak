# Requirements traceability — SIH 2026 PS 26182

Every clause of the problem statement, where it is implemented, how it is verified, and what is
still partial. Status: ✅ done · 🟡 done with a stated limitation · ⏭ next step.

## "The system should"

| # | PS requirement | Implementation | Verified by | Status |
|---|---|---|---|---|
| 1 | Automatically analyse suspect wallet addresses reported on the Sahyog platform | `POST /v1/sahyog/reports`: wallets → chain auto-detection by format and checksum (`addresses.detect_chains`) → EVM activity probe → queued case → workers → signed callback to an allow-listed Sahyog host (`service.py`) | `tests/test_service.py::test_ingestion_detects_chains_and_rejects_invalid`, `::test_callback_refused_unless_allow_listed` | 🟡 Our side of the contract is complete. I4C's Sahyog API spec is not public, so submission goes through a dry-run gateway (ADR-0010) |
| 2 | Trace transaction paths to the nearest centralised exchange / custodial wallet service / VASP receiving direct deposits from the suspect wallet | Time-respecting BFS (`tracer.py`, ADR-0006). VASP categories exchange, custodial wallet, payment processor, ATM. `nearest_vasps` ranking (fewest hops, then confidence). Deposit address identified by R-SWEEP / R-CONSOLIDATION | `tests/test_tracer.py`, `tests/test_case.py::test_demo_analysis_outputs`, live runs on Tron and Bitcoin | ✅ |
| 3 | Map deposit addresses and flows across Bitcoin, Ethereum, Tron, BNB Chain, Solana, Polygon and other major chains | 10 chains: Bitcoin, Ethereum, BNB Chain, Polygon, Arbitrum, Base, OP Mainnet, Avalanche, Tron, Solana (ADR-0017) | adapter tests on real response shapes (`test_adapters.py`, `test_solana.py`); live checks on Tron, Bitcoin, Solana RPC, Midgard | 🟡 BNB Chain, Base, OP and Avalanche need a paid Etherscan plan or a compatible provider (checked 2026-09-30) |
| 4a | Identify exchange clusters | `entity:<chain>:<id>` clusters plus Bitcoin `multi_input` clusters (`profiles.py`, ADR-0013) | `test_case.py::test_demo_analysis_outputs` | ✅ |
| 4b | Identify hot wallets | R-SWEEP-TARGET, label roles (R-LABEL-ROLE) | same | ✅ |
| 4c | Identify deposit wallets | R-SWEEP (account chains), R-CONSOLIDATION / D-COSPEND (Bitcoin) | `test_tracer.py::test_service_endpoint_stops_path_and_carries_attribution`, `::test_sweep_role_not_claimed_when_address_also_pays_elsewhere` | ✅ |
| 4d | Identify mixers / tumblers | Labels (Tornado Cash, Blender, Sinbad, Wasabi, CoinJoin collectors, research datasets), structural CoinJoin detection, T-MIXER / T-COINJOIN | `test_tracer.py::test_mixer_is_a_service_endpoint`, demo | ✅ |
| 4e | Identify DeFi bridges | BRIDGE category from labels and intel providers; THORChain via its own records; unlabelled contracts and program-derived addresses flagged for review | `test_case.py::test_cross_chain_continuation` | 🟡 Bridge *label* coverage is thin. Wormhole / LayerZero resolvers are next (ADR-0014) |
| 4f | Identify cross-chain swap services | THORChain resolver (`X-THORCHAIN`) with destination confirmation and continuation trace | live: BTC → TRON.USDT swap resolved and confirmed (2026-09-30) | ✅ |
| 5 | Integrate Sahyog with blockchain-intelligence APIs and graph-analytics engines | Chain APIs (Etherscan V2, TronGrid, Esplora, Solana RPC, Midgard), intel providers (Chainalysis sanctions, Etherscan name tags; `intel.py`), Neo4j Cypher and GraphML exports (`exports.py`) | `test_units.py::test_chainalysis_provider_parses_and_is_strict`, `::test_etherscan_nametag_provider`, `::test_exports_cover_transfers_and_cross_chain` | ✅ |
| 6 | Automated tagging and confidence scoring for suspected VASPs | Tags per address (`profiles.py`). Grades A/B/C/X (`attribution.py`). **Confidence 0–100** points rubric (`scoring.py`, `data/scoring_policy.yaml`, ADR-0012), itemised | `test_units.py::test_confidence_points_are_itemised_and_sum`, `::test_hard_rules_zero_the_score`, `test_case.py::test_confidence_items_sum_to_score` | ✅ |
| 7 | Generate investigation-ready reports for LEAs | Template-only HTML report: summary, nearest VASPs, confidence breakdowns, paths with per-transfer verification, attribution evidence, typologies, clusters, risk, coverage, drafts, evidence manifest, BSA s.63 certificate template. SHA-256 sidecar. JSON findings (ADR-0011) | `test_case.py::test_report_is_watermarked_and_has_no_probabilities`, `test_api.py` | ✅ (PDF by printing the HTML) |
| 8 | Assist routing lawful disclosure or freezing requests to the correct VASP through Sahyog | DISCLOSURE and FREEZE drafts per VASP, ISSUER_FREEZE for stablecoins. Status rules. VASP directory with sourced contact channels. Officer approval. Gateway adapter (ADR-0010) | `test_case.py::test_demo_scenario_outcomes`, `::test_verification_mismatch_blocks_routing`, `::test_ready_requires_directory_channel` | 🟡 Dry-run gateway until the Sahyog spec is available |

## "The system may additionally support"

| PS item | Implementation | Status |
|---|---|---|
| Visualisation of fund movement | Dashboard graph (Cytoscape.js) with cross-chain edges; Neo4j/GraphML exports | ✅ |
| Cross-chain transaction mapping | ADR-0014 (THORChain now; same interface for more) | 🟡 one protocol today |
| Risk scoring | Wallet and flow risk, points rubric (`risk.py`) | ✅ |
| Identification of laundering typologies | T-PEEL, T-PASS, T-FANOUT, T-FANIN, T-MIXER, T-COINJOIN, T-CHAINHOP (`typologies.py`) | ✅ |
| Alerting for high-risk wallets (ransomware, darknet, terrorism financing, fraud) | Case alerts A-SANCTIONS / A-RANSOMWARE / A-DARKWEB / A-TERRORISM / A-FRAUD / A-HIGH-RISK-WALLET / A-FREEZE-OPPORTUNITY (issuer-freezable tokens only) / A-FUNDS-HELD; watchlist A-WATCH-MOVEMENT; cross-case A-CROSS-CASE (`risk.py`, `service.py`) | 🟡 terrorism-financing flags come only from labels that carry them (e.g. OFAC designations are flagged as sanctioned). No dedicated TF dataset is shipped |

## "Expected solution"

| PS item | Implementation | Status |
|---|---|---|
| Automated identification of nearest VASP/exchange linked to unknown wallets | `nearest_vasps` per subject and direction, in the API, dashboard, report and callbacks | ✅ |
| API-driven blockchain tracing and attribution support | REST API (`api.py`), OpenAPI at `/docs`; guide in `docs/sahyog-integration.md` | ✅ |
| Multi-chain transaction analysis and visualisation | 10 chains, cross-chain continuation, graph | ✅ |
| Real-time generation of investigative intelligence | Queue and workers; watchlist monitor with movement alerts and automatic follow-up traces; cross-case alerts; callbacks | 🟡 polling (default 10 min), not a streaming node feed (ADR-0015) |
| Risk classification of wallets and transaction flows | `subject_risks`, `flow_risks` | ✅ |
| Dashboard for LEAs with case-based analytics and reporting | Dashboard tabs (Cases, Sahyog intake, Alerts, Watchlist, Analytics, Lookup); `/v1/analytics` | ✅ |
| Scalable architecture for large-volume analysis | Persistent queue, horizontally scalable workers, Docker compose; documented path to Postgres, object storage and self-hosted nodes (ADR-0015) | 🟡 single-host queue today |

## Aims

| Aim | How the system serves it |
|---|---|
| Reduce investigation time | One call from Sahyog to traced, verified, graded, drafted requests. Chain auto-detection. Nearest-VASP answer. |
| Improve asset-freezing efficiency | Dormant-funds detection with live balance; issuer-freeze drafts (Tether/T3 FCU); VASP freeze drafts; watchlist alerts the moment funds move |
| Enhance attribution | Source-trust-checked grades, itemised confidence, deposit-address detection, co-spend and EVM key derivations, attestation feedback loop |
| Strengthen cross-border investigations | Directory of foreign VASP LEA channels (Binance portal, Kodex, Crypto.com); cross-chain links; FIU-IND registration facts |

## Robustness requirements (team)

| Requirement | Where |
|---|---|
| No ML / LLM in the evidence path; no probabilities | ADR-0002, ADR-0011, ADR-0012 |
| Every fact re-verifiable; every claim sourced; every inference rule-named | ADR-0003, ADR-0009 |
| Reproducible results | findings hash plus replay (ADR-0004); test `test_live_run_replays_to_identical_findings_hash` |
| Fail closed on bad input or bad data | checksum validation, strict provider parsing, EvidenceCorrupted, MISMATCH gives 0 and BLOCKED |
| Honest about gaps | coverage section per trace; this matrix |

## Next steps (ordered)

1. Obtain the Sahyog API specification from I4C and replace `DryRunSahyogGateway`.
2. Add a BNB Chain data source that doesn't need a paid Etherscan plan (a compatible provider or a node).
3. Wormhole and LayerZero resolvers (ADR-0014), each verified live first.
4. Grow Indian VASP label coverage through attestations from Sahyog replies (`anveshak labels attest`).
5. Build and run the Docker images; PostgreSQL queue for multi-host.
