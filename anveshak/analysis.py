"""Post-trace analysis: confidence scores, nearest VASPs, typologies, risk, roles, clusters, alerts.

Everything here is a deterministic function of the traces, the verifications and the
scoring policy, so it is part of the reproducible findings.
"""

from __future__ import annotations

from .chains.base import Verification
from .domain import EndpointKind, Frozen, Grade
from .policy import ScoringPolicy
from .profiles import AddressProfile, Cluster, build_profiles
from .risk import Alert, RiskAssessment, alerts_for, flow_risks, wallet_risk
from .scoring import ConfidenceScore, score_endpoint
from .sourcetrust import SourceTrust
from .tracer import TraceResult
from .typologies import TypologyHit, detect


class NearestVasp(Frozen):
    rank: int
    entity_id: str | None
    entity_name: str | None
    address: str
    deposit_address: str | None
    hops: int
    grade: Grade
    confidence: int
    band: str
    endpoint_id: str


class TraceAnalysis(Frozen):
    chain: str
    subject: str
    direction: str
    confidences: tuple[ConfidenceScore, ...]
    nearest_vasps: tuple[NearestVasp, ...]
    typologies: tuple[TypologyHit, ...]
    flow_risks: tuple[RiskAssessment, ...]
    profiles: tuple[AddressProfile, ...]
    clusters: tuple[Cluster, ...]
    alerts: tuple[Alert, ...]


def _nearest(trace: TraceResult, scores: dict[str, ConfidenceScore]) -> list[NearestVasp]:
    best: dict[str, tuple] = {}
    for e in trace.endpoints:
        if e.kind is not EndpointKind.VASP or e.attribution is None or e.id not in scores:
            continue
        s = scores[e.id]
        key = e.attribution.entity_id if e.attribution.grade is not Grade.X and e.attribution.entity_id else f"conflict:{e.address}"
        rank_key = (e.hops, -s.score, e.address)
        if key not in best or rank_key < best[key][0]:
            best[key] = (rank_key, e, s)
    ordered = sorted(best.values(), key=lambda v: v[0])
    return [
        NearestVasp(
            rank=i,
            entity_id=e.attribution.entity_id,
            entity_name=e.attribution.entity_name,
            address=e.address,
            deposit_address=e.adjacent_address if e.adjacent_role else None,
            hops=e.hops,
            grade=e.attribution.grade,
            confidence=s.score,
            band=s.band,
            endpoint_id=e.id,
        )
        for i, (_, e, s) in enumerate(ordered, start=1)
    ]


def analyze(traces: list[TraceResult], verifications: dict[str, Verification], policy: ScoringPolicy, trust: SourceTrust | None) -> tuple[list[TraceAnalysis], list[RiskAssessment]]:
    per_trace = []
    for trace in traces:
        scores = {}
        for e in trace.endpoints:
            s = score_endpoint(e, verifications, policy)
            if s is not None:
                scores[e.id] = s
        hits = detect(trace, policy)
        profiles, clusters = build_profiles(trace, hits)
        per_trace.append((trace, scores, hits, profiles, clusters))

    subject_risks: dict[tuple[str, str], RiskAssessment] = {}
    subjects = sorted({(t.chain.value, t.subject) for t in traces})
    for chain, subject in subjects:
        related = [(t, h) for t, _, h, _, _ in per_trace if t.chain.value == chain and t.subject == subject]
        subject_risks[(chain, subject)] = wallet_risk(chain, subject, [t for t, _ in related], [x for _, h in related for x in h], policy, trust)

    analyses = []
    for trace, scores, hits, profiles, clusters in per_trace:
        analyses.append(
            TraceAnalysis(
                chain=trace.chain.value,
                subject=trace.subject,
                direction=trace.params.direction.value,
                confidences=tuple(sorted(scores.values(), key=lambda s: (-s.score, s.endpoint_id))),
                nearest_vasps=tuple(_nearest(trace, scores)),
                typologies=tuple(hits),
                flow_risks=tuple(flow_risks(trace, hits, policy, trust)),
                profiles=tuple(profiles),
                clusters=tuple(clusters),
                alerts=tuple(alerts_for(trace, subject_risks[(trace.chain.value, trace.subject)])),
            )
        )
    return analyses, [subject_risks[k] for k in subjects]
