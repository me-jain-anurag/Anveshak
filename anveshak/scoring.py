"""Confidence score for "value traced from the subject reached entity E" (ADR-0012).

    score = attribution points + path-verification points + proximity points + corroboration points

each taken from data/scoring_policy.yaml and itemised with the evidence that earned it.
Two hard rules override the sum: a conflicted attribution (grade X) scores 0, and a path
with any transfer that failed verification (MISMATCH) scores 0.

This is an evidence *ranking*, not a probability. It exists because the problem statement
asks for confidence scoring; it is built so that every point can be recomputed by hand.
"""

from __future__ import annotations

from .chains.base import Verification, VerificationStatus
from .domain import Attribution, Endpoint, EndpointKind, Frozen, Grade, SourceClass
from .policy import ScoringPolicy


class ScoreItem(Frozen):
    component: str
    points: int
    reason: str
    counted: bool = True


class ConfidenceScore(Frozen):
    endpoint_id: str
    address: str
    entity_id: str | None
    entity_name: str | None
    score: int
    band: str
    grade: Grade | None
    items: tuple[ScoreItem, ...]
    hard_rule: str | None = None
    policy_version: str


def attribution_points(att: Attribution, policy: ScoringPolicy) -> ScoreItem:
    p = policy.confidence.attribution
    classes = att.effective_classes or tuple(l.source_class for l in att.labels)
    pairs = list(zip(att.labels, classes))
    if any(c is SourceClass.ENTITY_ATTESTED for _, c in pairs):
        src = next(l for l, c in pairs if c is SourceClass.ENTITY_ATTESTED)
        base, why = p.entity_attested, f"entity-attested source ({src.source_id}: {src.primary_source})"
    elif any(c is SourceClass.AUTHORITY for _, c in pairs):
        src = next(l for l, c in pairs if c is SourceClass.AUTHORITY)
        base, why = p.authority, f"public-authority source ({src.source_id}: {src.primary_source})"
    else:
        keys = sorted({l.independence_key for l, c in pairs if c is SourceClass.CURATED})
        if keys:
            base = min(p.curated_cap, p.curated_first + p.curated_each_additional * (len(keys) - 1))
            why = f"{len(keys)} independent curated source(s): {', '.join(keys)}"
        else:
            base, why = p.weak_only, "weak sources only"
    if att.derived is not None:
        derived = min(p.derived_cap, max(0, base - p.derived_penalty))
        return ScoreItem(
            component="attribution",
            points=derived,
            reason=f"derived via {att.derived.rule} from {att.derived.anchor_address} whose attribution earns {base} ({why}); "
            f"minus {p.derived_penalty} for the derivation, capped at {p.derived_cap}",
        )
    return ScoreItem(component="attribution", points=base, reason=why)


def path_points(endpoint: Endpoint, verifications: dict[str, Verification], policy: ScoringPolicy) -> tuple[ScoreItem, bool]:
    """Returns the item and whether a MISMATCH (hard rule) was found."""
    p = policy.confidence.path
    statuses = [verifications[t].status if t in verifications else VerificationStatus.UNVERIFIABLE for t in endpoint.path]
    n = len(statuses)
    if VerificationStatus.MISMATCH in statuses:
        bad = statuses.count(VerificationStatus.MISMATCH)
        return ScoreItem(component="path", points=0, reason=f"{bad} of {n} transfer(s) failed independent verification"), True
    if VerificationStatus.ERROR in statuses:
        return ScoreItem(component="path", points=p.some_error, reason=f"verification could not complete for {statuses.count(VerificationStatus.ERROR)} of {n} transfer(s)"), False
    if VerificationStatus.UNVERIFIABLE in statuses:
        return ScoreItem(component="path", points=p.some_unverifiable, reason=f"{statuses.count(VerificationStatus.UNVERIFIABLE)} of {n} transfer(s) cannot be re-checked with the configured source"), False
    return ScoreItem(component="path", points=p.all_verified, reason=f"all {n} transfer(s) re-verified against transaction-level data"), False


def proximity_points(endpoint: Endpoint, policy: ScoringPolicy) -> ScoreItem:
    p = policy.confidence.proximity
    points = p.by_hops.get(endpoint.hops, p.beyond)
    return ScoreItem(component="proximity", points=points, reason=f"{endpoint.hops} hop(s) from the subject")


def score_endpoint(endpoint: Endpoint, verifications: dict[str, Verification], policy: ScoringPolicy) -> ConfidenceScore | None:
    if endpoint.kind not in (EndpointKind.VASP, EndpointKind.SERVICE) or endpoint.attribution is None:
        return None
    att = endpoint.attribution
    items: list[ScoreItem] = []
    hard: str | None = None
    if att.grade is Grade.X:
        hard = "grade X — owners conflict; score fixed at 0"
        items.append(ScoreItem(component="attribution", points=0, reason=f"conflicting owners: {', '.join(att.conflicts)}"))
    else:
        items.append(attribution_points(att, policy))
    path_item, mismatch = path_points(endpoint, verifications, policy)
    items.append(path_item)
    if mismatch and hard is None:
        hard = "a transfer on the path failed verification (MISMATCH); score fixed at 0"
    items.append(proximity_points(endpoint, policy))
    if endpoint.adjacent_role and "R-SWEEP" in endpoint.adjacent_role:
        items.append(ScoreItem(component="corroboration", points=policy.confidence.corroboration.deposit_sweep_pattern, reason=endpoint.adjacent_role))
    total = 0 if hard else max(0, min(100, sum(i.points for i in items)))
    if hard:
        items = [i.model_copy(update={"counted": False}) for i in items]
    return ConfidenceScore(
        endpoint_id=endpoint.id,
        address=endpoint.address,
        entity_id=att.entity_id,
        entity_name=att.entity_name,
        score=total,
        band=policy.confidence.band(total) if not hard else "none",
        grade=att.grade,
        items=tuple(items),
        hard_rule=hard,
        policy_version=policy.version,
    )
