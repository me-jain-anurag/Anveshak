# ADR-0010: Request drafts with officer approval; Sahyog gateway behind an adapter

- Status: accepted
- Date: 2026-09-30

## Context

The PS asks the system to "assist investigators in automatically routing lawful disclosure or
freezing requests to the correct VASP through the SAHYOG portal". Sending legal requests is a
consequential, outward-facing act, and I4C has not published a public Sahyog API specification.

## Decision

- The engine produces **drafts**, grouped by VASP per subject and direction:
  - DISCLOSURE: KYC and records of the credited or debited account.
  - FREEZE: a debit hold on those accounts.
  - ISSUER_FREEZE: a token-level freeze by the stablecoin issuer, when funds are still held.
- **Status rules** (in order):
  - BLOCKED: grade X, or every path failed verification.
  - ANALYST_REVIEW: no fully verified path to grade A/B; confidence below the policy threshold
    (`routing.ready_min_confidence`, and `freeze_min_confidence` for freezes); no directory entry;
    or no verified contact channel. Issuer freezes always.
  - READY_FOR_APPROVAL: everything above satisfied.
- **Nothing is sent automatically.** An officer approves a READY draft (name and ID recorded). Only
  then does the `SahyogGateway` receive it. The only implementation today is `DryRunSahyogGateway`:
  it writes the exact payload plus its sha256 to `var/outbox/` for manual upload. A real client
  replaces it behind the same `submit` signature once I4C provides the spec.
- The VASP directory (`data/vasp_directory.yaml`) stores contact channels and facts (FIU-IND
  registration, Sahyog onboarding, official channels), each with source URL and check date.
- Legal provisions are a placeholder the officer fills in. The draft cites what the Bitget press
  release reports about Sahyog, marked "verify with the legal cell".

## Consequences

- Automation stops at the point where legal accountability starts.
- Directory coverage limits READY drafts. Each verified channel added improves throughput.
