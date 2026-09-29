# ADR-0009: Re-verify every transfer a reported path rests on

- Status: accepted
- Date: 2026-09-30

## Context

Address-history listings come from indexers (Etherscan's account API, TronGrid's event index,
Esplora address index). Indexers have bugs, re-orgs and inconsistencies. A report that relies
on a single listing inherits them.

## Decision

Every transfer on a path to a VASP, service, dormant, CoinJoin, contract or high-activity
endpoint is re-checked against a **transaction-level** endpoint, field by field:

| Chain | Listing | Verification |
|---|---|---|
| EVM | `txlist` / `tokentx` (indexer) | JSON-RPC proxy: `eth_getTransactionReceipt` (status, Transfer log with topics and amount, block), `eth_getTransactionByHash` (native), `eth_getBlockByNumber` (timestamp) |
| Tron | `/v1/accounts/…` (event index) | full-node API: `gettransactioninfobyid` (receipt, log, block time), `gettransactionbyid` (native transfer parameters) |
| Bitcoin | `/address/…/txs` | `/tx/{txid}` (block, time, input membership, output address and value) |
| Solana | signatures plus parsed transactions | re-fetch, re-parse, **and** check each token account's balance change equals the net of the parsed instruction amounts |

Statuses: `verified`, `mismatch` (the fact is not trusted), `unverifiable` (e.g. EVM internal
transfers, which receipts cannot show), `error`. A mismatch sets the confidence score to 0 and
blocks routing. Unverifiable transfers lower the score and require analyst review.

## Consequences

- More API calls per case (bounded by `verify_paths(cap=400)`).
- A different provider can be configured for verification in future, for full independence from the listing provider.
