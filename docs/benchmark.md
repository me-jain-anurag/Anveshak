# Ground-truth benchmark: does Anveshak find the right VASP?

PS 26182 asks for the *nearest* VASP and a confidence for it. This benchmark checks both
against cases where a public document states which VASP was on one side of specific on-chain funds.

## How the cases were built

* **Only public documents, only what they say.** Every case in `benchmarks/cases/*.yaml` cites
  a U.S. federal court filing published on justice.gov. Each case records the URL, the sha256 of
  the PDF as downloaded on 2026-10-04, the paragraph and PDF page, and a verbatim quote. The
  quotes were checked by script against the text extracted from the PDF. (U.S. federal
  government works are in the public domain, so quoting is fine.)
* **What a case asserts:** the subject address, a direction, and the expected VASP(s).
  * `out`: value leaving the subject reaches the VASP. For example, the subject is a deposit
    address at the VASP, or sent funds to a swap service.
  * `in`: the subject was funded from the VASP, e.g. withdrawals from an account there.
* **Where the document says less than an address-level fact, the case says so.**
  * The S.D.N.Y. complaint makes its Nobitex statement about a cluster of seven addresses, and
    only "upon information and belief". BM-14 and BM-15 are marked `claim_scope: cluster`.
  * The D.D.C. OKX complaint does not name the blockchain. Before adding those cases, we
    confirmed USDT transfers on Ethereum into each stated deposit address on the stated dates.

| Source | Cases | Chain | Expected VASP |
|---|---|---|---|
| D.D.C. verified complaint, 225,364,961 USDT (2025), ¶178–¶186 | BM-01 … BM-09 | Ethereum | OKX (5 deposit addresses, 4 withdrawal destinations) |
| D.D.C. verified complaint, Case 1:25-cv-02967, ¶43, ¶45, ¶51, ¶52 | BM-10 … BM-13 | Bitcoin | Kraken / Crypto.com, SwapSpace (×2), Strike |
| S.D.N.Y. verified complaint, Case 1:26-cv-08010, fn. 8, ¶46 | BM-14, BM-15 | Tron | Nobitex (cluster-level claim) |

That is 15 cases across 3 chains, including Tron USDT. Tron is represented only by the two
cluster-level cases. We found no public document naming an exchange deposit address for specific Tron USDT. Adding such cases
is the first item of future work.

## How it is scored

Run it with `anveshak benchmark`. The engine traces each case live with the stored labels and
records all evidence, so `anveshak benchmark --replay` reproduces a run offline.

| Outcome | Meaning |
|---|---|
| HIT@1 | The nearest VASP (rank 1) is an expected one. |
| HIT | An expected VASP was reached, but not as the nearest. |
| WRONG | Only other VASPs were reached. |
| NOT_FOUND | No VASP was reached. The coverage reason is shown. |
| SKIPPED | The chain cannot be traced with the current configuration. |

Correct and other attributions are reported with their grades and confidence. This shows
whether confidence separates right from wrong. Policy weights are **not** tuned to this
benchmark. Any change would go through `data/scoring_policy.yaml` and an ADR.

### Configuration needed per chain

* **Bitcoin and Tron:** no key needed (Esplora, TronGrid).
* **Ethereum** needs one of:
  * `ETHERSCAN_API_KEY` (free tier), for full history; or
  * a JSON-RPC endpoint that serves historical `eth_getLogs`, for a window-limited log scan.
    For example, `ANVESHAK_RPC_ETHEREUM=https://rpc.mevblocker.io`, which on 2026-10-04 returned
    the 2023 transfers used in BM-01. The public `ethereum-rpc.publicnode.com` refused archive
    log queries without a token on that date.

  Without either, BM-01 … BM-09 are reported as SKIPPED, not as misses.

## Results

The table below is rewritten by `anveshak benchmark`. Read "not found" and "skipped" rows as
coverage gaps, not as wrong answers.

* SwapSpace, Strike and Nobitex are in neither the directory nor the shipped label sets, so a
  miss there measures label coverage, not tracing.
* The Ethereum cases need a configured source (see above). On Windows hosts, see
  [Microsoft Defender note](adr/0021-keyless-evm-rpc-log-scan.md#microsoft-defender-false-positive):
  the EVM cases are best run on Linux or in Docker.

<!-- BENCHMARK-RESULTS:START -->
_Not run yet._
<!-- BENCHMARK-RESULTS:END -->

## Limits of this benchmark

* **Small and hand-picked from enforcement documents.** These describe flows that investigators
  already traced successfully. That makes the benchmark easier than a random case.
* **U.S. documents only.** Indian court documents with on-chain details were not found in
  public form.
* **An expected VASP can be hard to label.** Small swap services (SwapSpace) are rarely in
  public label sets. A label import for them would need a primary source of the same kind as
  the shipped ones.
