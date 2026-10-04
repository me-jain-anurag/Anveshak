# Anveshak — evidence-first wallet → VASP attribution

Smart India Hackathon 2026 · Problem Statement **26182**: *Automated Attribution of Unknown
Cryptocurrency Wallets to Nearest Virtual Asset Service Providers (VASPs) through Blockchain
Intelligence APIs.*

Given a suspect wallet (reported on the Sahyog portal, or entered by an officer), Anveshak does the following:

- **Follows the money** on-chain to the **nearest exchange or other service** it entered, across
  **Bitcoin, Ethereum, BNB Chain, Polygon, Arbitrum, Base, OP Mainnet, Avalanche, Tron and Solana**.
  Backwards, it finds the service that funded the wallet.
- **Crosses chains** through **THORChain, Wormhole, LayerZero and Across**, using each protocol's
  own records and confirming the payout on the destination chain.
- **Names the service and backs it up:**
  * who the service is, and how well that is supported (grade A/B/C/X and a 0–100 confidence score);
  * which deposit address the funds entered;
  * exactly which transactions prove the link, each one independently re-verified.
- **Classifies risk and patterns:**
  * wallet and flow risk;
  * laundering typologies: peel chains, pass-through, fan-in/out, mixers, CoinJoin, chain-hopping;
  * hot wallets, deposit wallets and clusters;
  * alerts on sanctions, ransomware, darknet, terrorism and fraud links.
- **Returns routing recommendations to Sahyog.** It drafts disclosure and freeze requests for the
  right intermediary (with Sahyog's own intermediary id where known), and issuer freezes when
  stablecoins are still in place.
  * The officer approves and sends **inside Sahyog**.
  * Sahyog reports the outcome back and records the VASP's reply, which confirms or **denies**
    the attribution for future cases.
- **Watches** addresses still holding traced funds and alerts the moment they move.

## Design principle: no guessing

No ML model, no probability and no AI-generated text is used anywhere in the evidence path.
Every statement is one of the following:

| kind | example | how you can check it |
|---|---|---|
| **Fact** | tx `3f…a1` moved 4,000 USDT from `TX…` to `TY…` in block 75,002,400 | re-verified against a second, transaction-level endpoint; the raw response is stored under its SHA-256 |
| **Claim** | "`TY…` belongs to Binance", per Binance's own proof-of-reserves page | the label names its source. "Entity-attested" counts only on the entity's official channel, and "authority" only on a government domain |
| **Inference** | grade **A** (rule G-A1), confidence **96/100**, deposit address by rule **R-SWEEP** | a fixed, published rule, itemised point by point |

If none of these apply, the answer is **unknown**, and each trace reports what it did not examine.

The **confidence score** the PS asks for is a points rubric (attribution evidence + path
verification + proximity + corroboration), with weights in a versioned policy file. It ranks
evidence strength. **It is not a probability.** See [ADR-0002](docs/adr/0002-no-probabilistic-scores.md)
and [ADR-0012](docs/adr/0012-scoring-policy.md).

How well it finds the right VASP is measured against public court filings, and the misses are
reported: see [docs/benchmark.md](docs/benchmark.md).

## Quick start

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/Scripts/anveshak demo            # synthetic scenario covering every rule (watermarked, not evidence)
.venv/Scripts/anveshak serve           # dashboard + API + workers at http://127.0.0.1:8000
```

Live tracing uses public APIs. Copy `.env.example` to `.env`:

| chains | source | key |
|---|---|---|
| Tron | TronGrid | optional (`TRONGRID_API_KEY`), rate-limited without one |
| Bitcoin | Esplora (blockstream.info) | none |
| Solana | any JSON-RPC (`SOLANA_RPC_URL`) | public endpoint works, but slowly |
| Ethereum, Polygon, Arbitrum | Etherscan API V2 | `ETHERSCAN_API_KEY` (free tier) |
| BNB Chain, Base, OP Mainnet, Avalanche | Etherscan API V2 with `ETHERSCAN_PAID=1`, **or** a keyless JSON-RPC log scan (window-limited, verified tokens; needs the incident time) | none for the log scan; `ANVESHAK_RPC_<CHAIN>` to use your own node |
| cross-chain | THORChain Midgard, Wormholescan, LayerZero Scan, Across API | none |
| intelligence (optional) | Chainalysis sanctions API, Etherscan name tags | `CHAINALYSIS_API_KEY`, `ETHERSCAN_NAMETAGS=1` |

```bash
anveshak trace --chain tron --address T... --case-ref "FIR 123/2026" --direction both
anveshak trace --chain bsc --address 0x... --case-ref "FIR 124/2026" --since 2026-09-01T00:00:00+05:30
anveshak replay <case_id>              # re-run from stored evidence; the findings hash must match
anveshak trace ... --pack packs/case1 # record a self-contained evidence pack; `anveshak replay --pack packs/case1` offline, on any machine
anveshak worker                        # extra worker process (shares the case queue)
anveshak monitor --once                # check watched addresses now
anveshak export <case_id> --format neo4j
anveshak labels import graphsense | ofac | thorchain
anveshak labels attest --chain tron --address T... --entity-id binance --entity-name Binance \
    --document-ref "Sahyog reply REF-123 dated 2026-10-02" --as-of 2026-10-02 [--denies] [--reply-id REF-123]
anveshak clients add --client-id sahyog-portal --role sahyog      # prints the API key once
anveshak benchmark                     # ground-truth benchmark → docs/benchmark.md
python tools/mock_sahyog/mock_sahyog.py --key <key> --wallet T...  # play the Sahyog side end to end
docker compose up --scale worker=4
```

## Documentation

- [Requirements traceability](docs/requirements-traceability.md): every PS clause → code → test → status
- [Architecture](docs/architecture.md): pipeline, modules, security, known limitations
- [Sahyog integration guide](docs/sahyog-integration.md): Sahyog as the client; recommendations,
  outcomes, replies, screening, keys and scope
- [Benchmark](docs/benchmark.md): cases from public court filings, results and limits
- [Decision records](docs/adr/): 26 ADRs
- [References](docs/references.md): every paper, dataset, API, document and standard used, and where

## Tests

```bash
.venv/Scripts/python -m pytest
```

The suite makes no network calls; CI runs it on every push, along with a Docker build and a
health check. It covers:

* checksum test vectors (BIP-173/350, EIP-55, real Tron pairs, live Solana token accounts);
* every grading and scoring rule, including formal denials;
* the adapters and bridge resolvers, against response shapes captured from the live APIs;
* the keyless EVM log scan, against a fake node that prunes and limits ranges;
* verification mismatch handling, and evidence replay reproducing the findings hash;
* the queue, watchlist, ingestion and API;
* per-client access control, agency scoping and the audit log;
* a full Sahyog round trip driven by the mock Sahyog client.
