# ADR-0019: Sahyog is the client — recommendations out, outcomes and replies back in

- Status: accepted
- Date: 2026-10-04

## Context

The PS asks that attribution results be routed into the Sahyog workflow, so that requests
reach the correct intermediary. Sahyog is the government portal through which LEAs send
notices to intermediaries. Officers already work in it: they approve requests and send them
there, and the replies come back there. If Anveshak called a Sahyog API to send requests, it
would duplicate the portal's approval workflow, and no public Sahyog API exists to call.

The direction of integration is therefore Sahyog → Anveshak. Sahyog (or an LEA dashboard)
calls Anveshak's API and acts on what it returns.

## Decision

1. **Intake.** `POST /v1/sahyog/reports` takes the wallets reported for one investigation,
   validates each one by format and checksum, detects its chains, and queues one case. The
   case carries the Sahyog reference and the agency id.
2. **Recommendations, schema `anveshak.recommendation/v1`**, one per (intermediary, request type).
   They come from `GET /v1/cases/{id}/recommendations` and in the signed completion callback.
   Each recommendation contains:
   * readiness (`ready_for_approval` / `analyst_review` / `blocked`) and its reasons;
   * the intermediary, with **Sahyog's own intermediary id** when known;
   * `alternative_channels`: verified contact channels outside Sahyog (e.g. a VASP's
     law-enforcement portal), for intermediaries not on Sahyog;
   * the accounts, the transactions and the attribution basis (grade, rules, confidence);
   * the draft request text, and the current outcome status with its full history.

   No personal data is ever included.
3. **Intermediary ids** come from Sahyog's own list (`PUT /v1/directory/sahyog-intermediaries`).
   * Matching against the directory is **exact only**: entity id, display name or alias,
     ignoring case. An explicit `entity_id` in the upload also counts.
   * Unmatched and ambiguous entries are returned to the uploader, never guessed. A request
     sent to the wrong intermediary is worse than one without an id.
4. **Outcomes come back** through `POST /v1/cases/{id}/recommendations/{rid}/status`
   (`submitted`, `acknowledged`, `responded`, `complied`, `declined`, `funds_frozen` …).
   * The table is append-only: SQLite triggers reject UPDATE and DELETE.
   * Outcomes appear in the dashboard and in analytics, per intermediary.
5. **Replies become evidence.** A VASP's reply is recorded with `POST /v1/attestations`. It
   either **confirms** an address (grade A, rule G-A1) or **denies** it (rule G-N1, ADR-0020),
   and carries the Sahyog reply id. `POST /v1/cases/{id}/rerun` then re-runs the same request
   with the new label. The old case and its findings hash stay unchanged.
6. **Approvals happen in Sahyog.** Anveshak's own approve endpoint is off by default and returns
   403 with directions. `ANVESHAK_STANDALONE_APPROVALS=1` enables it for pilots without Sahyog.
   It then writes to a dry-run outbox as before (ADR-0010).
7. **Screening.** `POST /v1/screen` answers synchronously from labels and the case database
   only, with no chain calls. Sahyog can use it to check reported wallets at intake.
8. **A mock Sahyog client** (`tools/mock_sahyog`) drives the whole loop. The test suite uses it
   in-process: report → signed callback → intermediary ids → outcome statuses → confirming
   reply → re-run → grade A, ready for approval.

## Consequences

- Anveshak never sends anything to an intermediary. The officer's decision stays in the
  government's system of record.
- Callback signatures use `ANVESHAK_CALLBACK_SECRET` (HMAC-SHA256 over the exact body).
  Callback hosts must be on `ANVESHAK_CALLBACK_ALLOWLIST` and use https. Plain http is allowed
  only for a loopback test receiver.
- The schema is versioned. A breaking change would be `…/v2`, served alongside v1.
