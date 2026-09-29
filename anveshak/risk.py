"""Risk classification of wallets and transaction flows (ADR-0012), plus alerts.

Wallet risk (the subject):
    score = min(100, max(direct flag points, exposure points, service points) + min(cap, typology points))
      direct    — a risk flag on the subject itself: flag points × source weight
      exposure  — a flag on an address on a traced path: flag points × source weight × distance decay
      service   — a traced path reaching a mixer / darknet market / gambling / bridge / CoinJoin (decayed)
      typology  — laundering patterns detected in the subject's traced flows

Flow risk (one path, subject → endpoint): the same components restricted to that path.

Weights come from data/scoring_policy.yaml; every contribution is itemised. Level
"none_observed" means no indicator was observed in the traced data — never "clean".
"""

from __future__ import annotations

from enum import StrEnum

from .domain import Category, EndpointKind, Frozen, RiskFlag, RiskHit
from .policy import ScoringPolicy
from .scoring import ScoreItem
from .sourcetrust import SourceTrust
from .tracer import TraceResult
from .typologies import TypologyHit


class RiskAssessment(Frozen):
    chain: str
    address: str
    scope: str  # "wallet" or "flow:<endpoint id>"
    score: int
    level: str
    items: tuple[ScoreItem, ...]
    policy_version: str


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


class Alert(Frozen):
    severity: AlertSeverity
    rule: str
    chain: str
    addresses: tuple[str, ...]
    message: str


CRIME_FAMILIES = {
    RiskFlag.SANCTIONED: "A-SANCTIONS",
    RiskFlag.TERRORISM: "A-TERRORISM",
    RiskFlag.EXTREMISM: "A-TERRORISM",
    RiskFlag.RANSOMWARE: "A-RANSOMWARE",
    RiskFlag.DARK_WEB: "A-DARKWEB",
    RiskFlag.HACK: "A-HACK",
    RiskFlag.SCAM: "A-FRAUD",
    RiskFlag.INVESTMENT_FRAUD: "A-FRAUD",
    RiskFlag.PHISHING: "A-FRAUD",
    RiskFlag.PONZI: "A-FRAUD",
    RiskFlag.EXTORTION: "A-FRAUD",
    RiskFlag.ISSUER_FROZEN: "A-ISSUER-FROZEN",
}

SERVICE_KEYS = {Category.MIXER: "mixer", Category.MARKET: "market", Category.GAMBLING: "gambling", Category.BRIDGE: "bridge"}


def _flag_items(hit: RiskHit, policy: ScoringPolicy, trust: SourceTrust | None, percent: int, where: str) -> list[ScoreItem]:
    """One item per flag, using the best-supported label for that flag."""
    items = []
    for flag in hit.flags:
        best, best_label = 0, None
        for label in hit.labels:
            if flag not in label.risk_flags:
                continue
            klass = trust.effective(label, None)[0] if trust else label.source_class
            weight = policy.risk.flag_source_percent.get(klass, 0)
            if weight > best:
                best, best_label = weight, label
        if best_label is None:
            continue
        base = policy.risk.flag_points.get(flag, 0)
        points = base * best * percent // 10_000
        items.append(
            ScoreItem(
                component="direct" if where == "subject" else "exposure",
                points=points,
                reason=f"{flag} on {where} {hit.address} ({best_label.source_id}: {best_label.text}); {base} × source {best}% × distance {percent}%",
            )
        )
    return items


def _distances(trace: TraceResult) -> dict[str, int]:
    """Smallest hop count at which each address appears on any endpoint path."""
    by_id = {t.id: t for t in trace.transfers}
    dist: dict[str, int] = {trace.subject: 0}
    for e in trace.endpoints:
        for i, tid in enumerate(e.path, start=1):
            t = by_id[tid]
            nxt = t.receiver if trace.params.direction.value == "out" else t.sender
            dist[nxt] = min(dist.get(nxt, i), i)
    return dist


def _service_items(endpoints, policy: ScoringPolicy) -> list[ScoreItem]:
    items = []
    for e in endpoints:
        key = None
        if e.kind is EndpointKind.COINJOIN_LIKE:
            key, name = "coinjoin", "CoinJoin-like transaction"
        elif e.kind is EndpointKind.SERVICE and e.attribution and e.attribution.category in SERVICE_KEYS:
            key, name = SERVICE_KEYS[e.attribution.category], f"{e.attribution.category} {e.attribution.entity_name or ''}".strip()
        if key is None or key not in policy.risk.service_points:
            continue
        percent = policy.risk.exposure_percent(e.hops)
        base = policy.risk.service_points[key]
        items.append(ScoreItem(component="service", points=base * percent // 100, reason=f"path reaches {name} at {e.hops} hop(s); {base} × distance {percent}%"))
    return items


def _combine(items: list[ScoreItem], typology_items: list[ScoreItem], policy: ScoringPolicy) -> tuple[int, list[ScoreItem]]:
    base_items = items
    top = max(base_items, key=lambda i: i.points, default=None)
    typology_total = min(policy.risk.typology_cap, sum(i.points for i in typology_items))
    score = min(100, (top.points if top else 0) + typology_total)
    marked = [i.model_copy(update={"counted": i is top}) for i in base_items] + [i for i in typology_items]
    return score, marked


def _typology_items(hits: list[TypologyHit], policy: ScoringPolicy) -> list[ScoreItem]:
    seen, items = set(), []
    for h in hits:
        points = policy.risk.typology_points.get(h.typology.value, 0)
        if not points or h.typology in seen:
            continue  # each typology counts once
        seen.add(h.typology)
        items.append(ScoreItem(component="typology", points=points, reason=f"{h.rule}: {h.detail}"))
    return items


def wallet_risk(subject_chain: str, subject: str, traces: list[TraceResult], typologies: list[TypologyHit], policy: ScoringPolicy, trust: SourceTrust | None) -> RiskAssessment:
    items: list[ScoreItem] = []
    for trace in traces:
        risks = {r.address: r for r in trace.risks}
        dist = _distances(trace)
        for address, hit in sorted(risks.items()):
            if address == subject:
                continue
            items += _flag_items(hit, policy, trust, policy.risk.exposure_percent(dist.get(address, 99)), f"address {dist.get(address, '?')} hop(s) away")
        items += _service_items(trace.endpoints, policy)
        if subject in risks and not any(i.component == "direct" for i in items):
            items += _flag_items(risks[subject], policy, trust, 100, "subject")
    score, marked = _combine(items, _typology_items(typologies, policy), policy)
    return RiskAssessment(chain=subject_chain, address=subject, scope="wallet", score=score, level=policy.risk.level(score), items=tuple(marked), policy_version=policy.version)


def flow_risks(trace: TraceResult, typologies: list[TypologyHit], policy: ScoringPolicy, trust: SourceTrust | None) -> list[RiskAssessment]:
    by_id = {t.id: t for t in trace.transfers}
    risks = {r.address: r for r in trace.risks}
    out = []
    for e in trace.endpoints:
        if e.kind not in (EndpointKind.VASP, EndpointKind.SERVICE, EndpointKind.DORMANT, EndpointKind.COINJOIN_LIKE):
            continue
        path_addresses = []
        for i, tid in enumerate(e.path, start=1):
            t = by_id[tid]
            path_addresses.append((i, t.receiver if trace.params.direction.value == "out" else t.sender))
        items: list[ScoreItem] = []
        if trace.subject in risks:
            items += _flag_items(risks[trace.subject], policy, trust, 100, "subject")
        for hops, address in path_addresses:
            if address in risks:
                items += _flag_items(risks[address], policy, trust, policy.risk.exposure_percent(hops), f"path address at hop {hops}")
        items += _service_items([e], policy)
        path_txs = {by_id[t].tx_hash for t in e.path}
        on_path = [h for h in typologies if path_txs & set(h.tx_hashes)]
        score, marked = _combine(items, _typology_items(on_path, policy), policy)
        out.append(
            RiskAssessment(chain=trace.chain.value, address=e.address, scope=f"flow:{e.id}", score=score, level=policy.risk.level(score), items=tuple(marked), policy_version=policy.version)
        )
    return out


def alerts_for(trace: TraceResult, subject_risk: RiskAssessment | None, freezable_assets: frozenset[str] = frozenset()) -> list[Alert]:
    """`freezable_assets`: asset keys whose issuer can freeze balances (e.g. USDT, USDC). Only
    those make an unhosted balance a freeze opportunity; anything else is "funds held"."""
    alerts: list[Alert] = []
    for hit in trace.risks:
        for flag in hit.flags:
            rule = CRIME_FAMILIES.get(flag)
            if rule is None:
                continue
            on_subject = hit.address == trace.subject
            severity = AlertSeverity.CRITICAL if on_subject and flag in (RiskFlag.SANCTIONED, RiskFlag.TERRORISM) else (
                AlertSeverity.HIGH if on_subject or flag in (RiskFlag.SANCTIONED, RiskFlag.TERRORISM, RiskFlag.RANSOMWARE, RiskFlag.DARK_WEB) else AlertSeverity.MEDIUM
            )
            where = "the subject" if on_subject else "an address on a traced path"
            alerts.append(Alert(severity=severity, rule=rule, chain=trace.chain.value, addresses=(hit.address,), message=f"{flag.replace('_', ' ')} flag on {where}: {hit.address}"))
    for bal in trace.balances:
        if bal.amount <= 0:
            continue
        if bal.asset_key in freezable_assets:
            alerts.append(
                Alert(
                    severity=AlertSeverity.HIGH,
                    rule="A-FREEZE-OPPORTUNITY",
                    chain=trace.chain.value,
                    addresses=(bal.address,),
                    message=f"{bal.formatted} still held at {bal.address} ({bal.as_of}) — the issuer can freeze it; act before it moves",
                )
            )
        else:
            alerts.append(
                Alert(
                    severity=AlertSeverity.MEDIUM,
                    rule="A-FUNDS-HELD",
                    chain=trace.chain.value,
                    addresses=(bal.address,),
                    message=f"{bal.formatted} still held at {bal.address} ({bal.as_of}) — no issuer freeze possible; watched for movement",
                )
            )
    for e in trace.endpoints:
        if e.kind is EndpointKind.SERVICE and e.attribution and e.attribution.category in (Category.MIXER, Category.BRIDGE):
            alerts.append(Alert(severity=AlertSeverity.MEDIUM, rule="A-OBFUSCATION", chain=trace.chain.value, addresses=(e.address,), message=f"value entered {e.attribution.category} {e.attribution.entity_name or e.address}"))
        if e.kind is EndpointKind.COINJOIN_LIKE:
            alerts.append(Alert(severity=AlertSeverity.MEDIUM, rule="A-OBFUSCATION", chain=trace.chain.value, addresses=(e.address,), message="value entered a CoinJoin-like transaction"))
    if subject_risk and subject_risk.level in ("severe", "high"):
        alerts.append(Alert(severity=AlertSeverity.HIGH, rule="A-HIGH-RISK-WALLET", chain=subject_risk.chain, addresses=(subject_risk.address,), message=f"subject wallet risk {subject_risk.level} ({subject_risk.score}/100)"))
    unique = {(a.rule, a.addresses, a.message): a for a in alerts}
    order = list(AlertSeverity)
    return sorted(unique.values(), key=lambda a: (order.index(a.severity), a.rule, a.addresses))

