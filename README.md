# Anveshak

**Evidence-first attribution of cryptocurrency wallets to the nearest exchange or other
virtual asset service provider (VASP).**

Given a suspect wallet, Anveshak follows the funds on-chain to the nearest VASP. It states how
well that attribution is supported, and drafts the disclosure or freeze request for the officer
to send through India's Sahyog portal. Every step rests on stored, independently re-verifiable
evidence. Nothing is guessed.

Built for Smart India Hackathon 2026, problem statement 26182 (*Automated Attribution of Unknown
Cryptocurrency Wallets to Nearest VASPs through Blockchain Intelligence APIs*). How each
requirement is met: [docs/requirements-traceability.md](docs/requirements-traceability.md).

## What it does

- **Follows the money** forwards to the nearest exchange or service the funds entered, or
  backwards to the service that funded the wallet. Chains: Bitcoin, Ethereum, BNB Chain,
  Polygon, Arbitrum, Base, OP Mainnet, Avalanche, Tron and Solana.
- **Crosses chains** through THORChain, Wormhole, LayerZero and Across, using each protocol's
  own records and confirming the payout on the destination chain.
- **Names the service and shows why:**
  * the entity, with a grade (A/B/C/X) and a 0–100 confidence score;
  * the deposit address the funds entered;
  * every transaction on the path, each re-verified against a second endpoint.
- **Flags risk:**
  * sanctions, ransomware, darknet and fraud links;
  * laundering patterns: peel chains, pass-through, fan-in/out, mixers, CoinJoin, chain-hopping;
  * hot wallets, deposit wallets and clusters.
- **Drafts the requests:** disclosure and freeze requests for the right intermediary, and
  issuer freezes for stablecoins still in place. Sahyog calls Anveshak's API, the officer
  approves inside Sahyog, and the VASP's reply flows back to confirm or deny the attribution.
- **Watches** addresses that still hold traced funds, and alerts when they move.
- **Produces a sealed report** (HTML, SHA-256), with a certificate template under Section 63
  of the Bharatiya Sakshya Adhiniyam.

## How it decides

No ML model, probability or generated text is used anywhere in the evidence path. Every
statement in a report is one of three kinds:

| Kind | Example | How you can check it |
|---|---|---|
| **Fact** | tx `3f…a1` moved 4,000 USDT from `TX…` to `TY…` in block 75,002,400 | re-verified against a transaction-level endpoint; the raw response is stored under its SHA-256 |
| **Claim** | "`TY…` belongs to Binance", per Binance's own proof-of-reserves page | the label names its source, and the source's authority is checked |
| **Inference** | grade **A** (rule G-A1), confidence **96/100**, deposit address by rule **R-SWEEP** | a fixed, published rule, itemised point by point |

When none of these apply, the answer is **unknown**, and every trace lists what it did not
examine.

**The confidence score** is a points rubric (attribution evidence, path verification, proximity,
corroboration). Its weights live in a versioned policy file. It ranks evidence strength and is
**not a probability** ([ADR-0002](docs/adr/0002-no-probabilistic-scores.md),
[ADR-0012](docs/adr/0012-scoring-policy.md)).

**Benchmark.** Anveshak is tested against 15 cases taken from public US court filings. It named
the correct exchange in 7 of them, each at grade A. It named a wrong one once, at grade C (which
never goes to approval). In the other 7 it said "not found" rather than guess.
See [docs/benchmark.md](docs/benchmark.md).

## Quick start

### 1. Install

You need Python 3.11 or newer and git.

**macOS / Linux**

```bash
git clone https://github.com/me-jain-anurag/Anveshak.git
cd Anveshak
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/me-jain-anurag/Anveshak.git
cd Anveshak
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

### 2. Try it without any setup

```bash
anveshak demo     # a synthetic case that exercises every rule (fictional data, marked "not evidence")
anveshak serve    # dashboard and API at http://127.0.0.1:8000
```

In the dashboard, click **Run synthetic demo**, then open the case to see:
- the fund-flow graph and the nearest VASPs with grades and confidence;
- the recommended requests;
- the full report.

### 3. Trace a real wallet

This address is named as an OKX deposit address in a US court filing. Ethereum can be traced
without any API key through a public JSON-RPC endpoint:

```bash
export ANVESHAK_RPC_ETHEREUM=https://rpc.mevblocker.io   # PowerShell: $env:ANVESHAK_RPC_ETHEREUM="https://rpc.mevblocker.io"
export ANVESHAK_LOGSCAN_HOURS=96                         # PowerShell: $env:ANVESHAK_LOGSCAN_HOURS="96"
anveshak trace --chain ethereum --address 0x7f2de657ab21f269ea8e37eea1a9245af6d2e669 \
  --direction out --since 2023-03-17T00:00:00Z --until 2023-03-21T00:00:00Z \
  --max-hops 3 --max-expansions 40 --case-ref "Example: D.D.C. USDT complaint, para 178"
```

It finishes in under a minute. The output includes:
- the endpoints of each path;
- `nearest VASP #1: okex at 1 hop(s), grade A, confidence 95`;
- the drafted requests;
- the findings hash and the path of the sealed HTML report.

`anveshak serve` shows the same case in the dashboard.

> **Windows:** Microsoft Defender's behaviour monitoring can stop live traces on EVM chains
> (a known false positive, [ADR-0021](docs/adr/0021-keyless-evm-rpc-log-scan.md#microsoft-defender-false-positive)).
> Run EVM traces with Docker or WSL. Everything else works natively.

### 4. Or run everything with Docker

```bash
cp .env.example .env          # optional: add API keys
docker compose up --build     # API + dashboard on http://localhost:8000, plus workers and the watchlist monitor
docker compose run --rm api anveshak demo
```

## Configuration

Settings come from environment variables or a `.env` file in the project root. Every option is
listed in [.env.example](.env.example) and explained in the [user guide](docs/usage.md#configuration).

| Chains | Data source | Key needed |
|---|---|---|
| Bitcoin | Esplora (blockstream.info) | none |
| Tron | TronGrid | optional `TRONGRID_API_KEY` (faster) |
| Ethereum | Etherscan API V2 with `ETHERSCAN_API_KEY` (free tier), **or** a JSON-RPC endpoint set in `ANVESHAK_RPC_ETHEREUM` | none with JSON-RPC |
| Polygon, Arbitrum | Etherscan API V2 if `ETHERSCAN_API_KEY` is set, otherwise built-in public JSON-RPC endpoints | none |
| BNB Chain, Base, OP Mainnet, Avalanche | built-in public JSON-RPC endpoints, or Etherscan with a paid plan (`ETHERSCAN_PAID=1`) | none |
| Solana | any JSON-RPC endpoint (`SOLANA_RPC_URL`) | none; a provider endpoint is faster |
| Cross-chain | THORChain Midgard, Wormholescan, LayerZero Scan, Across API | none |

EVM chains traced through JSON-RPC need the incident time (`--since`), because the scan covers a
window after it (default 72 hours, `ANVESHAK_LOGSCAN_HOURS`).

## Commands

| Command | What it does |
|---|---|
| `anveshak demo` | run the synthetic scenario |
| `anveshak serve [--host H] [--port P]` | API, dashboard, case workers and watchlist monitor |
| `anveshak trace --chain C --address A --case-ref R [...]` | trace one or more addresses with live data |
| `anveshak replay <case_id>` | re-run a case from its stored evidence; the findings hash must match |
| `anveshak trace ... --pack DIR` / `anveshak replay --pack DIR` | record a self-contained evidence pack / replay it offline on any machine |
| `anveshak pack load DIR` | show a recorded pack in the dashboard |
| `anveshak labels attest ...` | record a VASP's written confirmation or denial of an address |
| `anveshak labels import graphsense\|ofac\|thorchain` | refresh label datasets from their sources |
| `anveshak labels lookup --chain C --address A` / `labels stats` | inspect labels |
| `anveshak clients add --client-id ID --role ROLE` | create an API client and print its key once |
| `anveshak worker` / `anveshak monitor` | extra case worker / watchlist monitor processes |
| `anveshak export <case_id> --format neo4j\|graphml` | export the case graph |
| `anveshak benchmark [--replay]` | run the court-filing benchmark |

`anveshak <command> --help` lists every option. The [user guide](docs/usage.md) walks through
each workflow.

## Documentation

| Document | For |
|---|---|
| [User guide](docs/usage.md) | installing, configuring and using Anveshak: CLI, dashboard, API, deployment, troubleshooting |
| [Sahyog integration guide](docs/sahyog-integration.md) | the API contract for the Sahyog portal: reports, recommendations, outcomes, replies |
| [Architecture](docs/architecture.md) | pipeline, modules, security model, known limitations |
| [Requirements traceability](docs/requirements-traceability.md) | every PS 26182 requirement → implementation → test |
| [Benchmark](docs/benchmark.md) | cases from public court filings, results and limits |
| [Decision records](docs/adr/) | 26 architecture decision records |
| [References](docs/references.md) | every paper, dataset, API and standard used |

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

The test suite makes no network calls: adapters are tested against response shapes captured
from the live APIs. CI runs it on Python 3.12 and 3.13 for every push, along with a Docker build
and a health check.
