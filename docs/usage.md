# User guide

This guide covers installing, configuring and using Anveshak:

- [Install](#install)
- [Concepts](#concepts)
- [Tracing a wallet](#tracing-a-wallet)
- [The dashboard](#the-dashboard)
- [Reproducing a result](#reproducing-a-result)
- [Recording a VASP's reply](#recording-a-vasps-reply)
- [Watchlist and alerts](#watchlist-and-alerts)
- [Labels](#labels)
- [HTTP API](#http-api)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Benchmark](#benchmark)
- [Troubleshooting](#troubleshooting)

## Install

Requirements: Python 3.11 or newer, and git. Docker is optional.

```bash
git clone https://github.com/me-jain-anurag/Anveshak.git
cd Anveshak
python3 -m venv .venv            # Windows: py -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e .                 # add ".[dev]" for the test suite
anveshak --version
anveshak demo                    # checks the installation with a synthetic case
```

On Windows, if PowerShell refuses to run the activation script, call the tools directly
instead, e.g. `.venv\Scripts\anveshak demo`.

With Docker, `docker compose up --build` starts everything (see [Deployment](#deployment)).

Runtime data is written to `var/` in the project directory (or `ANVESHAK_VAR_DIR`):
- `evidence/`: every raw API response, content-addressed;
- `cases.sqlite3`: cases, queue, watchlist, alerts and audit log;
- `reports/`: findings JSON and sealed HTML reports.

## Concepts

| Term | Meaning |
|---|---|
| **Case** | one investigation request: a case reference, one or more subject addresses, and trace settings |
| **Direction** | `out` follows value leaving the subject (where did it go?); `in` follows value arriving (who funded it?) |
| **Hop** | one transfer along a path. The *nearest* VASP is the one reached in the fewest hops |
| **Endpoint** | where a path stops; its kind says why (below) |
| **Grade** | how well an attribution is supported: **A** entity- or authority-attested, **B** independent curated sources agree, **C** a single or weak source, **X** sources conflict |
| **Confidence** | 0–100 points from a published rubric (attribution, verification, proximity, corroboration). Not a probability |
| **Recommendation** | a drafted disclosure, freeze or issuer-freeze request, with readiness: `ready_for_approval`, `analyst_review` or `blocked` |
| **Findings hash** | SHA-256 of everything the conclusions depend on. A replay from stored evidence must reproduce it |

Endpoint kinds:

| Kind | Meaning |
|---|---|
| `vasp` | the funds entered an attributed VASP (exchange, custodial wallet, payment processor) |
| `service` | a non-VASP service: mixer, bridge, DeFi, gambling |
| `high_activity` | an unlabelled address too busy to follow; possibly an unlabelled service. Review manually |
| `unlabeled_contract` | a smart contract without a label; funds may be pooled |
| `coinjoin_like` | a Bitcoin CoinJoin; deterministic linking stops here |
| `dormant` / `origin` | value stopped moving here (forward) / no earlier funding found (backward) |
| `hop_limit` / `not_expanded` | the hop limit or the search budget was reached |
| `source_error` | a data source failed; a coverage gap, reported as such |

## Tracing a wallet

```bash
anveshak trace --chain tron --address TXYZ... --case-ref "FIR 123/2026, Cyber PS" --direction both
```

| Option | Default | Use |
|---|---|---|
| `--chain` | required | `bitcoin`, `ethereum`, `bsc`, `polygon`, `arbitrum`, `base`, `optimism`, `avalanche`, `tron`, `solana` |
| `--address` | required | repeat for several subjects on the same chain |
| `--case-ref` | required | your reference (FIR number, Sahyog reference) |
| `--direction` | `both` | `out`, `in` or `both` |
| `--since` / `--until` | none | ISO-8601 with timezone, e.g. `2026-09-01T00:00:00+05:30`. Required for EVM chains traced through JSON-RPC |
| `--max-hops` | 5 | how far to follow (1–10) |
| `--max-expansions` | 150 | search budget: the number of addresses whose history is fetched |
| `--max-branch` | 20 | the most transfers followed out of one address |
| `--follow-all-assets` | off | follow every verified asset, not only the one that arrived |
| `--pack DIR` | none | also write a self-contained evidence pack ([Reproducing a result](#reproducing-a-result)) |

The command prints, for each subject and direction:
- the endpoints of each path, with their attribution;
- the nearest VASPs with grade and confidence;
- the typologies detected;
- the coverage: what was expanded or excluded, and why.

It then prints the drafted requests, the **findings hash**, and the location and SHA-256 of the
HTML report.

**The report** is meant for the case file. It contains:
- a summary;
- each path with per-transaction verification;
- the attribution evidence with its sources;
- the confidence breakdown, risk and typologies;
- "what we did not examine";
- the data sources used;
- an evidence manifest;
- a certificate template under Section 63 of the Bharatiya Sakshya Adhiniyam.

### Chain notes

- **Bitcoin** uses Esplora; no key is needed. Co-spent inputs link addresses (except in
  CoinJoins), and addresses co-spent with very busy wallets are not followed.
- **Tron** uses TronGrid. It works without a key but is slow; set `TRONGRID_API_KEY` for real
  casework.
- **EVM chains** use Etherscan API V2 when your key's plan covers the chain. Otherwise they use
  a JSON-RPC scan of verified-token transfers in a window after `--since`:
  * the default window is 72 hours (`ANVESHAK_LOGSCAN_HOURS`);
  * native-coin transfers are not visible in a log scan;
  * for old incidents, use an endpoint that serves historical logs, set with
    `ANVESHAK_RPC_<CHAIN>`.
- **Solana** works with the public endpoint, but slowly. Set `SOLANA_RPC_URL` to a provider
  endpoint.
- **Cross-chain** swaps through THORChain, Wormhole, LayerZero and Across are followed onto the
  destination chain automatically, using each protocol's own record.

## The dashboard

Run `anveshak serve` and open http://127.0.0.1:8000.

| Tab | What it is for |
|---|---|
| **Cases** | start a case, or **Run synthetic demo**. Opening a case shows:<br>• the fund-flow graph;<br>• the nearest VASPs;<br>• the recommendations and their outcome history;<br>• the endpoints;<br>• the full report, and a re-run |
| **Sahyog intake** | paste wallets reported on Sahyog (any chain; the chain is detected from the address format) |
| **Alerts** | sanctions/risk hits and watchlist movements |
| **Watchlist** | addresses being monitored for fund movement |
| **Analytics** | outcomes per intermediary, VASPs reached, typologies, risk levels |
| **Lookup** | one address's labels, risk and earlier cases; screen up to 100 addresses at once |

If API keys are configured, click **API key** and paste a key with at least the `investigator`
role. The key is kept for that browser tab only.

## Reproducing a result

Every API response a case relies on is stored under its SHA-256. To re-run a case without any
network access:

```bash
anveshak replay <case_id>     # prints RESULT: MATCH when the findings hash is reproduced
```

An **evidence pack** bundles a case with all of its evidence, so it can be checked on any
machine, for example by a court-appointed expert:

```bash
anveshak trace --chain ethereum --address 0x... --since ... --case-ref "..." --pack packs/case-42
anveshak replay --pack packs/case-42      # offline, on any machine, with no configuration
anveshak pack load packs/case-42          # show it in the dashboard
```

The findings record the data sources used, so a pack replays identically wherever it is opened.
Endpoint keys are never recorded.

## Recording a VASP's reply

When a VASP replies to a request, record its answer. Future cases use it as grade-A evidence,
or, for a denial, stop attributing the address to that VASP:

```bash
anveshak labels attest --chain tron --address TXYZ... --entity-id binance --entity-name Binance \
    --document-ref "Sahyog reply REF-123 dated 2026-10-02" --as-of 2026-10-02 [--denies] [--reply-id REF-123]
```

Re-run an existing case (dashboard **Re-run**, or `POST /v1/cases/{id}/rerun`) to apply the
reply. Only the document reference is stored, never personal data from the reply.

## Watchlist and alerts

- **Adding addresses:** addresses that still hold traced funds are added to the watchlist
  automatically. Add others in the dashboard or with `POST /v1/watchlist`.
- **Monitoring:** `anveshak serve` checks the watchlist every 10 minutes
  (`ANVESHAK_MONITOR_INTERVAL`). Run `anveshak monitor --once` to check now.
- **When funds move:** an alert is raised and, by default, a follow-up trace is queued
  (`ANVESHAK_AUTO_FOLLOW_UP`).

## Labels

Attribution comes from labels, each with its source and source class.

- **Shipped:**
  * imported GraphSense tag packs;
  * OFAC sanctions;
  * THORChain vault addresses;
  * a curated VASP directory (`data/vasp_directory.yaml`);
  * the asset registry (`data/assets.yaml`).
- **Commands:**
  * `anveshak labels stats`: what is loaded;
  * `anveshak labels lookup --chain C --address A`: every label on an address, and the
    resulting grade;
  * `anveshak labels import graphsense|ofac|thorchain`: refresh a dataset from its source;
    the files and a manifest of hashes are written under `data/labels/imported/`.
- **Recorded replies** are written to `var/labels/attestations.jsonl`.

## HTTP API

The API is served by `anveshak serve`. Interactive OpenAPI documentation is at `/docs`. The
Sahyog portal's full contract is in the [Sahyog integration guide](sahyog-integration.md).

**Authentication.** Create one client per caller. The key is printed once, and only its SHA-256
is stored:

```bash
anveshak clients add --client-id cyber-ps-investigators --role investigator --agency DL-IFSO
```

Send the key in the `X-API-Key` header. Roles:

| Role | Can |
|---|---|
| `sahyog` | report wallets, read every case's recommendations, report outcomes, record replies, screen addresses |
| `investigator` | create and read its own agency's cases, screen addresses, manage the watchlist |
| `supervisor` | as investigator, plus record replies and outcomes |
| `auditor` | read every case and the audit log |
| `admin` | everything |

Without any client configured, the API is open. That is for local development only, and
`/v1/meta` says so.

**Main endpoints:**

| Method and path | Purpose |
|---|---|
| `POST /v1/cases` | start a case: `{"case_reference": "...", "subjects": [{"chain": "tron", "address": "T..."}], "directions": ["out"], "since": "..."}` |
| `GET /v1/cases/{id}` | status and findings |
| `GET /v1/cases/{id}/report` | the sealed HTML report |
| `GET /v1/cases/{id}/recommendations` | drafted requests (schema `anveshak.recommendation/v1`) |
| `POST /v1/cases/{id}/rerun` | run the same request again |
| `GET /v1/addresses/{chain}/{address}` | attribution, risk and earlier cases for one address (no chain calls) |
| `POST /v1/screen` | the same for up to 100 addresses: `{"addresses": ["T...", "0x..."]}` |
| `POST /v1/attestations` | record a VASP's reply (confirms or denies) |
| `GET/POST /v1/watchlist`, `GET /v1/alerts` | monitoring |
| `GET /v1/audit/verify` | verify the hash-chained audit log |

Example:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/cases -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"case_reference": "FIR 123/2026", "subjects": [{"chain": "bitcoin", "address": "bc1q..."}], "directions": ["out"]}'
curl -s http://127.0.0.1:8000/v1/cases/<case_id> -H "X-API-Key: $KEY"
```

## Deployment

`docker compose up --build -d` starts three services sharing one volume for evidence, the
database and reports:

- `api`: API and dashboard on port 8000, with one embedded worker;
- `worker`: two case workers. Scale with `docker compose up --scale worker=4`;
- `monitor`: the watchlist monitor.

Put configuration in `.env`; Compose passes it to every service. For a production deployment:

- **API access:** create a client for each caller (`anveshak clients add`), with `--ip` allowlists
  for fixed callers such as the Sahyog portal.
- **Transport:** serve the API behind a TLS-terminating reverse proxy.
- **Callbacks to Sahyog:** set `ANVESHAK_CALLBACK_ALLOWLIST` and `ANVESHAK_CALLBACK_SECRET`.
  Callbacks then go only to those hosts, over HTTPS, signed with HMAC-SHA256.
- **Backups:** back up the `var` volume. Evidence objects are immutable and can be archived per
  case.

The design for scaling beyond one host is in [ADR-0015](adr/0015-scaling.md).

## Configuration

Set these as environment variables or in a `.env` file in the project root. Every value is
optional.

| Variable | Default | Purpose |
|---|---|---|
| `ETHERSCAN_API_KEY` | none | Etherscan API V2 key (free tier covers Ethereum, Polygon, Arbitrum) |
| `ETHERSCAN_PAID` | off | `1` if the key's plan also covers BNB Chain, Base, OP Mainnet, Avalanche |
| `ANVESHAK_RPC_<CHAIN>` | built-in public endpoints for bsc, polygon, arbitrum, base, optimism, avalanche | JSON-RPC endpoint per EVM chain, e.g. `ANVESHAK_RPC_ETHEREUM`. Keys inside the URL are never recorded |
| `ANVESHAK_LOGSCAN_HOURS` | 72 | window after the incident time for JSON-RPC log scans |
| `TRONGRID_API_KEY` | none | TronGrid key (higher rate limits) |
| `SOLANA_RPC_URL` | public mainnet endpoint | Solana JSON-RPC endpoint |
| `SOLANA_MAX_SIGNATURES` | 300 | signatures fetched per Solana address |
| `ESPLORA_BASE_URL`, `TRONGRID_BASE_URL`, `ETHERSCAN_BASE_URL` | public services | alternative or self-hosted endpoints |
| `ANVESHAK_RESOLVERS` | `thorchain,wormhole,layerzero,across` | cross-chain resolvers to use |
| `ANVESHAK_EVM_CHAINS` | `ethereum,polygon,arbitrum,bsc` | EVM chains probed when a reported `0x` address arrives without a chain |
| `CHAINALYSIS_API_KEY` | none | Chainalysis sanctions screening, stored and graded like any label |
| `ETHERSCAN_NAMETAGS` | off | `1` to use Etherscan name tags (needs a plan that includes them) |
| `ANVESHAK_VAR_DIR` | `var` | evidence, database and reports |
| `ANVESHAK_DATA_DIR` | `data` | reference data: labels, registry, directory, policies |
| `ANVESHAK_API_CLIENTS_FILE` | `<var>/api_clients.yaml` | API clients file |
| `ANVESHAK_API_TOKEN` | none | legacy single admin key, used when no clients file exists |
| `ANVESHAK_MAX_BODY_BYTES` | 1048576 | larger request bodies are rejected |
| `ANVESHAK_CALLBACK_ALLOWLIST` | none | hosts that may receive case callbacks |
| `ANVESHAK_CALLBACK_SECRET` | none | HMAC key for callback signatures |
| `ANVESHAK_STANDALONE_APPROVALS` | off | `1` enables Anveshak's own approve endpoint (pilots without Sahyog) |
| `ANVESHAK_EMBEDDED_WORKERS` | 2 | case workers inside `anveshak serve` |
| `ANVESHAK_MONITOR_INTERVAL` | 600 | watchlist check interval in seconds (`0` disables it in this process) |
| `ANVESHAK_AUTO_FOLLOW_UP` | on | queue a follow-up trace when watched funds move |

## Benchmark

`anveshak benchmark` traces 15 cases taken from public US court filings and scores whether the
nearest VASP matches the one the filing names. It updates [benchmark.md](benchmark.md).
`--replay` re-scores from recorded evidence, offline. The Ethereum cases need
`ANVESHAK_RPC_ETHEREUM` or `ETHERSCAN_API_KEY`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `... is traced through a public RPC log scan ...: the incident time since is required` | EVM chains without an Etherscan plan are scanned in a window after the incident. Pass `--since` (or the dashboard's *Since* field) |
| `pruned history unavailable` in the coverage section | the public endpoint does not keep old logs. Set `ANVESHAK_RPC_<CHAIN>` to an archive endpoint, or use an Etherscan key |
| Tron traces are slow or show `HTTP 429` | TronGrid's anonymous rate limit. Set `TRONGRID_API_KEY` |
| `budget exhausted` in the coverage section | raise `--max-expansions`. The coverage section lists what was not expanded |
| `RESULT: DIFFERENT` on replay | the label set or VASP directory changed since the case ran. The replay prints which |
| Port 8000 already in use | `anveshak serve --port 8080` |
| On Windows, the process stops without an error during an EVM trace | a known Microsoft Defender false positive ([ADR-0021](adr/0021-keyless-evm-rpc-log-scan.md#microsoft-defender-false-positive)). Run EVM traces in Docker or WSL |
