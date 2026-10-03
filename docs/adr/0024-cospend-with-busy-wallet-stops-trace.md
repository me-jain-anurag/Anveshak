# ADR-0024: Co-spend with a high-activity address stops the trace (R-COSPEND-SERVICE)

- Status: accepted
- Date: 2026-10-04
- Trigger: benchmark case BM-10 ([benchmark.md](../benchmark.md))

## Context

BM-10 comes from a D.D.C. complaint. A victim withdrew BTC from **Kraken** to a scam address.
Traced backwards ("funding in"), the first live run reported **Binance, grade A, confidence 93,
at 2 hops** as the nearest VASP. That answer was wrong, and highly confident.

The replayed evidence shows why:

* Kraken's withdrawal transaction spent several inputs together. Two of them are busy hot
  wallets (22,486 and 19,830 transactions), and the tracer flagged both as high-activity. A
  third input was a quiet address, most likely a Kraken deposit address funded earlier from
  Binance. The tracer expanded it, walked inside Kraken's custody, and found Binance.
* Following an exchange's pooled funds attributes other people's money. For labelled services
  the tracer already stops (ADR-0006), and for Bitcoin the common-input rule D-COSPEND
  (ADR-0008) already ties co-spent inputs to one controller when one of them is *labelled*.
* Neither rule applied here, because none of Kraken's addresses are in the shipped labels.

## Decision

For Bitcoin, the tracer does not expand an address that was spent in the same transaction as
a **high-activity** address. That means one with at least `high_activity_tx_count`
confirmed transactions (default 1,000; a trace parameter), or whose history already exceeded
the fetch cap.

* **Backward traces:** the transaction checked is the one that brought the trace to the address.
* **Forward traces:** the transactions checked are the ones in which the address spends the value.
* **The address becomes a HIGH_ACTIVITY endpoint** with a note naming the busy co-input, its
  transaction count and the transaction id, plus "review manually".
* **CoinJoin-like transactions are excluded,** as for D-COSPEND, since their inputs have many
  controllers.
* **Cost:** at most 8 extra lookups per address, using Esplora's `/address/{a}` statistics
  call, recorded as evidence like everything else (replayable).

This is the same common-input-ownership reasoning as D-COSPEND, used only to **stop** a trace,
never to attribute. It can only remove claims, never add them.

## Effect on the benchmark

BM-10 changed from WRONG (Binance A/93) to NOT_FOUND. The busy Kraken wallets are reported at
hop 1 as likely unlabelled services. No other Bitcoin case changed outcome.

Policy weights were not changed. `docs/benchmark.md` shows results after this ADR. The run
before it is described here.

## Consequences

- Fewer confident wrong answers for "funding in" traces through exchanges whose wallets are not
  labelled. The price is that the trace stops at the exchange's wallet without naming it. Only
  a label can name an exchange, and that label must come from a source.
- Account-model chains are unaffected: they have no multi-input transactions. Their hot-wallet
  sweeps are handled by R-SWEEP and the high-activity endpoint.
