# ADR-0012: Confidence and risk scores as versioned points rubrics

- Status: accepted (amends ADR-0002)
- Date: 2026-09-30

## Context

The PS explicitly requires "confidence scoring for suspected VASPs" and "risk classification of
wallets and transaction flows". ADR-0002 rules out model probabilities. We need scores that a
court can follow.

## Decision

Scores are **sums of integer points for listed evidence**. Weights are in `data/scoring_policy.yaml`
(versioned, hashed into every case). Each contribution is itemised in the report and the API.

**Confidence (0–100)**: "value traced from the subject reached entity E"

| Component | Max | Points |
|---|---|---|
| attribution | 60 | entity-attested 60 · authority 55 · curated 30 (+15 per further independent source, cap 50) · weak only 10 · derived (D-COSPEND, D-EVM-KEY): anchor minus 15, cap 45 |
| path | 25 | all transfers verified 25 · some unverifiable 10 · verification error 5 |
| proximity | 10 | 1 hop 10, 2 → 8, 3 → 6, 4 → 4, 5 → 2 |
| corroboration | 5 | deposit-sweep pattern (R-SWEEP) observed |

Hard rules (not configurable): grade X gives 0, and any MISMATCH on the path gives 0.
Bands: high ≥ 80, moderate ≥ 60, low ≥ 40, very low < 40.

**Risk (0–100)**, for the subject wallet and per traced flow:
`min(100, max(direct flag, exposure, service) + min(30, typologies))`

- direct: a flag on the subject (sanctioned 100, terrorism 100, ransomware 90, dark web / hack 80,
  scam and fraud family 70 …) × a source weight (authority/entity 100%, curated 75%, weak 25%)
- exposure: a flag on a traced counterparty × source weight × distance decay (1 hop 80%, 2 → 60%, 3 → 40%, 4 → 20%, beyond 10%)
- service: a mixer / market (60), CoinJoin (50), gambling / bridge (20) reached, decayed by distance
- typology: peel chain 15, chain-hopping 15, pass-through 10, fan-in 10, fan-out 10 (each counted once)

Levels: severe ≥ 80, high ≥ 60, medium ≥ 40, low ≥ 1, `none_observed` = 0 ("no indicators
observed", never "clean").

Routing thresholds in the same policy: a disclosure is READY at ≥ 60 confidence, a freeze at ≥ 70.

## Alternatives considered

- Probability from a classifier: rejected (ADR-0002).
- Grades only: rejected. The PS requires a score, and investigators need to rank many results.

## Consequences

- The weights are policy choices, not empirical truths, and are documented as such. Changing
  them is a reviewed change to a versioned file.
- Old cases keep the policy hash they were scored under.
