# ADR-0004: Content-addressed evidence store and deterministic replay

- Status: accepted
- Date: 2026-09-30

## Context

Blockchain explorers and APIs change, rate-limit, re-index and go offline. A report produced
today must still be checkable next year, and it must be possible to show that the tool did not
invent data.

## Decision

- Every HTTP response from a data source is stored **byte-for-byte** under its SHA-256
  (`var/evidence/objects/`). A request index maps each (redacted) request to the response it received.
- Parsed records carry the hash (`Transfer.evidence_id`, `Verification.evidence_ids`,
  `Balance.evidence_id`, `CrossChainLink.evidence_id`, intel labels' `dataset_ref`).
- Every read re-hashes the object. A modified object raises `EvidenceCorrupted`.
- **API keys never reach disk**: secret query parameters and headers are redacted before a request
  is recorded or used as an index key.
- `CaseFindings` contains only what the conclusions depend on (request, label/directory/policy
  snapshots, traces, verifications, analyses, routing, cross-chain links, evidence ids). It has no
  wall-clock times or random ids. Its SHA-256 is the **findings hash**.
- `anveshak replay <case_id>` re-runs a case with a `ReplayFetcher` (no network) and must reproduce
  the findings hash exactly. This is tested (`tests/test_adapters.py::test_live_run_replays_to_identical_findings_hash`).

## Consequences

- The evidence store grows with use. It is append-only and can be archived per case.
- Replay needs the same label, directory and policy snapshots. Their hashes are in the findings,
  and replay reports any difference.
