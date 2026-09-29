"""Service layer: queue workers, post-processing, watchlist monitor, Sahyog ingestion, analytics.

The API process and `anveshak worker` / `anveshak monitor` processes all use this module.
Everything that depends on time or on other cases (alerts from the watchlist, cross-case
sightings, callbacks) lives here, outside the reproducible findings of a single case.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from . import demo
from .addresses import detect_chains
from .case import CaseRequest, CaseResult, DataMode, Engine, Subject, ensure_dirs, load_standard
from .chain import Chain, ChainFamily
from .chains.evm import EtherscanSource
from .config import Settings
from .domain import Direction, EndpointKind
from .errors import AnveshakError, SourceError
from .evidence import EvidenceStore, LiveFetcher
from .intel import ChainalysisSanctions, EtherscanNametags
from .policy import ScoringPolicy
from .report import write_report
from .sourcetrust import SourceTrust
from .storage import CaseStore


class SahyogReport(BaseModel):
    """Wallets reported on the Sahyog portal for one investigation."""

    sahyog_reference: str = Field(min_length=3, max_length=120)
    agency: str | None = Field(default=None, max_length=200)
    officer: str | None = Field(default=None, max_length=200)
    wallets: list[str] = Field(min_length=1, max_length=50)
    directions: list[Direction] = Field(default_factory=lambda: [Direction.OUT, Direction.IN])
    since: datetime | None = None
    max_hops: int = Field(default=5, ge=1, le=10)
    callback_url: str | None = Field(default=None, max_length=500)


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        ensure_dirs(settings)
        self.store = CaseStore(settings.db_path)
        self._lock = threading.Lock()
        self.reload()

    # ------------------------------------------------------------------ resources

    def reload(self) -> None:
        with self._lock:
            self.labels, self.registry, self.directory = load_standard(self.settings)
            self.policy = ScoringPolicy.load(self.settings.data_dir / "scoring_policy.yaml")
            self.trust = SourceTrust.load(self.settings.data_dir / "authorities.yaml", self.directory.official_sources())

    def _live_fetcher(self) -> LiveFetcher:
        s = self.settings
        return LiveFetcher(
            EvidenceStore(s.evidence_dir),
            min_interval={
                "api.etherscan.io": 0.25,
                "api.trongrid.io": 0.2 if s.trongrid_api_key else 0.6,
                "blockstream.info": 0.25,
                "api.mainnet-beta.solana.com": 0.35,
                "gateway.liquify.com": 0.5,
                "public.chainalysis.com": 0.2,
            },
        )

    def engine(self, mode: DataMode) -> Engine:
        if mode is DataMode.SYNTHETIC:
            return demo.engine(self.registry, self.directory)
        fetcher = self._live_fetcher()
        providers = []
        if self.settings.chainalysis_api_key:
            providers.append(ChainalysisSanctions(fetcher, self.settings.chainalysis_api_key))
        if self.settings.etherscan_nametags and self.settings.etherscan_api_key:
            providers.append(EtherscanNametags(fetcher, self.settings.etherscan_api_key, self.settings.etherscan_base_url))
        return Engine(
            DataMode.LIVE, self.labels, self.registry, self.directory, settings=self.settings, fetcher=fetcher,
            policy=self.policy, trust=self.trust, providers=providers,
        )

    # ------------------------------------------------------------------ queue

    def submit(self, request: CaseRequest, mode: DataMode, sahyog_reference: str | None = None, callback_url: str | None = None) -> str:
        case_id = uuid.uuid4().hex
        self.store.create(case_id, request.case_reference, mode.value, request.model_dump(mode="json"), sahyog_reference, callback_url)
        return case_id

    def work_once(self, worker_id: str) -> bool:
        row = self.store.claim_next(worker_id)
        if row is None:
            return False
        self.run_job(row)
        return True

    def run_job(self, row: dict) -> None:
        case_id = row["case_id"]
        try:
            request = CaseRequest.model_validate_json(row["request_json"])
            result = self.engine(DataMode(row["data_mode"])).run(request, case_id=case_id)
            write_report(result, self.settings.reports_dir)
            self.store.set_done(case_id, result.model_dump_json(), result.findings_hash)
        except Exception as exc:  # noqa: BLE001 — recorded on the case, never swallowed
            self.store.set_failed(case_id, f"{type(exc).__name__}: {exc}")
            return
        self.post_process(result, row)

    def worker_loop(self, worker_id: str, stop: threading.Event, poll_seconds: float = 0.5) -> None:
        self.store.requeue_stale()
        while not stop.is_set():
            try:
                busy = self.work_once(worker_id)
            except Exception:  # noqa: BLE001 — a broken job must not kill the worker
                busy = False
            if not busy:
                stop.wait(poll_seconds)

    # ------------------------------------------------------------------ post-processing (time/cross-case dependent)

    def post_process(self, result: CaseResult, row: dict) -> None:
        f = result.findings
        live = f.data_mode is DataMode.LIVE
        for analysis in f.analyses:
            for a in analysis.alerts:
                self.store.add_alert(a.severity.value, a.rule, a.message, a.chain, a.addresses[0] if a.addresses else None, result.case_id)
        sightings = set()
        for trace in f.traces:
            sightings.add((trace.chain.value, trace.subject, "subject"))
            for e in trace.endpoints:
                sightings.add((e.chain.value, e.address, e.kind.value))
            for t in trace.transfers:
                sightings.add((t.chain.value, t.sender, "path"))
                sightings.add((t.chain.value, t.receiver, "path"))
        if live:  # synthetic demo addresses must never link to real cases
            self.store.record_sightings(result.case_id, sorted(sightings))
            seen = set()
            for link in self.store.other_cases_for(result.case_id):
                key = (link["chain"], link["address"], link["other_case_id"])
                # A VASP/service address shared by two cases is expected (e.g. an exchange hot
                # wallet); shared *unlabelled* addresses are the intelligence worth flagging.
                if key in seen or {link["role"], link["other_role"]} & {"vasp", "service"}:
                    continue
                seen.add(key)
                self.store.add_alert(
                    "high", "A-CROSS-CASE",
                    f"{link['address']} ({link['chain']}) also appears in case {link['other_reference']} as {link['other_role']}",
                    link["chain"], link["address"], result.case_id, {"other_case_id": link["other_case_id"]},
                )
            for trace in f.traces:
                for bal in trace.balances:
                    if bal.amount > 0:
                        self.store.watch(trace.chain.value, bal.address, result.case_id, f"{bal.formatted} held after trace ({bal.as_of})")
        callback = row.get("callback_url")
        if callback:
            self.send_callback(result, callback, row.get("sahyog_reference"))

    def send_callback(self, result: CaseResult, url: str, sahyog_reference: str | None) -> None:
        host = (urlsplit(url).hostname or "").lower()
        allowed = any(host == h or host.endswith("." + h) for h in self.settings.callback_allowlist)
        if urlsplit(url).scheme != "https" or not allowed:
            self.store.record_callback(result.case_id, url, "refused", "callback host not in ANVESHAK_CALLBACK_ALLOWLIST (or not https)")
            return
        f = result.findings
        body = {
            "case_id": result.case_id,
            "sahyog_reference": sahyog_reference,
            "case_reference": f.request.case_reference,
            "findings_hash": result.findings_hash,
            "nearest_vasps": [n.model_dump(mode="json") for a in f.analyses for n in a.nearest_vasps],
            "routing": [
                {k: v for k, v in d.model_dump(mode="json").items() if k != "draft_text"}
                for d in f.routing
            ],
        }
        raw = json.dumps(body, sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
        if self.settings.api_token:
            headers["X-Anveshak-Signature"] = "sha256=" + hmac.new(self.settings.api_token.encode(), raw, hashlib.sha256).hexdigest()
        try:
            response = httpx.post(url, content=raw, headers=headers, timeout=15)
            self.store.record_callback(result.case_id, url, f"http {response.status_code}", None)
        except httpx.HTTPError as exc:
            self.store.record_callback(result.case_id, url, "error", str(exc))

    # ------------------------------------------------------------------ watchlist monitor

    def monitor_once(self) -> list[int]:
        """Check every active watch; alert on any new outgoing transfer since the last check."""
        created: list[int] = []
        engine = self.engine(DataMode.LIVE)
        for watch in self.store.watches():
            chain = Chain(watch["chain"])
            try:
                history = engine.source(chain).history(watch["address"])
            except (AnveshakError, SourceError) as exc:
                self.store.add_alert("info", "A-WATCH-ERROR", f"could not check {watch['address']}: {exc}", watch["chain"], watch["address"], watch["case_id"])
                continue
            outgoing = {t.id: t for t in history.transfers if t.sender == watch["address"]}
            if watch["baseline_json"] is None:
                self.store.update_watch(watch["watch_id"], sorted(outgoing))
                continue
            baseline = set(json.loads(watch["baseline_json"]))
            new = [outgoing[i] for i in sorted(set(outgoing) - baseline, key=lambda i: outgoing[i].order_key)]
            for t in new:
                created.append(
                    self.store.add_alert(
                        "critical", "A-WATCH-MOVEMENT",
                        f"watched funds moved: {t.formatted_amount} from {t.sender} to {t.receiver} in {t.tx_hash}",
                        watch["chain"], watch["address"], watch["case_id"], {"tx": t.tx_hash, "explorer": chain.tx_url(t.tx_hash)},
                    )
                )
            if new and self.settings.auto_follow_up:
                original = self.store.get(watch["case_id"]) if watch["case_id"] else None
                ref = f"{original['case_reference'] if original else 'watch'} / follow-up {datetime.now(timezone.utc):%Y-%m-%d %H:%M}"
                request = CaseRequest(case_reference=ref[:120], subjects=(Subject(chain=chain, address=watch["address"]),), directions=(Direction.OUT,), since=new[0].timestamp)
                self.submit(request, DataMode.LIVE)
            self.store.update_watch(watch["watch_id"], sorted(outgoing))
        return created

    def monitor_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.settings.monitor_interval_seconds):
            try:
                self.monitor_once()
            except Exception:  # noqa: BLE001 — keep monitoring
                pass

    # ------------------------------------------------------------------ Sahyog ingestion

    def ingest(self, report: SahyogReport) -> dict:
        accepted, rejected, subjects = [], [], []
        engine = None
        for raw in report.wallets:
            wallet = raw.strip()
            chains = detect_chains(wallet)
            if not chains:
                rejected.append({"input": raw, "reason": "not a valid address on any supported chain (format/checksum)"})
                continue
            if chains[0].family is ChainFamily.EVM:
                if not self.settings.etherscan_api_key:
                    rejected.append({"input": raw, "reason": "EVM address but ETHERSCAN_API_KEY is not configured"})
                    continue
                engine = engine or self.engine(DataMode.LIVE)
                active, notes = [], []
                for name in self.settings.evm_probe_chains:
                    chain = Chain(name)
                    try:
                        source = engine.source(chain).inner
                        if isinstance(source, EtherscanSource) and source.has_activity(wallet):
                            active.append(chain)
                    except (AnveshakError, SourceError) as exc:
                        notes.append(f"{name}: {exc}")
                if not active:
                    rejected.append({"input": raw, "reason": "no activity on probed EVM chains", "notes": notes})
                    continue
                chains = active
                accepted.append({"input": raw, "chains": [c.value for c in chains], "notes": notes})
            else:
                accepted.append({"input": raw, "chains": [c.value for c in chains]})
            subjects += [Subject(chain=c, address=wallet) for c in chains]
        if not subjects:
            return {"case_id": None, "accepted": accepted, "rejected": rejected}
        reference = f"{report.sahyog_reference}" + (f" — {report.agency}" if report.agency else "")
        request = CaseRequest(
            case_reference=reference[:120], subjects=tuple(subjects[:20]), directions=tuple(report.directions),
            since=report.since, max_hops=report.max_hops, requested_by=report.officer,
        )
        case_id = self.submit(request, DataMode.LIVE, report.sahyog_reference, report.callback_url)
        return {"case_id": case_id, "accepted": accepted, "rejected": rejected, "subjects_traced": len(request.subjects)}

    # ------------------------------------------------------------------ analytics

    def analytics(self) -> dict:
        rows = self.store.done_results(limit=1000)
        by_mode: Counter = Counter()
        chains: Counter = Counter()
        typologies: Counter = Counter()
        risk_levels: Counter = Counter()
        route_status: Counter = Counter()
        vasps: dict[str, dict] = defaultdict(lambda: {"cases": set(), "ready_drafts": 0, "best_confidence": 0, "min_hops": None})
        hops_hist: Counter = Counter()
        links = 0
        for row in rows:
            data = json.loads(row["result_json"])
            f = data["findings"]
            by_mode[row["data_mode"]] += 1
            links += len(f.get("crosschain_links", []))
            for t in f["traces"]:
                chains[t["chain"]] += 1
            for a in f["analyses"]:
                for h in a["typologies"]:
                    typologies[h["typology"]] += 1
                for n in a["nearest_vasps"]:
                    if n["grade"] == "X":
                        continue
                    v = vasps[n["entity_name"] or n["entity_id"] or "?"]
                    v["cases"].add(row["case_id"])
                    v["best_confidence"] = max(v["best_confidence"], n["confidence"])
                    v["min_hops"] = n["hops"] if v["min_hops"] is None else min(v["min_hops"], n["hops"])
                    if n["rank"] == 1:
                        hops_hist[n["hops"]] += 1
            for r in f["subject_risks"]:
                risk_levels[r["level"]] += 1
            for d in f["routing"]:
                route_status[f"{d['request_type']}:{d['status']}"] += 1
                if d["status"] == "ready_for_approval" and d.get("target_entity_id"):
                    for name, v in vasps.items():
                        if name == d["target_name"] or name == (d["target_name"] or "").split(" (")[0]:
                            v["ready_drafts"] += 1
        alerts = Counter(a["severity"] for a in self.store.alerts(limit=10_000))
        return {
            "cases": self.store.status_counts(),
            "completed_by_mode": dict(by_mode),
            "traces_by_chain": dict(chains),
            "vasps_reached": sorted(
                ({"vasp": k, "cases": len(v["cases"]), "ready_drafts": v["ready_drafts"], "best_confidence": v["best_confidence"], "min_hops": v["min_hops"]} for k, v in vasps.items()),
                key=lambda x: (-x["cases"], x["vasp"]),
            ),
            "nearest_vasp_hops": {str(k): v for k, v in sorted(hops_hist.items())},
            "typologies": dict(typologies),
            "subject_risk_levels": dict(risk_levels),
            "routing": dict(route_status),
            "cross_chain_links": links,
            "alerts_by_severity": dict(alerts),
            "watchlist_active": len(self.store.watches()),
        }
