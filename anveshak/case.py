"""Case orchestration: request in, reproducible findings out.

`CaseFindings` is everything the conclusions depend on — the request, the exact label and
directory snapshots, the traces, the verifications and the routing drafts — and nothing
that varies between runs (no wall-clock times, no random ids). Its sha256 is the
`findings_hash`. Replaying a case from the evidence store with the same label and
directory snapshots must reproduce that hash exactly (ADR-0004).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator

from . import __version__
from .addresses import normalize
from .assets import AssetRegistry
from .attribution import Attributor
from .chain import Chain, ChainFamily
from .chains.base import CachingSource, ChainSource, Verification
from .chains.bitcoin import EsploraSource
from .chains.evm import EtherscanSource
from .chains.solana import SolanaRpcSource
from .chains.tron import TronGridSource
from .analysis import TraceAnalysis, analyze
from .config import DATA_DIR, Settings
from .crosschain import CrossChainLink, ThorchainResolver
from .directory import VaspDirectory
from .domain import Direction, EndpointKind, Frozen
from .errors import ConfigError, SourceError
from .evidence import EvidenceStore, Fetcher, LiveFetcher, ReplayFetcher, canonical_json, sha256_hex
from .labels.store import LabelStore
from .policy import ScoringPolicy
from .risk import RiskAssessment
from .routing import RoutingDecision, route
from .sourcetrust import SourceTrust
from .tracer import TraceParams, TraceResult, Tracer
from .verify import verify_paths


class DataMode(StrEnum):
    LIVE = "live"  # public APIs, every response recorded as evidence
    REPLAY = "replay"  # evidence store only, no network
    SYNTHETIC = "synthetic"  # built-in fictional demo data — NOT EVIDENCE


class Subject(Frozen):
    chain: Chain
    address: str

    @field_validator("address")
    @classmethod
    def _canonical(cls, v: str, info: ValidationInfo) -> str:
        chain = info.data.get("chain")
        if chain is None:
            raise ValueError("a valid chain is required before the address can be checked")
        return normalize(chain, v)  # AddressError is a ValueError → reported as a validation error


class CaseRequest(Frozen):
    case_reference: str = Field(min_length=1, max_length=120)
    subjects: tuple[Subject, ...] = Field(min_length=1, max_length=20)
    directions: tuple[Direction, ...] = (Direction.OUT, Direction.IN)
    max_hops: int = Field(default=5, ge=1, le=10)
    max_branch: int = Field(default=20, ge=1, le=200)
    max_expansions: int = Field(default=150, ge=1, le=5000)
    since: datetime | None = None
    until: datetime | None = None
    follow_all_assets: bool = False
    include_unverified_assets: bool = False
    follow_cross_chain: bool = True
    cross_chain_depth: int = Field(default=1, ge=0, le=3)
    requested_by: str | None = Field(default=None, max_length=200)

    @field_validator("directions")
    @classmethod
    def _unique(cls, v: tuple[Direction, ...]) -> tuple[Direction, ...]:
        if not v:
            raise ValueError("at least one direction is required")
        return tuple(sorted(set(v), key=lambda d: d.value, reverse=True))

    def params(self, direction: Direction) -> TraceParams:
        return TraceParams(
            direction=direction,
            max_hops=self.max_hops,
            max_branch=self.max_branch,
            max_expansions=self.max_expansions,
            since=self.since,
            until=self.until,
            follow_all_assets=self.follow_all_assets,
            include_unverified_assets=self.include_unverified_assets,
        )


class Continuation(Frozen):
    trace_index: int
    link_id: str
    parent_trace_index: int


class CaseFindings(Frozen):
    engine_version: str
    data_mode: DataMode
    request: CaseRequest
    label_snapshot: str
    directory_snapshot: str
    policy_snapshot: str
    policy_version: str
    traces: tuple[TraceResult, ...]
    verifications: tuple[Verification, ...]
    analyses: tuple[TraceAnalysis, ...]  # aligned with `traces`
    subject_risks: tuple[RiskAssessment, ...]
    crosschain_links: tuple[CrossChainLink, ...]
    continuations: tuple[Continuation, ...]  # traces started on another chain from a link
    crosschain_errors: tuple[str, ...]
    routing: tuple[RoutingDecision, ...]
    evidence_ids: tuple[str, ...]  # evidence the findings rest on (transfers, verifications, balances)


class CaseResult(Frozen):
    case_id: str
    created_at: datetime
    findings: CaseFindings
    findings_hash: str
    fetched_evidence_count: int  # everything fetched, including pages that yielded no findings


def findings_hash(findings: CaseFindings) -> str:
    return sha256_hex(canonical_json(findings.model_dump(mode="json")))


CROSSCHAIN_ENDPOINTS = frozenset({EndpointKind.SERVICE, EndpointKind.UNLABELED_CONTRACT, EndpointKind.HIGH_ACTIVITY})


class Engine:
    def __init__(
        self,
        mode: DataMode,
        labels: LabelStore,
        registry: AssetRegistry,
        directory: VaspDirectory,
        settings: Settings | None = None,
        sources: dict[Chain, ChainSource] | None = None,
        fetcher: Fetcher | None = None,
        policy: ScoringPolicy | None = None,
        trust: SourceTrust | None = None,
        providers: list | None = None,
        resolvers: list | None = None,
    ):
        self.mode = mode
        self.labels = labels
        self.registry = registry
        self.directory = directory
        self.policy = policy or ScoringPolicy.load(DATA_DIR / "scoring_policy.yaml")
        self.trust = trust or SourceTrust.load(DATA_DIR / "authorities.yaml", directory.official_sources())
        self.providers = providers or []
        self.settings = settings
        self._sources: dict[Chain, ChainSource] = dict(sources or {})
        self.fetcher = fetcher
        if mode is DataMode.SYNTHETIC and not sources:
            raise ValueError("synthetic mode needs in-memory sources")
        if mode is not DataMode.SYNTHETIC and fetcher is None:
            if settings is None:
                raise ValueError("live/replay mode needs settings or a fetcher")
            store = EvidenceStore(settings.evidence_dir)
            self.fetcher = ReplayFetcher(store) if mode is DataMode.REPLAY else LiveFetcher(
                store,
                min_interval={
                    "api.etherscan.io": 0.25,
                    "api.trongrid.io": 0.2 if settings.trongrid_api_key else 0.6,
                    "blockstream.info": 0.25,
                    "api.mainnet-beta.solana.com": 0.35,
                    "gateway.liquify.com": 0.5,
                },
            )
        if resolvers is not None:
            self.resolvers = resolvers
        elif mode is DataMode.SYNTHETIC:
            self.resolvers = []
        else:
            self.resolvers = [ThorchainResolver(self.fetcher)]

    def source(self, chain: Chain) -> ChainSource:
        if chain in self._sources:
            src = self._sources[chain]
            if not isinstance(src, CachingSource):
                src = self._sources[chain] = CachingSource(src)
            return src
        if self.mode is DataMode.SYNTHETIC:
            raise ConfigError(f"no synthetic data for {chain}")
        s = self.settings
        assert s is not None and self.fetcher is not None
        if chain.family is ChainFamily.EVM:
            src: ChainSource = EtherscanSource(
                chain, self.fetcher, self.registry, api_key=s.etherscan_api_key, base_url=s.etherscan_base_url,
                require_key=self.mode is DataMode.LIVE,
            )
        elif chain is Chain.TRON:
            src = TronGridSource(self.fetcher, self.registry, api_key=s.trongrid_api_key, base_url=s.trongrid_base_url)
        elif chain is Chain.SOLANA:
            src = SolanaRpcSource(self.fetcher, self.registry, rpc_url=s.solana_rpc_url, max_signatures=s.solana_max_signatures)
        else:
            src = EsploraSource(self.fetcher, self.registry, base_url=s.esplora_base_url)
        self._sources[chain] = CachingSource(src)
        return self._sources[chain]

    def _cross_chain(self, result: TraceResult, errors: list[str]) -> list[CrossChainLink]:
        """Ask each resolver whether the last transfer of a candidate path was a cross-chain swap."""
        if not self.resolvers:
            return []
        by_id = {t.id: t for t in result.transfers}
        found: list[CrossChainLink] = []
        seen: set[str] = set()
        for e in result.endpoints:
            if e.kind not in CROSSCHAIN_ENDPOINTS or not e.path:
                continue
            last = by_id[e.path[-1]]
            if last.tx_hash in seen:
                continue
            seen.add(last.tx_hash)
            for resolver in self.resolvers:
                try:
                    links = resolver.resolve(result.chain, last.tx_hash, last.sender, e.id)
                except SourceError as exc:
                    errors.append(f"{resolver.name} lookup for {last.tx_hash}: {exc}")
                    continue
                found.extend(self._confirm_destination(link) for link in links)
        return found

    def _confirm_destination(self, link: CrossChainLink) -> CrossChainLink:
        """Check that the protocol's outbound transaction really pays `to_address` on `to_chain`."""
        if link.to_chain is None or not link.to_tx:
            return link
        try:
            history = self.source(link.to_chain).history(link.to_address)
        except (ConfigError, SourceError) as exc:
            return link.model_copy(update={"destination_confirmed": None, "destination_detail": f"could not check: {exc}"})
        paid = [t for t in history.transfers if t.tx_hash.lower() == link.to_tx.lower() and t.receiver == link.to_address]
        if paid:
            return link.model_copy(update={"destination_confirmed": True, "destination_detail": f"{paid[0].formatted_amount} received in {paid[0].tx_hash}"})
        detail = "outbound transaction not found in the destination address history"
        if not history.complete:
            return link.model_copy(update={"destination_confirmed": None, "destination_detail": detail + " (history incomplete)"})
        return link.model_copy(update={"destination_confirmed": False, "destination_detail": detail})

    def run(self, request: CaseRequest, case_id: str | None = None) -> CaseResult:
        attributor = Attributor(self.labels, aliases=self.directory.aliases, trust=self.trust, providers=self.providers)
        traces: list[TraceResult] = []
        verifications: dict[str, Verification] = {}
        links: list[CrossChainLink] = []
        continuations: list[Continuation] = []
        crosschain_errors: list[str] = []
        # work items: (chain, subject, params, depth, parent trace index, link id)
        work: list[tuple[Chain, str, TraceParams, int, int | None, str | None]] = [
            (subject.chain, subject.address, request.params(direction), 0, None, None)
            for subject in request.subjects
            for direction in request.directions
        ]
        while work:
            chain, subject, params, depth, parent, link_id = work.pop(0)
            try:
                source = self.source(chain)
            except ConfigError as exc:
                crosschain_errors.append(f"cannot trace {chain} {subject}: {exc}")
                continue
            result = Tracer(source, attributor, self.registry).trace(subject, params)
            index = len(traces)
            traces.append(result)
            if parent is not None and link_id is not None:
                continuations.append(Continuation(trace_index=index, link_id=link_id, parent_trace_index=parent))
            verifications.update(verify_paths(result, source, already=verifications))
            if request.follow_cross_chain and params.direction is Direction.OUT:
                for link in self._cross_chain(result, crosschain_errors):
                    links.append(link)
                    if link.to_chain is not None and link.to_tx and depth < request.cross_chain_depth and link.destination_confirmed is not False:
                        origin = next(t for t in result.transfers if t.tx_hash == link.from_tx)
                        hops_used = next(e.hops for e in result.endpoints if e.id == link.endpoint_id)
                        cont = params.model_copy(update={"since": origin.timestamp, "until": None, "max_hops": max(1, params.max_hops - hops_used)})
                        work.append((link.to_chain, link.to_address, cont, depth + 1, index, link.id))

        analyses, subject_risks = analyze(traces, verifications, self.policy, self.trust, links)
        scores = {s.endpoint_id: s for a in analyses for s in a.confidences}
        by_link = {link.id: link for link in links}
        via = {}
        for c in continuations:
            link, parent = by_link[c.link_id], traces[c.parent_trace_index]
            via[c.trace_index] = (
                f"Value reached {link.to_address} on {link.to_chain_code} through a {link.protocol} cross-chain swap "
                f"({link.asset_in} -> {link.asset_out}; inbound tx {link.from_tx} on {link.from_chain.value}), "
                f"traced from the case subject {parent.subject} on {parent.chain.value} (rule {link.rule})."
            )
        decisions = route(request.case_reference, traces, verifications, self.directory, self.registry, scores, self.policy, via)

        evidence = set()
        for trace in traces:
            evidence.update(t.evidence_id for t in trace.transfers)
            evidence.update(b.evidence_id for b in trace.balances)
        for v in verifications.values():
            evidence.update(v.evidence_ids)
        evidence.update(link.evidence_id for link in links)
        evidence.discard("synthetic")

        findings = CaseFindings(
            engine_version=__version__,
            data_mode=self.mode,
            request=request,
            label_snapshot=self.labels.snapshot_hash(),
            directory_snapshot=self.directory.snapshot_hash(),
            policy_snapshot=self.policy.snapshot,
            policy_version=self.policy.version,
            traces=tuple(traces),
            verifications=tuple(verifications[k] for k in sorted(verifications)),
            analyses=tuple(analyses),
            subject_risks=tuple(subject_risks),
            crosschain_links=tuple(links),
            continuations=tuple(continuations),
            crosschain_errors=tuple(crosschain_errors),
            routing=tuple(sorted(decisions, key=lambda d: (d.chain, d.subject, d.direction, d.status, d.target_name))),
            evidence_ids=tuple(sorted(evidence)),
        )
        return CaseResult(
            case_id=case_id or uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc),
            findings=findings,
            findings_hash=findings_hash(findings),
            fetched_evidence_count=len(self.fetcher.used) if self.fetcher else 0,
        )


def load_standard(settings: Settings, include_synthetic: bool = False) -> tuple[LabelStore, AssetRegistry, VaspDirectory]:
    data = settings.data_dir
    extra = [p for p in (settings.var_dir / "labels").glob("*.jsonl")] if (settings.var_dir / "labels").exists() else []
    labels = LabelStore.load_dir(data / "labels", include_synthetic=include_synthetic, extra=extra)
    registry = AssetRegistry.load(data / "assets.yaml")
    directory = VaspDirectory.load(data / "vasp_directory.yaml", include_synthetic=include_synthetic)
    return labels, registry, directory


def ensure_dirs(settings: Settings) -> None:
    for d in (settings.var_dir, settings.evidence_dir, settings.reports_dir, settings.var_dir / "labels", settings.var_dir / "outbox"):
        Path(d).mkdir(parents=True, exist_ok=True)
