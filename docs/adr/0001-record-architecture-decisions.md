# ADR-0001: Record architecture decisions

- Status: accepted
- Date: 2026-09-30

## Context

The output of this system may be relied on in investigations and courts. Reviewers (judges,
LEA experts, defence counsel) will ask *why* it behaves as it does, not only
*what* it does. Decisions also need to survive changes in the team.

## Decision

Every significant design decision is recorded as a short ADR in `docs/adr/`, numbered, never
deleted (superseded ADRs are marked as such). Each states context, decision, alternatives
considered, and consequences, and links the code and references it relies on.

## Consequences

- Rule ids printed in reports (G-A1, R-SWEEP, X-THORCHAIN …) can be traced to the ADR that defines them.
- Changing a rule means writing or updating an ADR, which makes silent behaviour changes visible in review.
