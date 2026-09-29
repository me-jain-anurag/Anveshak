"""Laundering-typology detection over a trace's transfer graph (ADR-0013).

Each detector is a fixed rule with thresholds from data/scoring_policy.yaml. A hit lists
the exact addresses and transactions that satisfy the rule. The detectors only see the
transfers the tracer followed, so a hit is conclusive for the pattern while a miss only
means "not observed in the traced subgraph".

  T-PEEL      peel chain: ≥ min_steps consecutive transfers on one path, same asset, each
              smaller than the one before but keeping ≥ min_keep_percent of it
  T-PASS      rapid pass-through: an address forwards ≥ min_forward_percent of a received
              amount (same asset) within max_minutes
  T-FANOUT    fan-out / structuring: one address pays ≥ min_recipients distinct addresses
              within window_hours
  T-FANIN     fan-in / aggregation: one address is paid by ≥ min_senders distinct addresses
              within window_hours
  T-MIXER     value entered a mixer; T-COINJOIN: value entered a CoinJoin-like transaction
  T-CHAINHOP  value entered a bridge / cross-chain swap service, or a cross-chain link was resolved

References: FATF (2020) "Virtual Assets — Red Flag Indicators of Money Laundering and
Terrorist Financing" (transaction-pattern and anonymity indicators); Kappos et al. (2022)
"How to Peel a Million" (peel chains).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from enum import StrEnum

from .domain import Category, EndpointKind, Frozen, Transfer
from .policy import ScoringPolicy
from .tracer import TraceResult

FATF_RED_FLAGS = "FATF (2020) Virtual Assets Red Flag Indicators of ML/TF"


class Typology(StrEnum):
    PEEL_CHAIN = "peel_chain"
    RAPID_PASS_THROUGH = "rapid_pass_through"
    FAN_OUT = "fan_out"
    FAN_IN = "fan_in"
    MIXER = "mixer"
    COINJOIN = "coinjoin"
    CHAIN_HOPPING = "chain_hopping"


class TypologyHit(Frozen):
    typology: Typology
    rule: str
    addresses: tuple[str, ...]
    tx_hashes: tuple[str, ...]
    detail: str
    reference: str = FATF_RED_FLAGS


def _peel(trace: TraceResult, by_id: dict[str, Transfer], policy: ScoringPolicy) -> list[TypologyHit]:
    cfg = policy.typologies.peel_chain
    hits: dict[tuple[str, ...], TypologyHit] = {}
    for e in trace.endpoints:
        steps = [by_id[t] for t in e.path]
        run: list[Transfer] = steps[:1]
        for prev, cur in zip(steps, steps[1:]):
            keeps = cur.asset.key == prev.asset.key and cur.amount < prev.amount and cur.amount * 100 >= prev.amount * cfg.min_keep_percent
            if keeps:
                run.append(cur)
            else:
                if len(run) >= cfg.min_steps:
                    _add_peel(hits, run, cfg.min_keep_percent)
                run = [cur]
        if len(run) >= cfg.min_steps:
            _add_peel(hits, run, cfg.min_keep_percent)
    return list(hits.values())


def _add_peel(hits: dict, run: list[Transfer], keep: int) -> None:
    key = tuple(t.id for t in run)
    if key in hits:
        return
    amounts = " → ".join(t.formatted_amount for t in run)
    hits[key] = TypologyHit(
        typology=Typology.PEEL_CHAIN,
        rule="T-PEEL",
        addresses=tuple(dict.fromkeys([run[0].sender] + [t.receiver for t in run])),
        tx_hashes=tuple(t.tx_hash for t in run),
        detail=f"{len(run)} consecutive transfers, each smaller than the previous but keeping ≥{keep}% of it: {amounts}",
        reference="Kappos et al. (2022) How to Peel a Million; " + FATF_RED_FLAGS,
    )


def _pass_through(trace: TraceResult, policy: ScoringPolicy) -> list[TypologyHit]:
    cfg = policy.typologies.rapid_pass_through
    window = timedelta(minutes=cfg.max_minutes)
    out_by: dict[str, list[Transfer]] = defaultdict(list)
    in_by: dict[str, list[Transfer]] = defaultdict(list)
    for t in trace.transfers:
        out_by[t.sender].append(t)
        in_by[t.receiver].append(t)
    hits = []
    for address in sorted(in_by):
        for t_in in sorted(in_by[address], key=lambda t: t.order_key):
            forwarded = [
                t for t in out_by.get(address, [])
                if t.asset.key == t_in.asset.key and t_in.timestamp <= t.timestamp <= t_in.timestamp + window and t.not_before(t_in)
            ]
            total = sum(t.amount for t in forwarded)
            if forwarded and total * 100 >= t_in.amount * cfg.min_forward_percent:
                minutes = int((max(t.timestamp for t in forwarded) - t_in.timestamp).total_seconds() // 60)
                hits.append(
                    TypologyHit(
                        typology=Typology.RAPID_PASS_THROUGH,
                        rule="T-PASS",
                        addresses=(address,),
                        tx_hashes=(t_in.tx_hash, *sorted({t.tx_hash for t in forwarded})),
                        detail=f"received {t_in.formatted_amount} and forwarded {t_in.asset.format(total)} within {minutes} min",
                    )
                )
                break  # one hit per address is enough
    return hits


def _fan(trace: TraceResult, policy: ScoringPolicy, outgoing: bool) -> list[TypologyHit]:
    cfg = policy.typologies.fan_out if outgoing else policy.typologies.fan_in
    minimum = cfg.min_recipients if outgoing else cfg.min_senders
    window = timedelta(hours=cfg.window_hours)
    groups: dict[str, list[Transfer]] = defaultdict(list)
    for t in trace.transfers:
        groups[t.sender if outgoing else t.receiver].append(t)
    hits = []
    for address in sorted(groups):
        ts = sorted(groups[address], key=lambda t: t.order_key)
        for i, first in enumerate(ts):
            inside = [t for t in ts[i:] if t.timestamp - first.timestamp <= window]
            parties = {t.receiver if outgoing else t.sender for t in inside}
            if len(parties) >= minimum:
                hits.append(
                    TypologyHit(
                        typology=Typology.FAN_OUT if outgoing else Typology.FAN_IN,
                        rule="T-FANOUT" if outgoing else "T-FANIN",
                        addresses=(address,),
                        tx_hashes=tuple(sorted({t.tx_hash for t in inside})),
                        detail=f"{'paid' if outgoing else 'was paid by'} {len(parties)} distinct addresses within {cfg.window_hours} h",
                    )
                )
                break
    return hits


def _service_typologies(trace: TraceResult, by_id: dict[str, Transfer]) -> list[TypologyHit]:
    hits = []
    for e in trace.endpoints:
        last = by_id[e.path[-1]].tx_hash if e.path else None
        txs = (last,) if last else ()
        if e.kind is EndpointKind.SERVICE and e.attribution and e.attribution.category is Category.MIXER:
            hits.append(TypologyHit(typology=Typology.MIXER, rule="T-MIXER", addresses=(e.address,), tx_hashes=txs, detail=f"value entered mixer {e.attribution.entity_name}"))
        elif e.kind is EndpointKind.SERVICE and e.attribution and e.attribution.category is Category.BRIDGE:
            hits.append(TypologyHit(typology=Typology.CHAIN_HOPPING, rule="T-CHAINHOP", addresses=(e.address,), tx_hashes=txs, detail=f"value entered bridge / cross-chain service {e.attribution.entity_name}"))
        elif e.kind is EndpointKind.COINJOIN_LIKE:
            hits.append(TypologyHit(typology=Typology.COINJOIN, rule="T-COINJOIN", addresses=(e.address,), tx_hashes=txs, detail="value entered a CoinJoin-like transaction"))
    return hits


def detect(trace: TraceResult, policy: ScoringPolicy) -> list[TypologyHit]:
    by_id = {t.id: t for t in trace.transfers}
    hits = _peel(trace, by_id, policy) + _pass_through(trace, policy) + _fan(trace, policy, True) + _fan(trace, policy, False) + _service_typologies(trace, by_id)
    unique = {(h.rule, h.addresses, h.tx_hashes): h for h in hits}
    return [unique[k] for k in sorted(unique)]
