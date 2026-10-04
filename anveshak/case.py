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
from .chain import ETHERSCAN_FREE_TIER, Chain, ChainFamily
from .chains.base import CachingSource, ChainSource, Verification
from .chains.bitcoin import EsploraSource
from .chains.evm import EtherscanSource
from .chains.rpc import RpcLogSource
from .chains.solana import SolanaRpcSource
from .chains.tron import TronGridSource
from .analysis import TraceAnalysis, analyze
from .config import DATA_DIR, Settings
from .crosschain import RESOLVERS, CrossChainLink, recipient_from_destination
from .directory import VaspDirectory
from .domain import Direction, EndpointKind, Frozen
from .errors import ConfigError, SourceError
from .evidence import EvidenceStore, Fetcher, LiveFetcher, ReplayFetcher, canonical_json, redact_endpoint, sha256_hex
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


class ChainSourceConfig(Frozen):
    """How one chain's data was obtained. No secrets: POST endpoints are recorded redacted."""

    chain: Chain
    backend: str  # "etherscan" | "rpc" (window-limited log scan) | "trongrid" | "solana-rpc" | "esplora"
    endpoint: str
    window_hours: int | None = None  # rpc only
    max_span: int | None = None  # rpc only
    max_signatures: int | None = None  # solana-rpc only


class SourceConfig(Frozen):
    """The data-source settings that decide which requests a case makes (ADR-0025).

    Recorded in the findings so that a replay asks the evidence store for exactly the requests
    the live run made, whatever the replaying machine is configured with.
    """

    chains: tuple[ChainSourceConfig, ...] = ()
    resolvers: tuple[str, ...] = ()


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
    source_config: SourceConfig | None = None  # None: synthetic, or recorded before ADR-0025


class CaseResult(Frozen):
    case_id: str
    created_at: datetime
    findings: CaseFindings
    findings_hash: str
    fetched_evidence_count: int  # everything fetched, including pages that yielded no findings


def findings_hash(findings: CaseFindings) -> str:
    data = findings.model_dump(mode="json")
    if data.get("source_config") is None:
        data.pop("source_config", None)  # findings without it hash exactly as they did before ADR-0025
    return sha256_hex(canonical_json(data))


def evm_backend(settings: Settings, chain: Chain) -> str:
    """Which data source an EVM chain uses (ADR-0021): "etherscan" when the configured key's
    plan covers the chain, else "rpc" (window-limited log scan) when an RPC URL is known."""
    if settings.etherscan_api_key and (chain in ETHERSCAN_FREE_TIER or settings.etherscan_paid):
        return "etherscan"
    if settings.evm_rpc_urls.get(chain.value):
        return "rpc"
    return "etherscan"  # will fail with a clear ConfigError about the missing key


def chains_needing_since(settings: Settings, chains) -> list[Chain]:
    return [c for c in chains if c.family is ChainFamily.EVM and evm_backend(settings, c) == "rpc"]


CROSSCHAIN_ENDPOINTS = frozenset({EndpointKind.SERVICE, EndpointKind.UNLABELED_CONTRACT, EndpointKind.HIGH_ACTIVITY})


LIVE_MIN_INTERVAL = {
    "api.etherscan.io": 0.25,
    "blockstream.info": 0.25,
    "api.mainnet-beta.solana.com": 0.35,
    "gateway.liquify.com": 0.5,
    "api.wormholescan.io": 0.5,
    "scan.layerzero-api.com": 0.5,
    "app.across.to": 0.5,
}


def live_fetcher(settings: Settings, store: EvidenceStore) -> LiveFetcher:
    """The live fetcher with the per-host pacing used everywhere (CLI, API workers, benchmark)."""
    return LiveFetcher(store, min_interval={**LIVE_MIN_INTERVAL, "api.trongrid.io": 0.2 if settings.trongrid_api_key else 0.6})


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
        source_config: SourceConfig | None = None,
    ):
        self.mode = mode
        self.labels = labels
        self.registry = registry
        self.directory = directory
        self.policy = policy or ScoringPolicy.load(DATA_DIR / "scoring_policy.yaml")
        self.trust = trust or SourceTrust.load(DATA_DIR / "authorities.yaml", directory.official_sources())
        self.providers = providers or []
        self.settings = settings
        self.window_start: datetime | None = None  # bounds RPC log scans (set from the request)
        self._sources: dict[Chain, ChainSource] = dict(sources or {})
        # replay: reuse the recorded source configuration; live: record what is used
        if source_config is not None and mode is not DataMode.REPLAY:
            raise ValueError("a recorded source configuration is only used to replay (its endpoints are redacted)")
        self._recorded = {c.chain: c for c in source_config.chains} if source_config is not None else {}
        self._used: dict[Chain, ChainSourceConfig] = {}
        self.fetcher = fetcher
        if mode is DataMode.SYNTHETIC and not sources:
            raise ValueError("synthetic mode needs in-memory sources")
        if mode is not DataMode.SYNTHETIC and fetcher is None:
            if settings is None:
                raise ValueError("live/replay mode needs settings or a fetcher")
            store = EvidenceStore(settings.evidence_dir)
            self.fetcher = ReplayFetcher(store) if mode is DataMode.REPLAY else live_fetcher(settings, store)
        if resolvers is not None:
            self.resolvers = resolvers
        elif mode is DataMode.SYNTHETIC:
            self.resolvers = []
        else:
            if source_config is not None:
                names = source_config.resolvers
            else:
                names = settings.crosschain_resolvers if settings is not None else tuple(RESOLVERS)
            self.resolvers = [RESOLVERS[n](self.fetcher) for n in names if n in RESOLVERS]

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
        if chain in self._recorded:
            cfg = self._recorded[chain]
            url = cfg.endpoint  # redacted where needed; request keys are computed on the redacted form
        else:
            cfg, url = self._configured(chain)
        if cfg.backend == "rpc":
            if self.window_start is None:
                raise ConfigError(
                    f"{chain.display_name} is traced through a public RPC log scan (no Etherscan plan covers it): "
                    "the incident time `since` is required to bound the scan window"
                )
            src: ChainSource = RpcLogSource(
                chain, self.fetcher, self.registry, rpc_url=url, window_start=self.window_start,
                window_hours=cfg.window_hours or s.logscan_window_hours, max_span=cfg.max_span or 5000,
            )
        elif cfg.backend == "etherscan":
            src = EtherscanSource(
                chain, self.fetcher, self.registry, api_key=s.etherscan_api_key, base_url=url,
                require_key=self.mode is DataMode.LIVE,
            )
        elif cfg.backend == "trongrid":
            src = TronGridSource(self.fetcher, self.registry, api_key=s.trongrid_api_key, base_url=url)
        elif cfg.backend == "solana-rpc":
            src = SolanaRpcSource(self.fetcher, self.registry, rpc_url=url, max_signatures=cfg.max_signatures or s.solana_max_signatures)
        else:
            src = EsploraSource(self.fetcher, self.registry, base_url=url)
        self._used[chain] = cfg
        self._sources[chain] = CachingSource(src)
        return self._sources[chain]

    def _configured(self, chain: Chain) -> tuple[ChainSourceConfig, str]:
        """(what the findings record, the URL actually called) for a chain, from the settings."""
        s = self.settings
        assert s is not None
        if chain.family is ChainFamily.EVM and evm_backend(s, chain) == "rpc":
            url = s.evm_rpc_urls[chain.value]
            cfg = ChainSourceConfig(
                chain=chain, backend="rpc", endpoint=redact_endpoint(url),
                window_hours=s.logscan_window_hours, max_span=int(s.rpc_max_span.get(chain.value, 5000)),
            )
        elif chain.family is ChainFamily.EVM:
            url = s.etherscan_base_url
            cfg = ChainSourceConfig(chain=chain, backend="etherscan", endpoint=url)
        elif chain is Chain.TRON:
            url = s.trongrid_base_url
            cfg = ChainSourceConfig(chain=chain, backend="trongrid", endpoint=url)
        elif chain is Chain.SOLANA:
            url = s.solana_rpc_url
            cfg = ChainSourceConfig(chain=chain, backend="solana-rpc", endpoint=redact_endpoint(url), max_signatures=s.solana_max_signatures)
        else:
            url = s.esplora_base_url
            cfg = ChainSourceConfig(chain=chain, backend="esplora", endpoint=url)
        return cfg, url

    def source_config(self) -> SourceConfig | None:
        if self.mode is DataMode.SYNTHETIC:
            return None
        names = tuple(getattr(r, "name", type(r).__name__) for r in self.resolvers)
        return SourceConfig(chains=tuple(self._used[c] for c in sorted(self._used, key=lambda c: c.value)), resolvers=names)

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
                if result.chain not in getattr(resolver, "chains", frozenset(Chain)):
                    continue
                try:
                    links = resolver.resolve(result.chain, last.tx_hash, last.sender, e.id)
                except SourceError as exc:
                    errors.append(f"{resolver.name} lookup for {last.tx_hash}: {exc}")
                    continue
                found.extend(self._confirm_destination(link) for link in links)
        return found

    def _confirm_destination(self, link: CrossChainLink) -> CrossChainLink:
        """Check that the protocol's outbound transaction really pays `to_address` on `to_chain`:
        first at transaction level (`tx_transfers`), else in the destination address history.
        A link whose record names no recipient (LayerZero) gets it from the destination
        transaction, only if a single receiver remains after excluding protocol contracts."""
        if link.to_chain is None or not link.to_tx:
            return link
        try:
            source = self.source(link.to_chain)
            in_tx = source.tx_transfers(link.to_tx)
            if not link.to_address:
                if in_tx is None:
                    return link.model_copy(update={"destination_detail": "recipient unresolved: the destination source cannot list a transaction's transfers"})
                recipient, why = recipient_from_destination(link, in_tx)
                if recipient is None:
                    return link.model_copy(update={"destination_detail": f"recipient unresolved: {why}"})
                paid = [t for t in in_tx if t.receiver == recipient]
                return link.model_copy(update={
                    "to_address": recipient, "recipient_basis": why, "destination_confirmed": True,
                    "amount_out": link.amount_out or paid[0].formatted_amount,
                    "destination_detail": f"{paid[0].formatted_amount} received in {paid[0].tx_hash}",
                })
            if in_tx is not None:
                paid = [t for t in in_tx if t.receiver == link.to_address]
                if paid:
                    return link.model_copy(update={"destination_confirmed": True, "destination_detail": f"{paid[0].formatted_amount} received in {paid[0].tx_hash}"})
                if link.to_chain is Chain.SOLANA and in_tx:
                    # Solana transfers are reported per wallet (token-account owner); a protocol
                    # record may name the token account instead. Not a contradiction, not a confirmation.
                    owners = sorted({t.receiver for t in in_tx})
                    return link.model_copy(update={"destination_confirmed": None, "destination_detail": (
                        "the record's recipient is not a wallet paid in the destination transaction; it may be a token account "
                        f"(wallets paid: {', '.join(owners[:4])})")})
            history = source.history(link.to_address)
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
        self.window_start = request.since
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
                    if link.to_chain is not None and link.to_tx and link.to_address and depth < request.cross_chain_depth and link.destination_confirmed is not False:
                        origin = next(t for t in result.transfers if t.tx_hash == link.from_tx)
                        hops_used = next(e.hops for e in result.endpoints if e.id == link.endpoint_id)
                        cont = params.model_copy(update={"since": origin.timestamp, "until": None, "max_hops": max(1, params.max_hops - hops_used)})
                        work.append((link.to_chain, link.to_address, cont, depth + 1, index, link.id))

        freezable = frozenset(t.asset.key for t in self.registry.tokens() if t.issuer)
        analyses, subject_risks = analyze(traces, verifications, self.policy, self.trust, links, freezable)
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
            source_config=self.source_config(),
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
