# Anveshak — evidence-first wallet → VASP attribution

Smart India Hackathon 2026 · Problem Statement **26182**: *Automated Attribution of Unknown
Cryptocurrency Wallets to Nearest Virtual Asset Service Providers (VASPs) through Blockchain
Intelligence APIs.*

Given a suspect wallet, Anveshak follows the money on-chain to the **nearest exchange or other
service** it entered (and, backwards, the service that funded it). It says **who that service is, how
well that is supported, and exactly which transactions prove the link**. It then drafts the
disclosure or freeze request for an officer to approve on the Sahyog portal.

## Design principle: no guessing

Anveshak produces **no ML scores, no probabilities, and no AI-generated text**. Every statement in
its output is one of the following:

| kind | example | how you can check it |
|---|---|---|
| **Fact** | tx `3f…a1` moved 4,000 USDT from `TX…` to `TY…` in block 75,002,400 | re-verified against a second, transaction-level endpoint; the raw response is stored under its SHA-256 |
| **Claim** | "`TY…` belongs to Binance", per Binance's own proof-of-reserves list | the label names its source, the class of that source, and the dataset record it came from |
| **Inference** | grade **A** by rule **G-A1**; deposit-address pattern by rule **R-SWEEP** | a fixed, published rule applied to the facts and claims above |

If none of these apply, the answer is **unknown**, and the report says what was not examined.
Attribution **grades** (A attested · B corroborated · C single-source · X conflicted) come from
*who* makes a claim and *whether independent sources agree*. They are not probabilities.
See [ADR-0002](docs/adr/0002-no-probabilistic-scores.md).

## Quick start

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/Scripts/anveshak demo            # synthetic scenario (fictional data, watermarked)
.venv/Scripts/anveshak serve           # dashboard + API at http://127.0.0.1:8000
```

Live tracing uses public APIs. Copy `.env.example` to `.env`:

| chain | source | key |
|---|---|---|
| Tron | TronGrid | optional (`TRONGRID_API_KEY`), rate-limited without one |
| Bitcoin | Esplora (blockstream.info) | none |
| Ethereum, Polygon | Etherscan API V2 | `ETHERSCAN_API_KEY` (free tier) |
| BNB Smart Chain | Etherscan API V2 | paid Etherscan plan, or any Etherscan-compatible `ETHERSCAN_BASE_URL` |

```bash
anveshak trace --chain tron --address T... --case-ref "FIR 123/2026" --direction both
anveshak replay <case_id>              # re-run from stored evidence; the findings hash must match
anveshak labels import graphsense      # refresh public label datasets
anveshak labels import ofac
anveshak labels attest --chain tron --address T... --entity-id binance --entity-name Binance \
    --document-ref "Sahyog reply REF-123 dated 2026-10-02" --as-of 2026-10-02
```

`attest` is the feedback loop. When a VASP confirms an address in its reply, that address becomes
a grade-A label for every future case.

## What it does

- **Chains:** Bitcoin, Ethereum, BNB Smart Chain, Polygon, Tron (native coins, plus USDT/USDC by
  verified contract address).
- **Tracing:** forward ("where did the money go") and backward ("who funded this wallet"). Paths
  follow time order, stop after a fixed number of hops, and stop at services. Search limits are
  reported, not hidden.
- **Recognising services:** labels (GraphSense TagPacks, OFAC SDN, investigator attestations) are
  graded by fixed rules. It also detects Bitcoin co-spend with an exchange wallet, EVM same-key
  addresses, the account-chain deposit-sweep pattern, and CoinJoin structure.
- **Stopping points:** VASPs, mixers, bridges and DeFi contracts, unlabelled contracts,
  high-activity addresses, and addresses where funds are still sitting (with the current balance).
- **Verification:** every transfer on a reported path is re-checked against a transaction-level
  endpoint (EVM JSON-RPC receipts, Tron full-node API, Esplora tx).
- **Routing:** request drafts grouped by VASP. A draft is `ready_for_approval` only if it has a
  fully verified path, a grade A/B attribution, and a verified contact channel. Freeze-review
  suggestions go to stablecoin issuers when USDT/USDC is still in place. Nothing is sent without an
  officer's approval.
- **Report:** a print-ready HTML report with the evidence manifest and a BSA 2023 s.63 certificate
  template. Its SHA-256 is written alongside it.

## Documentation

- [Architecture](docs/architecture.md): modules, data flow, extension points
- [Decision records (ADRs)](docs/adr/): why it is built this way
- [References](docs/references.md): every paper, dataset, API and source used, and where
- [Limitations](docs/architecture.md#known-limitations)

## Tests

```bash
.venv/Scripts/python -m pytest
```
