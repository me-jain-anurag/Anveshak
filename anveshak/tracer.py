"""Time-respecting, hop-limited search for the nearest services (ADR-0006).

From the subject address the tracer follows transfers breadth-first (fewest hops first):

  * forward (Direction.OUT): only transfers that happen at or after the moment value
    arrived at the current address — value cannot leave before it arrived;
  * backward (Direction.IN): only transfers into the current address at or before the
    moment value left it.

A path ends when it reaches an address attributed to a service (VASP, mixer, bridge ...),
an unlabelled contract, a CoinJoin-like transaction, an address too busy to expand, an
address where value stops moving, or the hop limit. Every one of these is reported as an
`Endpoint` with its full path, so nothing the tracer did — or chose not to do — is hidden.

No amounts are "attributed" probabilistically. Each path reports a bottleneck: the
smallest transfer on it, an upper bound on what could have moved along that path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field

from .addresses import normalize
from .assets import AssetRegistry
from .attribution import Attributor
from .chain import Chain, ChainFamily
from .chains.base import ChainSource
from .domain import (
    AddressHistory,
    Asset,
    AssetAmount,
    Attribution,
    Direction,
    Endpoint,
    EndpointKind,
    Frozen,
    Grade,
    RiskHit,
    Transfer,
)
from .errors import SourceError

KIND_ORDER = list(EndpointKind)


class TraceParams(Frozen):
    direction: Direction = Direction.OUT
    max_hops: int = Field(default=5, ge=1, le=10)
    max_branch: int = Field(default=20, ge=1, le=200)
    max_expansions: int = Field(default=150, ge=1, le=5000)
    since: datetime | None = None
    until: datetime | None = None
    follow_all_assets: bool = False
    include_unverified_assets: bool = False
    check_balances: bool = True


class PruneRecord(Frozen):
    address: str
    candidates: int
    kept: int


class Coverage(Frozen):
    addresses_expanded: int
    incomplete_histories: tuple[str, ...]
    pruned: tuple[PruneRecord, ...]
    excluded_unverified_asset: int
    excluded_dust: int
    excluded_other_asset: int
    excluded_time_order: int  # transfers that happened before value arrived (forward) / after it left (backward)
    budget_exhausted: bool
    source_errors: tuple[str, ...]
    subject_note: str | None = None


class BalanceObservation(Frozen):
    address: str
    asset_key: str
    asset_symbol: str
    amount: int
    formatted: str
    evidence_id: str
    as_of: str


class TraceResult(Frozen):
    chain: Chain
    subject: str
    params: TraceParams
    transfers: tuple[Transfer, ...]
    endpoints: tuple[Endpoint, ...]
    attributions: tuple[Attribution, ...]
    risks: tuple[RiskHit, ...]
    balances: tuple[BalanceObservation, ...]
    coverage: Coverage


@dataclass(frozen=True)
class _Node:
    address: str
    hops: int
    via: Transfer | None  # the transfer that brought the trace here (None for the subject)
    path: tuple[str, ...]
    asset_key: str | None


class Tracer:
    def __init__(self, source: ChainSource, attributor: Attributor, registry: AssetRegistry):
        self.source = source
        self.attributor = attributor
        self.registry = registry
        self.chain = source.chain

    # ------------------------------------------------------------------ helpers

    def _is_contract(self, address: str) -> bool | None:
        if self.chain.family is not ChainFamily.EVM:
            return None
        try:
            return self.source.is_contract(address)
        except SourceError:
            return None

    def _bottleneck(self, path: tuple[str, ...], transfers: dict[str, Transfer]) -> AssetAmount | None:
        steps = [transfers[i] for i in path]
        if not steps or len({t.asset.key for t in steps}) != 1:
            return None
        smallest = min(steps, key=lambda t: t.amount)
        return AssetAmount(asset=smallest.asset, amount=smallest.amount)

    # ------------------------------------------------------------------ main loop

    def trace(self, subject: str, params: TraceParams) -> TraceResult:
        chain = self.chain
        subject = normalize(chain, subject)
        out = params.direction is Direction.OUT
        histories: dict[str, AddressHistory] = {}
        transfers: dict[str, Transfer] = {}
        endpoints: list[Endpoint] = []
        attributions: dict[str, Attribution] = {}
        risks: dict[str, RiskHit] = {}
        incomplete: list[str] = []
        pruned: list[PruneRecord] = []
        source_errors: list[str] = []
        excluded = {"unverified": 0, "dust": 0, "other_asset": 0, "time_order": 0}
        budget_exhausted = False
        subject_note: str | None = None
        best: dict[str, tuple] = {}
        expansions = 0

        def note(address: str) -> Attribution | None:
            hit = self.attributor.risk(chain, address)
            if hit:
                risks[address] = hit
            att = self.attributor.resolve(chain, address, self._is_contract if chain.family is ChainFamily.EVM else None)
            if att:
                attributions[address] = att
            return att

        def endpoint(kind: EndpointKind, node_address: str, hops: int, path: tuple[str, ...], **kw) -> None:
            endpoints.append(
                Endpoint(kind=kind, chain=chain, address=node_address, hops=hops, path=path, bottleneck=self._bottleneck(path, transfers), **kw)
            )

        note(subject)
        queue: deque[_Node] = deque([_Node(subject, 0, None, (), None)])

        while queue:
            node = queue.popleft()
            if expansions >= params.max_expansions:
                budget_exhausted = True
                endpoint(EndpointKind.NOT_EXPANDED, node.address, node.hops, node.path, notes=("search budget exhausted before this address was expanded",))
                continue
            try:
                history = self.source.history(node.address)
            except SourceError as exc:
                source_errors.append(f"{node.address}: {exc}")
                endpoint(EndpointKind.SOURCE_ERROR, node.address, node.hops, node.path, notes=(str(exc),))
                continue
            expansions += 1
            histories[node.address] = history

            if not history.complete:
                incomplete.append(f"{node.address}: {history.note}")
                if node.hops > 0:
                    endpoint(
                        EndpointKind.HIGH_ACTIVITY,
                        node.address,
                        node.hops,
                        node.path,
                        notes=(f"history exceeds the fetch cap ({history.note}) — possibly an unlabelled service; review manually",),
                    )
                    continue

            if node.hops > 0 and chain is Chain.BITCOIN:
                derived = self.attributor.derive_cospend(history)
                if derived is not None:
                    attributions[node.address] = derived
                    if derived.grade is Grade.X or derived.is_service:
                        prev = self._previous_address(node, transfers, out)
                        endpoint(
                            EndpointKind.VASP if derived.is_vasp else EndpointKind.SERVICE,
                            node.address,
                            node.hops,
                            node.path,
                            attribution=derived,
                            adjacent_address=prev,
                        )
                        continue

            candidates = self._candidates(node, history, params, out, excluded)

            coinjoin_txs = sorted({t.tx_hash for t in candidates if t.utxo is not None and t.utxo.coinjoin_like})
            if coinjoin_txs:
                candidates = [t for t in candidates if t.tx_hash not in coinjoin_txs]
                for tx in coinjoin_txs:
                    endpoint(
                        EndpointKind.COINJOIN_LIKE,
                        node.address,
                        node.hops,
                        node.path,
                        notes=(f"transaction {tx} has CoinJoin structure (≥3 distinct input addresses and ≥3 equal-value outputs); deterministic linking stops here",),
                    )

            if not candidates:
                if node.hops == 0:
                    received = self._subject_received_assets(history, params) if out else []
                    if received and not coinjoin_txs:
                        endpoint(
                            EndpointKind.DORMANT,
                            node.address,
                            0,
                            (),
                            notes=("the subject received value but made no qualifying outgoing transfer in the requested window � funds may still be held by the subject",),
                        )
                    else:
                        subject_note = "no qualifying transfers found for the subject in the requested window"
                elif not coinjoin_txs:
                    kind = EndpointKind.DORMANT if out else EndpointKind.ORIGIN
                    text = (
                        "no qualifying outgoing transfer observed after value arrived — funds may still be held here"
                        if out
                        else "no qualifying earlier incoming transfer observed — origin of the observed funds"
                    )
                    endpoint(kind, node.address, node.hops, node.path, notes=(text,))
                continue

            if len(candidates) > params.max_branch:
                ranked = sorted(candidates, key=lambda t: (-t.amount, t.order_key))
                pruned.append(PruneRecord(address=node.address, candidates=len(candidates), kept=params.max_branch))
                candidates = ranked[: params.max_branch]

            for t in sorted(candidates, key=lambda t: t.order_key):
                transfers[t.id] = t
                nxt = t.receiver if out else t.sender
                if nxt == subject:
                    continue
                path = node.path + (t.id,)
                hops = node.hops + 1
                att = note(nxt)
                if att is not None and (att.grade is Grade.X or att.is_service):
                    endpoint(
                        EndpointKind.VASP if att.is_vasp else EndpointKind.SERVICE,
                        nxt,
                        hops,
                        path,
                        attribution=att,
                        adjacent_address=node.address,
                        notes=("conflicting attribution — manual review",) if att.grade is Grade.X else (),
                    )
                    continue
                if chain.family is ChainFamily.EVM and self._is_contract(nxt) is True:
                    endpoint(
                        EndpointKind.UNLABELED_CONTRACT,
                        nxt,
                        hops,
                        path,
                        adjacent_address=node.address,
                        notes=("smart contract without a label — funds may be pooled (DEX, bridge, service); review manually",),
                    )
                    continue
                if hops >= params.max_hops:
                    endpoint(EndpointKind.HOP_LIMIT, nxt, hops, path, notes=(f"hop limit {params.max_hops} reached",))
                    continue
                key = t.order_key
                prev = best.get(nxt)
                if prev is not None and ((out and prev <= key) or (not out and prev >= key)):
                    continue  # already reached with an earlier (forward) / later (backward) transfer
                best[nxt] = key
                queue.append(_Node(nxt, hops, t, path, t.asset.key))

        endpoints = self._annotate_adjacent(endpoints, histories, transfers, out)
        balances = self._balances(endpoints, transfers, histories, params) if params.check_balances and out else []
        endpoints.sort(key=lambda e: (KIND_ORDER.index(e.kind), e.hops, e.address, e.path))

        return TraceResult(
            chain=chain,
            subject=subject,
            params=params,
            transfers=tuple(sorted(transfers.values(), key=lambda t: t.order_key)),
            endpoints=tuple(endpoints),
            attributions=tuple(attributions[a] for a in sorted(attributions)),
            risks=tuple(risks[a] for a in sorted(risks)),
            balances=tuple(balances),
            coverage=Coverage(
                addresses_expanded=expansions,
                incomplete_histories=tuple(incomplete),
                pruned=tuple(pruned),
                excluded_unverified_asset=excluded["unverified"],
                excluded_dust=excluded["dust"],
                excluded_other_asset=excluded["other_asset"],
                excluded_time_order=excluded["time_order"],
                budget_exhausted=budget_exhausted,
                source_errors=tuple(source_errors),
                subject_note=subject_note,
            ),
        )

    # ------------------------------------------------------------------ steps

    def _candidates(self, node: _Node, history: AddressHistory, params: TraceParams, out: bool, excluded: dict) -> list[Transfer]:
        result = []
        for t in history.transfers:
            if out:
                if t.sender != node.address or t.receiver == node.address:
                    continue
                if node.via is not None and not t.not_before(node.via):
                    excluded["time_order"] += 1
                    continue
            else:
                if t.receiver != node.address or t.sender == node.address:
                    continue
                if node.via is not None and not t.not_after(node.via):
                    excluded["time_order"] += 1
                    continue
            if node.via is None:
                if params.since and t.timestamp < params.since:
                    continue
                if params.until and t.timestamp > params.until:
                    continue
            if not t.asset.verified and not params.include_unverified_assets:
                excluded["unverified"] += 1
                continue
            if node.asset_key and not params.follow_all_assets and t.asset.key != node.asset_key:
                excluded["other_asset"] += 1
                continue
            if t.amount < self.registry.min_amount(t.asset):
                excluded["dust"] += 1
                continue
            result.append(t)
        return result

    @staticmethod
    def _previous_address(node: _Node, transfers: dict[str, Transfer], out: bool) -> str | None:
        if node.via is None:
            return None
        return node.via.sender if out else node.via.receiver

    def _annotate_adjacent(self, endpoints: list[Endpoint], histories: dict[str, AddressHistory], transfers: dict[str, Transfer], out: bool) -> list[Endpoint]:
        """R-SWEEP (account chains, forward traces): the address that paid a VASP is marked a
        deposit-address candidate if *every* qualifying transfer it made after value arrived
        went to addresses attributed to that same VASP entity."""
        if not out or self.chain.family is ChainFamily.UTXO:
            return endpoints
        annotated = []
        for e in endpoints:
            if e.kind is not EndpointKind.VASP or e.attribution is None or e.attribution.entity_id is None or len(e.path) < 2:
                annotated.append(e)
                continue
            adjacent = e.adjacent_address
            arrival = transfers[e.path[-2]]
            history = histories.get(adjacent or "")
            role = None
            if history is not None and history.complete:
                later = [
                    t
                    for t in history.transfers
                    if t.sender == adjacent
                    and t.not_before(arrival)
                    and t.asset.verified
                    and t.amount >= self.registry.min_amount(t.asset)
                ]
                owners = {(self.attributor.ownership(self.chain, t.receiver) or _NONE).entity_id for t in later}
                if later and owners == {e.attribution.entity_id}:
                    role = (
                        f"deposit-address pattern (R-SWEEP): all {len(later)} qualifying transfer(s) from this address "
                        f"after value arrived went to addresses attributed to {e.attribution.entity_name}"
                    )
            annotated.append(e.model_copy(update={"adjacent_role": role}))
        return annotated

    def _subject_received_assets(self, history: AddressHistory, params: TraceParams) -> list[Asset]:
        """Verified assets the subject received (in the requested window, above dust)."""
        assets = {}
        for t in history.transfers:
            if t.receiver != history.address or not t.asset.verified or t.amount < self.registry.min_amount(t.asset):
                continue
            if (params.since and t.timestamp < params.since) or (params.until and t.timestamp > params.until):
                continue
            assets[t.asset.key] = t.asset
        return [assets[k] for k in sorted(assets)]

    def _balances(self, endpoints: list[Endpoint], transfers: dict[str, Transfer], histories: dict[str, AddressHistory], params: TraceParams) -> list[BalanceObservation]:
        observations = []
        seen = set()
        for e in endpoints:
            if e.kind is not EndpointKind.DORMANT:
                continue
            if e.path:
                assets = [transfers[e.path[-1]].asset]
            else:  # the subject itself
                assets = self._subject_received_assets(histories[e.address], params)
            for asset in assets:
                self._observe_balance(e.address, asset, seen, observations)
        return sorted(observations, key=lambda b: (b.address, b.asset_key))

    def _observe_balance(self, address: str, asset: Asset, seen: set, observations: list[BalanceObservation]) -> None:
        key = (address, asset.key)
        if key in seen or not asset.verified:
            return
        seen.add(key)
        try:
            bal = self.source.balance(address, asset)
        except SourceError:
            return  # a missing balance is simply not reported; it never blocks the trace
        if bal is None:
            return
        observations.append(
            BalanceObservation(
                address=address,
                asset_key=asset.key,
                asset_symbol=asset.symbol,
                amount=bal.amount,
                formatted=asset.format(bal.amount),
                evidence_id=bal.evidence_id,
                as_of=bal.as_of,
            )
        )


class _NoAttribution:
    entity_id = None


_NONE = _NoAttribution()
