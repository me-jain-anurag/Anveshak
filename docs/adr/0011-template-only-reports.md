# ADR-0011: Template-only reports with hashes and a BSA s.63 certificate template

- Status: accepted
- Date: 2026-09-30

## Context

Investigation reports are read by people who cannot re-run the software. Generated prose (LLM
summaries) is fluent but can assert things the data does not support.

## Decision

- Reports are rendered from **fixed Jinja2 templates**. Every sentence is template text or a field of the findings.
- The report explains grades and scores in plain words, and states that scores are not probabilities.
- The report includes, for every trace: endpoints, confidence breakdowns, paths with per-transfer
  verification status and evidence ids, attribution evidence (every label with its source, the
  class claimed versus counted, and source-check notes), typologies, clusters, roles, risk flags,
  balances, and **coverage and limitations**.
- The HTML's SHA-256 is written to `<case>.html.sha256` (a document cannot contain its own hash).
  The findings JSON is written next to it.
- Section 8 is a **template** for the two-part certificate under s.63 BSA 2023 (Part A: producer;
  Part B: expert), pre-filled with the findings hash and method references. It must be verified
  with the legal cell before use.
- Synthetic (demo) reports carry a watermark and a banner: "SYNTHETIC — NOT EVIDENCE".

## Consequences

- Reports are longer than a vendor PDF, but every claim can be followed back to its source.
