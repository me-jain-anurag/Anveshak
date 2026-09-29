# References

Every source this project relied on, and where. "Checked" dates are when the source was
opened and its content confirmed while building. Sources we considered and deliberately did
not use are listed at the end, with the reason.

## 1. Problem context, law and operations (India)

| Source | What we took from it | Used in |
|---|---|---|
| [Bitget onboards on I4C's Sahyog portal (GlobeNewswire, 2025-06-23)](https://www.globenewswire.com/news-release/2025/06/23/3103143/0/en/Bitget-Onboards-on-India-s-I4C-s-Sahyog-Portal-to-Support-Local-Law-Enforcement.html) | Sahyog is an I4C portal for LEA data requests to service providers; requests are reported to be made under s.94 BNSS and s.79(3)(b) IT Act; more than 45 exchanges were onboarded (checked 2026-09-30) | `routing.LEGAL_BASIS_DEFAULT` (marked "verify with legal cell"), `data/vasp_directory.yaml` (Bitget `sahyog_onboarded`) |
| [49 crypto exchanges register with FIU in FY 2024-25 (CAalley)](https://www.caalley.com/news-updates/indian-news/49-crypto-exchanges-register-with-fiu-in-fy-2024-25-report) | VDA service providers register with FIU-IND as PMLA reporting entities | directory design (`fiu_ind_registered` fact) |
| [FIU-registered crypto exchanges in India, 2026 list (CryptoWire, 2026-07-12)](https://www.cryptowire.in/crypto/fiu-registered-crypto-exchanges-india/) | WazirX, CoinDCX, ZebPay, Mudrex, CoinSwitch and Binance listed as FIU-registered. **Secondary source**, so marked "confirm on fiuindia.gov.in" (checked 2026-09-30) | `data/vasp_directory.yaml` |
| [Section 63, Bharatiya Sakshya Adhiniyam 2023: certificate and hash value (GC-ATG)](https://gcatg.org/admissibility-electronic-evidence-sec-63-bsa/) · [text on Indian Kanoon](https://indiankanoon.org/doc/125020475/) | Electronic evidence needs a two-part certificate and hash values | report section 8 (certificate template), `.html.sha256` sidecar, ADR-0011 |
| [Binance Government Law Enforcement Request System](https://www.binance.com/en/support/law-enforcement) | Binance's LEA request channel | directory channel |
| [Kodex: Coinbase customer story](https://www.kodexglobal.com/customer-stories/coinbase) | Coinbase handles LEA requests through Kodex | directory channel |
| [Crypto.com: how LEAs get in touch](https://help.crypto.com/en/articles/1360625-how-can-law-enforcement-agencies-get-in-touch-with-crypto-com) | Crypto.com's LEA contact | directory channel |
| [T3 Financial Crime Unit](https://t3fcu.org/) · [The Block, 2026-05-14: T3 FCU tops $450M frozen](https://www.theblock.co/news/regulation/2026-05-14-tether-tron-trm-labs-freeze-over-450-million-in-illicit-crypto-assets-401252) | Tether/TRON/TRM unit that freezes illicit USDT on TRON | issuer-freeze routing (`RequestType.ISSUER_FREEZE`), Tether directory entry |
| [Chainalysis 2026 Crypto Crime Report (introduction)](https://www.chainalysis.com/blog/2026-crypto-crime-report-introduction/) | Stablecoins were 84% of illicit volume in 2025 | priority on USDT/USDC and Tron; issuer freeze route |
| [Griffin & Mei, "How Do Crypto Flows Finance Slavery? The Economics of Pig Butchering" (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4742235) | Scam proceeds flow as USDT into exchange deposit accounts | deposit-address detection (R-SWEEP) priority; demo scenario |
| [FATF, 2025 targeted update on VAs/VASPs](https://www.fatf-gafi.org/en/publications/Fatfrecommendations/targeted-update-virtual-assets-vasps-2025.html) · [FATF targeted report on stablecoins and unhosted wallets](https://www.fatf-gafi.org/en/publications/Virtualassets/targeted-report-stablecoins-unhosted-wallets.html) · [FATF virtual-assets publications, incl. the 2020 Red Flag Indicators](https://www.fatf-gafi.org/en/publications/Virtualassets/Virtual-assets.html) | Typologies (layering, pass-through, structuring, mixers, chain-hopping); unhosted-wallet and stablecoin risk | `typologies.py` rule set and references, risk rubric |

## 2. Methods (papers)

| Paper | What we took | Used in |
|---|---|---|
| Meiklejohn et al., [*A Fistful of Bitcoins*](https://cseweb.ucsd.edu/~smeiklejohn/files/imc13.pdf) (IMC 2013) | Common-input-ownership (multi-input) heuristic | `D-COSPEND` (attribution.py), `C-MULTI-INPUT` clusters (profiles.py) |
| Victor, [*Address Clustering Heuristics for Ethereum*](https://www.ifca.ai/fc20/preproceedings/31.pdf) (FC 2020) | Exchange deposit addresses forward funds to hot wallets | `R-SWEEP` (tracer.py), `R-SWEEP-TARGET` role |
| Kappos et al., [*How to Peel a Million*](https://arxiv.org/abs/2205.13882) (USENIX Security 2022) | Peel chains; change-address heuristics are error-prone | `T-PEEL`; ADR-0008 (no change heuristic) |
| Yousaf, Kappos, Meiklejohn, [*Tracing Transactions Across Cryptocurrency Ledgers*](https://arxiv.org/pdf/1810.12786) (USENIX Security 2019) | Cross-ledger tracing is feasible. Amount/time matching gives candidates, not proof | ADR-0014: identifier matching only |
| Zheng et al., [*SoK: Cross-Chain Transaction Identification and Matching*](https://arxiv.org/abs/2608.17532) (2026) | Taxonomy: deterministic identifier matching / field-constraint heuristics / model-assisted | `X-THORCHAIN` is category 1 (deterministic); ADR-0014 |
| Lin et al., [*Track and Trace (ABCTRACER)*](https://arxiv.org/abs/2504.01822) (2025) | Bridge tracing from event logs across 12 bridges | future resolver work (ADR-0014) |
| Stütz et al., [*Reuse of Public Keys Across UTXO and Account-Based Cryptocurrencies*](https://arxiv.org/abs/2601.19500) (FC 2026) | Keys are reused across chains, incl. Tron | `D-EVM-KEY`; future Tron↔EVM key link |
| Möser & Böhme, [*Towards Risk Scoring of Bitcoin Transactions*](https://maltemoeser.de/paper/risk-scoring.pdf); Tironsakkul et al., [taint analysis methods](https://ar5iv.labs.arxiv.org/html/1906.05754) | Poison, haircut, FIFO: taint depends on an arbitrary convention | ADR-0006: report a bottleneck upper bound instead of taint shares |
| Stockinger et al., [*Adoption and Actual Privacy of Decentralized CoinJoin Implementations*](https://arxiv.org/abs/2109.10229) | Structural CoinJoin detection | `is_coinjoin_like` (deliberately broader, stop-only) |
| Wu et al., [*Tutela*](https://arxiv.org/abs/2201.06811) · Wang et al., [*ZKP mixers*](https://arxiv.org/pdf/2201.09035) (WWW 2023) | Mixer deposit/withdrawal linking heuristics exist but are probabilistic | ADR-0013: tracing stops at mixers and flags them |
| Haslhofer et al., [*GraphSense*](https://arxiv.org/abs/2102.13613) | TagPacks: provenance-aware attribution tags | label import and provenance model (ADR-0005) |
| [*Clean Up the Mess: Data Pollution in Cryptocurrency Abuse Reporting*](https://arxiv.org/pdf/2410.21041) (2024) | Crowd-sourced abuse reports are noisy | weak source class; risk flags weighted by source (scoring policy) |

## 3. Data sources and APIs (formats verified against live responses)

| Source | Verified | Used in |
|---|---|---|
| [Etherscan API V2: txlist](https://docs.etherscan.io/api-reference/endpoint/txlist.md), [tokentx](https://docs.etherscan.io/api-reference/endpoint/tokentx.md), [getaddresstag](https://docs.etherscan.io/api-reference/endpoint/getaddresstag.md), [supported chains and free tier](https://docs.etherscan.io/supported-chains) | Docs samples; free tier: Ethereum, Polygon, Arbitrum yes; BSC, Base, OP, Avalanche no (2026-09-30) | `chains/evm.py`, `intel.EtherscanNametags`, `chain.ETHERSCAN_FREE_TIER` |
| TronGrid [`/v1/accounts/{a}/transactions/trc20`, `/transactions`, `/wallet/gettransactioninfobyid`, `/wallet/gettransactionbyid`](https://developers.tron.network/) | Live responses 2026-09-30 (hex↔base58 pairs used as test vectors) | `chains/tron.py`, `tests/test_addresses.py` |
| [Esplora HTTP API](https://github.com/Blockstream/esplora/blob/master/API.md) (blockstream.info) | Live responses 2026-09-30 | `chains/bitcoin.py` |
| Solana JSON-RPC: `getSignaturesForAddress`, `getTransaction` (jsonParsed), `getTokenAccountsByOwner`, `getBalance` | Live responses from mainnet-beta 2026-09-30. Associated-token-account derivation reproduced live accounts exactly | `chains/solana.py`, `tests/test_solana.py` |
| THORChain Midgard [`/v2/actions?txid=`](https://dev.thorchain.org/concepts/querying-thorchain.html) via the public gateway `gateway.liquify.com/chain/thorchain_midgard` · [memo format](https://dev.thorchain.org/concepts/memos.html) | Live 2026-09-30: a BTC→TRON.USDT swap resolved and its outbound transfer of 2,209.939306 USDT confirmed via TronGrid | `crosschain/thorchain.py` |
| [CoinGecko coins API](https://api.coingecko.com/api/v3/coins/tether) (`detail_platforms`, contract lookup) | USDT/USDC contracts and decimals per chain (2026-09-30). Found USDT on BSC has 18 decimals, and Polygon/Arbitrum "USDT" is USDT0 | `data/assets.yaml` |
| [GraphSense TagPacks](https://github.com/graphsense/graphsense-tagpacks) (MIT) · [confidence taxonomy](https://github.com/graphsense/graphsense-tagpack-tool/blob/master/src/tagpack/db/confidence.csv) · [DW-VA taxonomy](https://graphsense.github.io/DW-VA-Taxonomy/) | Pack format, confidence ids, entity/abuse ids | `labels/importers.py` (mapping tables) |
| [0xB10C OFAC sanctioned digital currency addresses](https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses) (MIT) · [OFAC Sanctions List Search](https://sanctionssearch.ofac.treas.gov/) | Nightly JSON lists per ticker | `labels/importers.import_ofac` |
| [Chainalysis sanctions screening API](https://auth-developers.chainalysis.com/sanctions-screening/api-reference/reference/check-if-an-address-is-sanctioned) (`public.chainalysis.com/api/v1/address/…`, `X-API-Key`) | Endpoint and header confirmed via search. The docs page was unavailable, so the response shape is parsed **strictly** and fails closed | `intel.ChainalysisSanctions` |

## 4. Standards

| Standard | Used in |
|---|---|
| [BIP-173 (bech32)](https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki), [BIP-350 (bech32m)](https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki) | `addresses.py` Bitcoin validation. Test vectors in `tests/test_addresses.py` |
| [EIP-55 mixed-case checksum](https://eips.ethereum.org/EIPS/eip-55) | EVM validation. Test vectors |
| [RFC 8032 (EdDSA / ed25519 point decoding)](https://www.rfc-editor.org/rfc/rfc8032) | `ed25519_on_curve`: Solana program-derived-address detection |
| [Solana program derived addresses](https://solana.com/docs/core/pda) | `solana_find_program_address`, associated token accounts |
| IVMS101 / TRISA [global directory](https://trisa.dev/reference/faq/index.html) | Directory data model direction (future: machine-readable VASP identities) |

## 5. Software

FastAPI, Pydantic v2, httpx, Jinja2, PyYAML, pycryptodome (Keccak-256 for EIP-55),
Uvicorn, SQLite (WAL), pytest, and [Cytoscape.js](https://js.cytoscape.org) (dashboard graph, loaded from cdnjs).

## 6. Considered and deliberately not used

| Option | Why not |
|---|---|
| ML / GNN classifiers and their datasets: [Elliptic](https://arxiv.org/abs/1908.02591), [Elliptic2](https://github.com/MITIBMxGraph/Elliptic2), [BABD-13](https://github.com/Y-Xiang-hub/Bitcoin-Address-Behavior-Analysis), [Chartalist](https://github.com/cakcora/chartalist), [IBM AMLworld](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml) | Their outputs are probabilities that cannot be explained per case or defended as evidence (ADR-0002). They could later feed an *offline lead list* kept outside findings. |
| LLM agents: [RiskTagger](https://arxiv.org/abs/2510.17848), [LOCARD](https://arxiv.org/pdf/2604.04211) | Generated text and decisions cannot be reproduced bit-for-bit. Reports are template-only (ADR-0011) |
| Change-address heuristics | Error-prone (Kappos et al. 2022). We follow every output instead (ADR-0008) |
| Taint shares (haircut / FIFO / poison) | The result depends on an arbitrary convention. We report a bottleneck upper bound (ADR-0006) |
| Amount/time matching across chains | Produces candidates, not links (ADR-0014) |
| Scraped explorer labels without a source per record | No dereferenceable primary source. Imported only through datasets that cite one, and graded "weak" otherwise |
| Commercial intelligence (Chainalysis Reactor, TRM, Elliptic, Arkham) as a dependency | Closed and costly. They can plug in as providers through `intel.py` without changing grading |
