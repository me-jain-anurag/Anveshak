# ADR-0021: EVM chains without a paid indexer — window-limited JSON-RPC log scan

- Status: accepted
- Date: 2026-10-04

## Context

The PS names BNB Chain. Etherscan API V2's free tier covers Ethereum, Polygon and Arbitrum
only (checked 2026-09-30). BNB Chain, Base, OP Mainnet and Avalanche need a paid plan. Plain
JSON-RPC has no "list this address's history" call, but `eth_getLogs` can filter ERC-20
`Transfer` events by token contract and by sender or receiver topic.

## Decision

- **`RpcLogSource`** implements `ChainSource` over any standard JSON-RPC endpoint:
  * `history(address)` runs two `eth_getLogs` queries per range: the address as sender, then
    as receiver. Both are filtered to the **verified registry token contracts**.
  * The scan covers a **window** `[since, since + ANVESHAK_LOGSCAN_HOURS]` (default 72 h,
    capped at the chain's latest block). The window is read from the chain itself, never the
    wall clock, so replays are exact.
  * Block bounds come from an estimate based on recent block times, an outward gallop, then a
    binary search over block timestamps. This works on endpoints that prune old blocks.
    Verified live on 2026-10-03: Base 23 calls, BSC 28, Avalanche 25, all with exact boundaries.
  * Range chunks default to 5,000 blocks (1,000 on the Polygon public endpoint) and are halved
    automatically when an endpoint rejects a range.
  * Each history declares itself **window-limited**. Its note states the window, "verified
    tokens only" and "native-coin transfers not visible in logs". The report shows a
    window-limited row in coverage.
  * There is a request budget per history. Running out of it is declared in the note too.
  * Verification uses `eth_getTransactionReceipt` + `eth_getBlockByNumber`. It checks the
    status, the block, the timestamp and the exact Transfer event by occurrence.
- **Backend choice** (`evm_backend`):
  * `etherscan` when a key exists and its plan covers the chain (free tier, or `ETHERSCAN_PAID=1`);
  * otherwise `rpc` when an RPC URL is known. Built-in public defaults exist for BSC, Polygon,
    Arbitrum, Base, OP and Avalanche, and `ANVESHAK_RPC_<CHAIN>` overrides or adds one, e.g.
    `ANVESHAK_RPC_ETHEREUM`;
  * cases on an `rpc` chain **must** give the incident time (`since`). Otherwise the API
    returns 422 and the CLI refuses.
- **Pruned endpoints:**
  * Errors such as "pruned history unavailable" or "missing trie node" raise a clear
    SourceError that names the setting to use for an archive endpoint.
  * Public endpoints differ. On 2026-10-04 `ethereum-rpc.publicnode.com` refused archive log
    queries without a token, while `rpc.mevblocker.io` served 2023 Ethereum logs.
- Tested with a fake node that prunes early blocks and limits ranges. The tests cover: exact
  window search, capping at the latest block, the pruned-horizon error, registry-only and
  window-only history, budget declaration, verification and tamper detection, extras
  (`is_contract`, `has_activity`, `tx_transfers`), backend choice, and engine replay to an
  identical findings hash.

## Microsoft Defender false positive

On the Windows development machine, Microsoft Defender's behaviour detection
`Behavior:Win32/SuspEtherRpcConn.B` killed the Python process with no traceback. This happened
when the GraphSense ransomware label packs (`ransomware.jsonl`, `ransomwhere.jsonl`) were
loaded **and** the process made EVM JSON-RPC calls (observed with bsc-rpc.publicnode.com).

* **Narrowing it down:** the same calls in a process without those labels ran normally.
  Defender's operational log named the detection.
* **Classification:** a heuristic false positive on legitimate investigation software, not a
  bug in the code.
* **What we did not do:** change security settings or add exclusions. That decision belongs to
  the operator.
* **Guidance:**
  * Run Anveshak on Linux or in Docker (the supported deployment, ADR-0015), where this
    heuristic does not apply.
  * On Windows, ask the IT administrator to review the detection.
  * The live BSC smoke test was run in a process without the ransomware labels. The full engine
    run uses replay, which makes no network calls.

## Consequences

- BNB Chain, Base, OP and Avalanche are traceable without a paid key, within a window and for
  verified tokens. Native-coin flows on those chains need an indexer (Etherscan paid,
  or a self-hosted node + indexer, ADR-0015).
- The window can miss activity outside it. That limit is always stated, never hidden.
