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
from .chains.base import ChainSource, Verification
from .chains.bitcoin import EsploraSource
from .chains.evm import EtherscanSource
from .chains.tron import TronGridSource
from .analysis import TraceAnalysis, analyze
from .config import DATA_DIR, Settings
from .directory import VaspDirectory
from .domain import Direction, Frozen
from .errors import ConfigError
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
                },
            )

    def source(self, chain: Chain) -> ChainSource:
        if chain in self._sources:
            return self._sources[chain]
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
        else:
            src = EsploraSource(self.fetcher, self.registry, base_url=s.esplora_base_url)
        self._sources[chain] = src
        return src

    def run(self, request: CaseRequest, case_id: str | None = None) -> CaseResult:
        attributor = Attributor(self.labels, aliases=self.directory.aliases, trust=self.trust, providers=self.providers)
        traces: list[TraceResult] = []
        verifications: dict[str, Verification] = {}
        for subject in request.subjects:
            source = self.source(subject.chain)
            tracer = Tracer(source, attributor, self.registry)
            for direction in request.directions:
                result = tracer.trace(subject.address, request.params(direction))
                traces.append(result)
                verifications.update(verify_paths(result, source, already=verifications))

        analyses, subject_risks = analyze(traces, verifications, self.policy, self.trust)
        scores = {s.endpoint_id: s for a in analyses for s in a.confidences}
        decisions = route(request.case_reference, traces, verifications, self.directory, self.registry, scores, self.policy)

        evidence = set()
        for trace in traces:
            evidence.update(t.evidence_id for t in trace.transfers)
            evidence.update(b.evidence_id for b in trace.balances)
        for v in verifications.values():
            evidence.update(v.evidence_ids)
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
