"""Post-trace analysis: confidence scores, nearest VASPs, typologies, risk, roles, clusters, alerts.

Everything here is a deterministic function of the traces, the verifications and the
scoring policy, so it is part of the reproducible findings.
"""

from __future__ import annotations

from .chains.base import Verification
from .crosschain import CrossChainLink
from .domain import EndpointKind, Frozen, Grade
from .policy import ScoringPolicy
from .profiles import AddressProfile, Cluster, build_profiles
from .risk import Alert, RiskAssessment, alerts_for, flow_risks, wallet_risk
from .scoring import ConfidenceScore, score_endpoint
from .sourcetrust import SourceTrust
from .tracer import TraceResult
from .typologies import Typology, TypologyHit, detect


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
        conflicted = e.attribution.grade is Grade.X or not e.attribution.entity_id
        key = f"conflict:{e.address}" if conflicted else e.attribution.entity_id
        # Resolved owners first (nearest, then strongest evidence); conflicted owners after them.
        rank_key = (conflicted, e.hops, -s.score, e.address)
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


def _link_hits(trace: TraceResult, links: list[CrossChainLink]) -> list[TypologyHit]:
    endpoint_ids = {e.id for e in trace.endpoints}
    hits = []
    for link in links:
        if link.endpoint_id not in endpoint_ids:
            continue
        destination = f"{link.to_chain.value if link.to_chain else link.to_chain_code}:{link.to_address or '(recipient unresolved)'}"
        hits.append(
            TypologyHit(
                typology=Typology.CHAIN_HOPPING,
                rule="T-CHAINHOP",
                addresses=tuple(a for a in (link.from_address, link.to_address) if a),
                tx_hashes=tuple(t for t in (link.from_tx, link.to_tx) if t),
                detail=f"{link.protocol} swap {link.asset_in} -> {link.asset_out} to {destination} ({link.rule})",
            )
        )
    return hits


def analyze(
    traces: list[TraceResult],
    verifications: dict[str, Verification],
    policy: ScoringPolicy,
    trust: SourceTrust | None,
    links: list[CrossChainLink] | None = None,
    freezable_assets: frozenset[str] = frozenset(),
) -> tuple[list[TraceAnalysis], list[RiskAssessment]]:
    per_trace = []
    for trace in traces:
        scores = {}
        for e in trace.endpoints:
            s = score_endpoint(e, verifications, policy)
            if s is not None:
                scores[e.id] = s
        hits = detect(trace, policy) + _link_hits(trace, links or [])
        hits = sorted({(h.rule, h.addresses, h.tx_hashes): h for h in hits}.values(), key=lambda h: (h.rule, h.addresses, h.tx_hashes))
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
                alerts=tuple(alerts_for(trace, subject_risks[(trace.chain.value, trace.subject)], freezable_assets)),
            )
        )
    return analyses, [subject_risks[k] for k in subjects]
