# ADR-0026: A busy account stops the trace on EVM chains (R-BUSY-ACCOUNT)

- Status: accepted
- Date: 2026-10-04
- Trigger: benchmark cases BM-04 and BM-07 ([benchmark.md](../benchmark.md))

## Context

ADR-0024 stopped Bitcoin traces from walking into an unlabelled exchange's pooled custody. It
assumed account-model chains were covered by R-SWEEP and the high-activity endpoint. On EVM
chains, an address is a high-activity endpoint only when its history exceeds the fetch cap.
The keyless JSON-RPC log scan (ADR-0021) reads a short window around the incident, so a busy
hot wallet can look quiet inside that window and get expanded.

The first live run of the Ethereum cases (D.D.C. OKX complaint) showed exactly this. The
filing says the subjects are OKX deposit addresses (BM-04) or received withdrawals from an
OKX account (BM-07). On-chain, both meet the same unlabelled address, `0x3d55ccb2…`:

* **In BM-07,** the withdrawals into the subject come from `0x3d55ccb2…`.
* **In BM-04,** the subject sends to `0x3d55ccb2…`.

That makes `0x3d55ccb2…` an OKX hot wallet in all but label. The tracer expanded it, and
others like it, and reported what other people's pooled money touched:

* **BM-07:** Kraken (grade C) at 3 hops;
* **BM-04:** Binance (grade C, confidence 43) at 2 hops.

Both were WRONG outcomes.

## Decision

On EVM chains, before an address past the subject is expanded, the tracer reads how many
transactions it has **sent**: its account nonce at the latest block (`eth_getTransactionCount`,
over JSON-RPC or Etherscan's proxy module). The response is recorded as evidence and replays
like any other.

* **Busy:** if the count is at least `high_activity_tx_count` (default 1,000, the same threshold
  as R-COSPEND-SERVICE), the address becomes a **HIGH_ACTIVITY** endpoint and is not expanded.
  Its note gives the count and the rule, plus "review manually".
* **Unreadable:** the address is expanded as before, and the failure is reported as a coverage
  gap.
* **Never checked:** the subject itself. Addresses attributed to a service, and contracts, end
  their paths before this point.

Like R-COSPEND-SERVICE, this rule can only **stop** a trace and remove claims. It never
attributes anything. Naming the service still needs a sourced label.

### Why the nonce

* **Lifetime figure:** it isn't limited to the scan window.
* **Cheap:** one call per address.
* **Verifiable:** anyone can re-query it.
* **Separates people from services:** an exchange hot wallet sends hundreds of thousands of
  transactions, while a person's wallet rarely reaches a thousand.

### Limits

* **Current, not historical:** the count is as of the query, not as of the incident. A wallet
  that became busy later is also stopped, which errs on the side of claiming less.
* **Receive-only services:** a collection wallet that mostly receives can keep a low nonce and
  is still expanded. The fetch-cap rule remains the backstop.
* **Tron:** not covered. TronGrid's account endpoint does not return a cheap lifetime count.

## Effect on the benchmark

Ethereum cases, first live run (before) and re-run (after this ADR), same labels and policy:

| Case | Before | After |
|---|---|---|
| BM-01 | HIT@1, OKX A/95 at 1 hop; also Binance, KuCoin (A), Bitfinex, Crypto.com, MXC (C) at 3 hops | HIT@1, OKX A/95 at 1 hop; no other VASP |
| BM-02, BM-03, BM-05, BM-06 | HIT@1, OKX A/95 at 1 hop | unchanged; no other VASP reached |
| BM-04 | **WRONG**: Binance C/43 at 2 hops, Crypto.com C/66 at 3 | **NOT_FOUND**: all 9 first-hop addresses stopped as high-activity |
| BM-07 | **WRONG**: Kraken (C) at 3 hops | **NOT_FOUND**: all 4 funding addresses stopped as high-activity |
| BM-08, BM-09 | not run before this ADR | HIT@1, OKX A/95 at 1 hop |

* **What stopped BM-04 and BM-07:** each first-hop address had sent between 52,633 and
  251,528 transactions.
* **Shared wallets:** seven of them also sit at hop 1 of BM-01, next to its labelled OKX
  wallets. That is consistent with OKX hot wallets, but the engine does not say so. Only a
  sourced label can name them.
* **Cost:** every Ethereum case now expands only its subject. Their findings cite 10–25
  evidence objects each; BM-01's and BM-04's cited 214 and 153 before.

## Consequences

- Fewer confident wrong answers on EVM chains where an exchange's hot wallets aren't
  labelled. Those paths stop at the hot wallet, reported as a likely unlabelled service.
- Traces through busy services are much cheaper: their history is never fetched.
- One extra request per expanded EVM address.
