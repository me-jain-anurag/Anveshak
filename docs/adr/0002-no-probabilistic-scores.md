# ADR-0002: Evidence grades and points rubrics, not ML or probabilistic scores

- Status: accepted (amended by ADR-0012 for the confidence and risk scores)
- Date: 2026-09-30

## Context

The problem statement asks for "automated tagging and confidence scoring for suspected VASPs"
and "risk classification". The obvious implementation is an ML classifier (GNNs on Elliptic-
style datasets) or an LLM that "assesses" a wallet. The output would be a number like 0.87 or
a paragraph of prose.

Neither can be defended as evidence:

- A model probability cannot be explained per case ("why 0.87 and not 0.6?").
- It changes when the model is retrained, so the same case gives different answers.
- It is calibrated on datasets whose labels are themselves noisy (see "Clean Up the Mess", references §2).
- Generated prose can state things that no source supports.

The team's requirement is explicit: robust, solid, hallucination-proof.

## Decision

1. **Nothing in the evidence path is produced by ML or an LLM.** No model output ever becomes
   a fact, a label, a grade, a score or a sentence in a report.
2. **Attribution strength is a categorical grade** (A attested · B corroborated · C single-source
   · X conflicted). It is computed by fixed rules from *who* makes a claim (source class, verified
   by the source-trust check, ADR-0005) and *whether independent sources agree*.
3. **The confidence and risk scores the PS asks for are points rubrics** (ADR-0012). They are
   integer points for listed pieces of evidence, with weights in a versioned policy file. Every
   point is itemised in the report and recomputable by hand. Reports state in plain words that
   they are not probabilities.
4. **"Unknown" is a valid answer.** Every trace reports what it did *not* examine (coverage).

## Alternatives considered

- *GNN classifier producing P(exchange).* Rejected: not explainable per case, not reproducible across retraining.
- *LLM investigator agent (RiskTagger, LOCARD).* Rejected for the evidence path: non-deterministic,
  can invent links. Acceptable later *only* as an offline lead generator whose suggestions an
  analyst must confirm, never shown as findings.
- *No score at all.* Rejected: the PS explicitly requires confidence scoring.

## Consequences

- Recall is lower than a vendor ML model on unlabelled services. The system says "high-activity,
  unlabelled — review manually" instead of guessing. Coverage grows through better labels and
  VASP attestations (ADR-0005), not through guessing.
- Every number in a report can be recomputed from the report itself and the policy file.
