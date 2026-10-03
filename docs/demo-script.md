# Demo script

A live demo must not depend on a conference network or on public-API rate limits.

* **Before the event:** run the live cases once, recording every API response into a *demo pack*.
* **On stage:** replay the packs offline. Replay is exact: the findings hash matches the live run.

## 1. Before the event (online, ~10–30 minutes)

```bash
# one pack per scenario; use addresses from public documents (e.g. the benchmark cases), never real victims' data
anveshak trace --chain bitcoin --address bc1px0jrexmzlefw8slm3mxldz9sxchxxr58d02nc5udeu645e27awaq7ang6k \
    --direction in --max-hops 2 --max-expansions 40 \
    --case-ref "DEMO — D.D.C. 1:25-cv-02967 ¶43 (Kraken withdrawal)" --pack demo/packs/btc-kraken
anveshak trace --chain tron --address T... --case-ref "DEMO — Tron USDT" --pack demo/packs/tron-usdt
anveshak replay --pack demo/packs/btc-kraken      # must print RESULT: MATCH
```

A pack holds:

* `evidence/`: every raw response, stored under its sha256;
* `case.json`: the findings;
* the sealed HTML report and its `.sha256`;
* `PACK.json`: case id, findings hash, label snapshot, number of evidence objects.

Packs contain only public blockchain data and public document references. If a pack is
committed, check its size first (`du -sh demo/packs/*`).

## 2. On stage (offline)

| Step | Command / action | What to say |
|---|---|---|
| 1 | `anveshak demo` | "A synthetic scenario that exercises every rule. It is watermarked *not evidence* and can never be submitted." |
| 2 | `anveshak replay --pack demo/packs/btc-kraken` → `RESULT: MATCH` | "Recorded from live public APIs. Every response is stored under its hash; replaying gives the identical findings hash. Anyone can re-check it." |
| 3 | `anveshak pack load demo/packs/btc-kraken`, then `anveshak serve`, open http://127.0.0.1:8000 | Dashboard → Cases → the pack case |
| 4 | Show the trace's endpoints | "The court filing says the victim withdrew from Kraken. Kraken's busy wallets at hop 1 are flagged as a likely unlabelled service. The engine does **not** claim Binance just because Binance money once passed through Kraken's wallets (ADR-0024). Without a sourced Kraken label, it says *unknown service* instead of guessing." |
| 5 | Recommendations table | Readiness, grade, confidence (points, not probability), Sahyog intermediary id or verified alternative channel, outcome history |
| 6 | Full report ↗ | Paths with per-transaction verification, attribution evidence with sources, coverage ("what we did not examine"), BSA s.63 certificate template |
| 7 | `python tools/mock_sahyog/mock_sahyog.py …` against the running server (or `pytest tests/test_sahyog.py -q`) | Sahyog → report → signed callback → outcome statuses → VASP reply confirms → re-run → grade A, ready |
| 8 | Lookup tab → Screen several addresses | Instant label / risk / prior-case screen, no chain calls |
| 9 | Analytics tab | Outcomes per intermediary, VASPs reached, typologies |
| 10 | `docs/benchmark.md` | Results against court filings, including the misses and why |

## 3. Things not to claim on stage

* That the confidence score is a probability. It is a points rubric.
* That a VASP was reached when the trace stopped at a high-activity address. Say "likely
  unlabelled service, flagged for review".
* That benchmark misses are hidden. They are listed, with the reason for each.

## 4. Checks before going on stage

* `pytest -q` passes.
* `anveshak replay --pack …` prints MATCH for every pack.
* The dashboard loads with the network disconnected. Cytoscape.js is served from
  `anveshak/static/vendor/`, not from a CDN.
* If API keys are configured, the dashboard's **API key** button holds a key with `case:read`.
