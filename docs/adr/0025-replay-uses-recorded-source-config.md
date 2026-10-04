# ADR-0025: Replay uses the recorded data-source configuration; endpoint credentials are redacted

- Status: accepted
- Date: 2026-10-04
- Trigger: replaying benchmark case BM-04 ([benchmark.md](../benchmark.md))

## Context

ADR-0004 promises that a live case can be replayed from its evidence, offline, to the same
findings hash. The evidence index is keyed by the exact request: method, URL, redacted
parameters and body. Which requests a case makes depends on the data-source settings, and
replay took those settings from the replaying machine, not from the case:

* **EVM backend choice** (ADR-0021): Etherscan if a key whose plan covers the chain is set,
  otherwise the JSON-RPC log scan if an endpoint is configured.
* **Endpoints:** RPC, Esplora, TronGrid, Solana.
* **Log-scan settings:** window length and block-range cap.
* **Cross-chain resolvers** (ADR-0022).

So an Ethereum case recorded through `ANVESHAK_RPC_ETHEREUM` replayed on a machine without that
variable looked for Etherscan requests, found none, and failed. This happened when BM-04 was
replayed for diagnosis. The same gap broke the benchmark workflow's replay step, which ran
without the live step's environment. An evidence pack recorded on one machine was only portable to
another configured identically.

A second gap was found while fixing this. Hosted node providers put the API key in the endpoint
URL (Infura `/v3/<key>`, Alchemy `/v2/<key>`, QuickNode `/<token>/`). Request URLs were recorded
as given, so such a key would have reached the evidence index, the findings and every pack,
contrary to ADR-0004's "API keys never reach disk".

## Decision

1. **The findings record the source configuration** (`CaseFindings.source_config`):
   * per chain used: the backend (`etherscan`, `rpc`, `trongrid`, `solana-rpc`, `esplora`), its
     endpoint, and the log-scan window and block-range cap where relevant;
   * the names of the cross-chain resolvers.

   It is part of the findings hash, because the window and backend bound what the trace could see.
2. **Replay uses the recorded configuration,** not the local settings (`anveshak replay`,
   `anveshak replay --pack`). A pack recorded anywhere replays anywhere.
3. **Endpoint credentials are redacted** from POST (JSON-RPC) endpoint URLs before they are
   recorded, used as an index key, shown in `/v1/meta`, or written to the findings. This covers:
   * userinfo;
   * secret query parameters;
   * path segments that look like tokens: at least 20 characters of `[A-Za-z0-9_-]`, with a
     digit or mixed case.

   Method names such as TronGrid's `gettransactioninfobyid` are not tokens. GET URLs are left
   alone, because their paths carry addresses and transaction ids that must stay distinct.
   Keys for GET APIs go in the dedicated settings (`ETHERSCAN_API_KEY`, `TRONGRID_API_KEY`),
   which are redacted as parameters and headers.
4. **The report shows the data sources** in its header, with the window where one applies.

## Compatibility

* **Findings recorded before this ADR** have no `source_config`. They hash exactly as before
  (an absent value is left out of the hash) and replay with the current settings, as they
  always did.
* **Shipped endpoint URLs contain no credentials,** so redaction leaves them unchanged and
  existing evidence keeps its request keys.

## Consequences

- Reports and packs name the endpoints used, without credentials. An examiner can see where
  the data came from.
- `anveshak benchmark --replay` builds each case afresh from the case file, so it still uses
  the local source settings. Run it with the same settings as the live run. The benchmark
  workflow now sets them for both steps.
- A provider whose key looks like a plain lowercase word in a GET path would not be detected.
  Such keys should go in a dedicated setting. None of the supported sources need one in a path.
