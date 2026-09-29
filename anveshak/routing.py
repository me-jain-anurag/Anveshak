"""Turn findings into disclosure / freeze request drafts for the correct VASP or issuer (ADR-0010).

A decision is a *draft for a human*. The engine never sends anything itself: the best status
it can reach is READY_FOR_APPROVAL, and only an officer's explicit approval hands a draft to
the Sahyog gateway.

Per VASP reached on a traced path, two drafts are produced:
  DISCLOSURE  identity (KYC) and records of the account(s) credited/debited
  FREEZE      freeze / debit hold on those account(s)
Per stablecoin issuer whose token is still held at a traced address:
  ISSUER_FREEZE  token-level freeze review (e.g. Tether / T3 FCU for USDT)

Status rules, evaluated in order:
  BLOCKED            owners conflict (grade X), or every path failed verification (MISMATCH)
  ANALYST_REVIEW     no fully verified path to a grade A/B attribution; confidence below the
                     policy threshold; entity not in the directory; no verified contact channel;
                     all issuer freezes
  READY_FOR_APPROVAL everything above satisfied
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from enum import StrEnum

from .assets import AssetRegistry
from .chain import Chain
from .chains.base import Verification, VerificationStatus
from .directory import Channel, VaspDirectory
from .domain import Direction, Endpoint, EndpointKind, Frozen, Grade
from .policy import ScoringPolicy
from .scoring import ConfidenceScore
from .tracer import TraceResult

LEGAL_BASIS_DEFAULT = (
    "[Legal provision to be entered by the investigating officer. Sahyog requests are reported to be "
    "made under Section 94 BNSS and Section 79(3)(b) of the IT Act — verify with the legal cell.]"
)


class RouteStatus(StrEnum):
    READY_FOR_APPROVAL = "ready_for_approval"
    ANALYST_REVIEW = "analyst_review"
    BLOCKED = "blocked"


class RequestType(StrEnum):
    DISCLOSURE = "disclosure"  # KYC / records of the credited or debited account(s)
    FREEZE = "freeze"  # freeze / debit hold on those account(s) at the VASP
    ISSUER_FREEZE = "issuer_freeze"  # token-level freeze by the stablecoin issuer


class TxRef(Frozen):
    tx_hash: str
    timestamp: datetime
    amount: str
    from_address: str
    to_address: str
    explorer_url: str


class RoutingDecision(Frozen):
    id: str
    chain: Chain
    direction: Direction
    subject: str
    target_entity_id: str | None
    target_name: str
    target_role: str
    request_type: RequestType
    status: RouteStatus
    reasons: tuple[str, ...]
    grade: Grade | None
    confidence: int | None
    confidence_band: str | None
    attribution_rules: tuple[str, ...]
    addresses: tuple[str, ...]
    transactions: tuple[TxRef, ...]
    endpoint_ids: tuple[str, ...]
    channels: tuple[Channel, ...]
    jurisdiction: str | None
    via: str | None = None  # how value reached this trace's subject, for cross-chain continuations
    draft_text: str


def _path_status(endpoint: Endpoint, verifications: dict[str, Verification]) -> VerificationStatus:
    statuses = [verifications[t].status if t in verifications else VerificationStatus.UNVERIFIABLE for t in endpoint.path]
    for bad in (VerificationStatus.MISMATCH, VerificationStatus.ERROR, VerificationStatus.UNVERIFIABLE):
        if bad in statuses:
            return bad
    return VerificationStatus.VERIFIED


def _decision_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _draft(case_ref: str, d: dict) -> str:
    channel = d["channels"][0] if d["channels"] else None
    lines = [
        f"Case reference: {case_ref}",
        f"To: {d['target_name']}" + (f" — via {channel.name} ({channel.url})" if channel else " — contact channel not yet verified"),
        f"Request: {d['request_type'].value.replace('_', ' ')}",
        f"Legal basis: {LEGAL_BASIS_DEFAULT}",
        "",
    ]
    if d.get("via"):
        lines += [d["via"], ""]
    if d["request_type"] in (RequestType.DISCLOSURE, RequestType.FREEZE):
        direction_text = (
            "received value that can be traced from the subject address"
            if d["direction"] is Direction.OUT
            else "sent value that can be traced to the subject address"
        )
        lines += [f"The following {d['chain']} transactions show that addresses attributed to {d['target_name']} {direction_text} {d['subject']}:", ""]
        for tx in d["transactions"]:
            lines.append(f"  - {tx.timestamp:%Y-%m-%d %H:%M:%S} UTC  {tx.amount}  {tx.from_address} -> {tx.to_address}  tx {tx.tx_hash}")
        if d["deposit_addresses"]:
            lines += ["", "Deposit address(es) identified by on-chain pattern: " + ", ".join(d["deposit_addresses"])]
        if d["request_type"] is RequestType.DISCLOSURE:
            lines += [
                "",
                "Requested: identity (KYC) and contact details of the account holder(s) credited or debited by the "
                "transactions above, the account's transaction history, linked bank accounts and payment instruments, "
                "login IP addresses and devices, and preservation of all related records.",
            ]
        else:
            lines += [
                "",
                "Requested: immediate freeze / debit hold on the account(s) credited by the transactions above and on "
                "any balance derived from them, confirmation of the frozen amount, and preservation of all related records.",
            ]
    else:
        lines += [f"Funds traced from {d['subject']} on {d['chain']} are still held at:", ""]
        lines += [f"  - {h}" for h in d["holdings"]]
        lines += ["", "Requested: review of a token-level freeze of the balance(s) above."]
    score = f"{d['confidence']}/100 ({d['band']})" if d["confidence"] is not None else "n/a"
    lines += [
        "",
        f"Attribution basis: grade {d['grade'] or 'n/a'} ({', '.join(d['rules']) or 'n/a'}); confidence score {score}. "
        "Grades and scores are evidence measures defined in the attached report, not probabilities.",
        "Every transaction listed can be independently checked on the public ledger using the hashes above.",
    ]
    return "\n".join(lines)


def route(
    case_ref: str,
    traces: list[TraceResult],
    verifications: dict[str, Verification],
    directory: VaspDirectory,
    registry: AssetRegistry,
    scores: dict[str, ConfidenceScore],
    policy: ScoringPolicy,
    via: dict[int, str] | None = None,
) -> list[RoutingDecision]:
    """`via` maps a trace index to a sentence explaining how value reached that trace's
    subject (set for cross-chain continuation traces)."""
    decisions: list[RoutingDecision] = []
    for index, trace in enumerate(traces):
        note = (via or {}).get(index)
        by_id = {t.id: t for t in trace.transfers}
        groups: dict[str, list[Endpoint]] = defaultdict(list)
        for e in trace.endpoints:
            if e.kind is not EndpointKind.VASP or e.attribution is None:
                continue
            a = e.attribution
            key = directory.canonical(a.entity_id) if a.grade is not Grade.X and a.entity_id else f"conflict:{e.address}"
            groups[key].append(e)

        for key, endpoints in sorted(groups.items()):
            decisions += _vasp_decisions(case_ref, trace, key, endpoints, by_id, verifications, directory, scores, policy, note)
        decisions += _issuer_decisions(case_ref, trace, directory, registry, note)
    return decisions


def _vasp_decisions(case_ref, trace, key, endpoints, by_id, verifications, directory, scores, policy, via=None) -> list[RoutingDecision]:
    path_state = {e.id: _path_status(e, verifications) for e in endpoints}
    conflicted = key.startswith("conflict:")
    entry = None if conflicted else directory.get(key)
    first_att = endpoints[0].attribution
    strong = [
        e for e in endpoints
        if path_state[e.id] is VerificationStatus.VERIFIED and e.attribution.grade in (Grade.A, Grade.B)
    ]
    chosen = strong or endpoints
    best_score = max((scores[e.id] for e in chosen if e.id in scores), key=lambda s: s.score, default=None)
    confidence = best_score.score if best_score else None
    band = best_score.band if best_score else None

    reasons: list[str] = []
    if conflicted:
        status = RouteStatus.BLOCKED
        reasons.append(f"conflicting attribution for {endpoints[0].address}: {', '.join(first_att.conflicts)}")
    elif all(s is VerificationStatus.MISMATCH for s in path_state.values()):
        status = RouteStatus.BLOCKED
        reasons.append("every path failed independent verification (MISMATCH) — the listed facts are not trusted")
    elif not strong:
        status = RouteStatus.ANALYST_REVIEW
        grades = sorted({e.attribution.grade.value for e in endpoints})
        states = sorted({s.value for s in path_state.values()})
        reasons.append(f"no fully verified path to a grade A/B attribution (grades: {', '.join(grades)}; path checks: {', '.join(states)})")
    elif confidence is not None and confidence < policy.routing.ready_min_confidence:
        status = RouteStatus.ANALYST_REVIEW
        reasons.append(f"confidence {confidence} is below the policy threshold {policy.routing.ready_min_confidence}")
    elif entry is None:
        status = RouteStatus.ANALYST_REVIEW
        reasons.append(f"entity '{key}' is not in the VASP directory — add it with a verified contact channel")
    elif not entry.channels:
        status = RouteStatus.ANALYST_REVIEW
        reasons.append(f"no verified contact channel recorded for {entry.display_name}")
    else:
        status = RouteStatus.READY_FOR_APPROVAL
        reasons.append(f"{len(strong)} fully verified path(s) to grade A/B address(es), confidence {confidence}; awaiting officer approval")

    grade = max((e.attribution.grade for e in chosen), key=lambda g: g.rank)
    rules = tuple(sorted({e.attribution.rule for e in chosen}))
    last_hops = [by_id[e.path[-1]] for e in chosen if e.path]
    tx_refs = tuple(
        TxRef(tx_hash=t.tx_hash, timestamp=t.timestamp, amount=t.formatted_amount, from_address=t.sender, to_address=t.receiver, explorer_url=t.chain.tx_url(t.tx_hash))
        for t in sorted({t.id: t for t in last_hops}.values(), key=lambda t: t.order_key)
    )
    deposit = sorted({e.adjacent_address for e in chosen if e.adjacent_role and e.adjacent_address})
    addresses = tuple(sorted({e.address for e in chosen} | set(deposit)))
    if conflicted:
        target_name = f"Unresolved owner ({' vs '.join(first_att.conflicts)})"
    else:
        target_name = entry.display_name if entry else (first_att.entity_name or key)
    jurisdiction = str(entry.jurisdiction.value) if entry and entry.jurisdiction.value else None

    out = []
    for request_type in (RequestType.DISCLOSURE, RequestType.FREEZE):
        r_status, r_reasons = status, list(reasons)
        if request_type is RequestType.FREEZE and status is RouteStatus.READY_FOR_APPROVAL and (confidence or 0) < policy.routing.freeze_min_confidence:
            r_status = RouteStatus.ANALYST_REVIEW
            r_reasons = [f"freeze requires confidence ≥ {policy.routing.freeze_min_confidence}; this is {confidence}"]
        fields = dict(
            chain=trace.chain, direction=trace.params.direction, subject=trace.subject, target_name=target_name, request_type=request_type,
            transactions=tx_refs, deposit_addresses=deposit, grade=grade.value, rules=rules, channels=entry.channels if entry else (),
            confidence=confidence, band=band, holdings=(), via=via,
        )
        out.append(
            RoutingDecision(
                id=_decision_id(trace.chain, trace.subject, trace.params.direction, key, request_type),
                chain=trace.chain,
                direction=trace.params.direction,
                subject=trace.subject,
                target_entity_id=None if conflicted else key,
                target_name=target_name,
                target_role=entry.role if entry else "vasp",
                request_type=request_type,
                status=r_status,
                reasons=tuple(r_reasons),
                grade=grade,
                confidence=confidence,
                confidence_band=band,
                attribution_rules=rules,
                addresses=addresses,
                transactions=tx_refs,
                endpoint_ids=tuple(e.id for e in endpoints),
                channels=entry.channels if entry else (),
                jurisdiction=jurisdiction,
                via=via,
                draft_text=_draft(case_ref, fields),
            )
        )
    return out


def _issuer_decisions(case_ref, trace, directory, registry, via=None) -> list[RoutingDecision]:
    issuer_hits: dict[str, list] = defaultdict(list)
    for bal in trace.balances:
        if bal.amount <= 0:
            continue
        token = next((ti for ti in registry.tokens() if ti.asset.key == bal.asset_key), None)
        if token is not None and token.issuer is not None:
            issuer_hits[token.issuer].append(bal)
    out = []
    for issuer, balances in sorted(issuer_hits.items()):
        entry = directory.get(issuer)
        name = entry.display_name if entry else issuer
        addresses = tuple(sorted({b.address for b in balances}))
        holdings = tuple(f"{b.address}: {b.formatted} ({b.as_of})" for b in balances)
        fields = dict(
            chain=trace.chain, direction=trace.params.direction, subject=trace.subject, target_name=name, request_type=RequestType.ISSUER_FREEZE,
            transactions=(), deposit_addresses=[], grade=None, rules=(), channels=entry.channels if entry else (), confidence=None, band=None, holdings=holdings, via=via,
        )
        out.append(
            RoutingDecision(
                id=_decision_id(trace.chain, trace.subject, "issuer", issuer),
                chain=trace.chain,
                direction=trace.params.direction,
                subject=trace.subject,
                target_entity_id=issuer,
                target_name=name,
                target_role="stablecoin_issuer",
                request_type=RequestType.ISSUER_FREEZE,
                status=RouteStatus.ANALYST_REVIEW,
                reasons=(f"token balance observed at traced address(es): {'; '.join(holdings)}", "issuer freezes always require analyst and officer judgement"),
                grade=None,
                confidence=None,
                confidence_band=None,
                attribution_rules=(),
                addresses=addresses,
                transactions=(),
                endpoint_ids=tuple(e.id for e in trace.endpoints if e.kind is EndpointKind.DORMANT and e.address in addresses),
                channels=entry.channels if entry else (),
                jurisdiction=None,
                via=via,
                draft_text=_draft(case_ref, fields),
            )
        )
    return out
