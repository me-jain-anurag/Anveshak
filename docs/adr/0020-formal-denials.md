# ADR-0020: A VASP's formal denial overrides other labels (rule G-N1)

- Status: accepted
- Date: 2026-10-04

## Context

Requests routed through Sahyog get replies. A reply can confirm that an address belongs to the
VASP, which is grade A evidence (G-A1). It can also say "this address is not ours". Before this
ADR the system had no way to record the second kind of reply. A curated or crawled label
naming that VASP would keep producing requests to the wrong recipient.

## Decision

- `Label.denies = true` records a denial. It names the entity, has no category and carries no
  risk flags. Like every label, it needs a primary source (the reply reference) and a date.
- **Rule G-N1**, applied before all other grading rules:
  1. A denial counts only if it is entity-attested after the source-trust check (ADR-0005),
     i.e. from the entity's official channel or a formal reply recorded by an investigator.
     A forum post "denying" ownership is ignored, with a note saying so.
  2. If it counts, every label naming that entity (aliases included) is disregarded for that
     address. Labels naming other entities still stand.
     * Example: a conflict between ex1 and ex2 becomes a grade C attribution to ex2.
  3. A later dated confirmation from the same entity supersedes the denial. A later denial
     supersedes an earlier confirmation.
  4. A confirmation and a denial with the **same date** are a conflict: grade X, rule G-N1.
     Nothing is routed until an analyst gets a clarification.
- **Recording a reply:**
  * Use `POST /v1/attestations` with `polarity: confirms | denies` and the `sahyog_reply_id`,
    or `anveshak labels attest --denies --reply-id …`.
  * Only the document reference is stored, never personal data from the reply.
- **Effect on findings:** recording a reply changes future findings only. Existing cases keep
  their findings hash. A re-run creates a new case linked to the old one (`parent_case_id`).

## Consequences

- The feedback loop works in both directions, so wrong attributions shrink as replies arrive.
- Denials live in the label snapshot (`var/labels/attestations.jsonl`). That snapshot's hash is
  part of every case, so replays stay exact.
- Tests: denial overrides curated labels; denial removes only the denying entity; later
  confirmation supersedes; same-date conflict gives X; weak-source denial ignored; store
  round trip; end-to-end denial through the API removes the recommendation.
