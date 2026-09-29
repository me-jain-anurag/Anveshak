"""HTTP API — the interface the Sahyog portal (or the LEA dashboard) talks to.

  Sahyog integration
    POST /v1/sahyog/reports                          reported wallets → auto-detected chains → queued case
  Cases
    POST /v1/cases                                   queue a case (live or synthetic)
    GET  /v1/cases | /v1/cases/{id}                  list / status + findings
    GET  /v1/cases/{id}/report                       HTML report
    GET  /v1/cases/{id}/graph                        fund-flow graph (Cytoscape elements)
    GET  /v1/cases/{id}/export/{neo4j|graphml|json}  exports for graph-analytics engines
    GET  /v1/cases/{id}/links                        other cases sharing addresses
    POST /v1/cases/{id}/routing/{decision}/approve   officer approval → Sahyog gateway (dry-run)
  Intelligence
    GET  /v1/addresses/{chain}/{address}             label attribution + risk (no chain calls)
    GET  /v1/detect/{address}                        chains an address is valid on
    POST /v1/attestations                            record a VASP confirmation (grade A label)
    GET  /v1/alerts, POST /v1/alerts/{id}/ack        alerts (case findings, watchlist, cross-case)
    GET/POST/DELETE /v1/watchlist                    addresses monitored for fund movement
    GET  /v1/analytics                               case-based analytics
    GET  /v1/meta                                    configuration, label snapshot, directory, policy

If ANVESHAK_API_TOKEN is set, every /v1 route except /v1/health requires `X-API-Key`.
Case execution runs in worker threads in this process (ANVESHAK_EMBEDDED_WORKERS) and/or
in separate `anveshak worker` processes sharing the same database.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, demo
from .addresses import AddressError, detect_chains, normalize
from .attribution import Attributor
from .case import CaseRequest, CaseResult, DataMode, Subject
from .chain import ETHERSCAN_FREE_TIER, Chain, ChainFamily
from .config import Settings, load_settings
from .domain import Category, Direction, SourceClass
from .exports import to_cypher, to_graphml
from .gateway import DryRunSahyogGateway
from .graph import build_graph
from .labels.importers import attestation_label
from .labels.store import LabelStore, write_jsonl
from .report import write_report
from .routing import RouteStatus
from .service import SahyogReport, Service

STATIC = Path(__file__).parent / "static"


class CaseCreate(BaseModel):
    mode: DataMode = DataMode.LIVE
    case_reference: str | None = Field(default=None, max_length=120)
    subjects: list[Subject] = Field(default_factory=list, max_length=20)
    directions: list[Direction] = Field(default_factory=lambda: [Direction.OUT, Direction.IN])
    max_hops: int = Field(default=5, ge=1, le=10)
    max_branch: int = Field(default=20, ge=1, le=200)
    max_expansions: int = Field(default=150, ge=1, le=5000)
    since: datetime | None = None
    until: datetime | None = None
    follow_all_assets: bool = False
    include_unverified_assets: bool = False
    follow_cross_chain: bool = True
    requested_by: str | None = Field(default=None, max_length=200)


class Approval(BaseModel):
    officer_name: str = Field(min_length=2, max_length=120)
    officer_id: str = Field(min_length=2, max_length=60)
    note: str | None = Field(default=None, max_length=1000)


class AttestationIn(BaseModel):
    chain: Chain
    address: str
    entity_id: str = Field(min_length=2, max_length=60)
    entity_name: str = Field(min_length=2, max_length=120)
    category: Category = Category.EXCHANGE
    document_ref: str = Field(min_length=4, max_length=300, description="e.g. 'Sahyog reply REF-123 dated 2026-10-02 from Binance'")
    as_of: date
    source_class: SourceClass = SourceClass.ENTITY_ATTESTED


class WatchIn(BaseModel):
    chain: Chain
    address: str
    reason: str = Field(min_length=3, max_length=300)
    case_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or load_settings())
    s = service.settings
    app = FastAPI(title="Anveshak — VASP attribution engine", version=__version__)
    app.state.service = service
    stop = threading.Event()
    app.state.stop = stop
    for i in range(max(0, s.embedded_workers)):
        threading.Thread(target=service.worker_loop, args=(f"api-{uuid.uuid4().hex[:6]}-{i}", stop, 0.2), daemon=True).start()
    if s.monitor_interval_seconds > 0:
        threading.Thread(target=service.monitor_loop, args=(stop,), daemon=True).start()

    def auth(x_api_key: str | None = Header(default=None)) -> None:
        if s.api_token and x_api_key != s.api_token:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    def load_result(case_id: str) -> CaseResult:
        row = service.store.get(case_id)
        if row is None:
            raise HTTPException(404, "case not found")
        if row["status"] != "done":
            raise HTTPException(409, f"case is {row['status']}")
        return CaseResult.model_validate_json(row["result_json"])

    # ------------------------------------------------------------------ meta

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/meta", dependencies=[Depends(auth)])
    def meta() -> dict:
        chains = []
        for chain in Chain:
            if chain.family is ChainFamily.EVM:
                ok = bool(s.etherscan_api_key)
                note = "Etherscan API V2" + ("" if ok else " — ETHERSCAN_API_KEY not set")
                if chain not in ETHERSCAN_FREE_TIER:
                    note += " (not on Etherscan's free tier: paid plan or compatible provider)"
            elif chain is Chain.TRON:
                ok, note = True, "TronGrid" + (" (with API key)" if s.trongrid_api_key else " (no key: rate-limited)")
            elif chain is Chain.SOLANA:
                ok, note = True, f"JSON-RPC at {s.solana_rpc_url}"
            else:
                ok, note = True, f"Esplora at {s.esplora_base_url}"
            chains.append({"chain": chain.value, "name": chain.display_name, "configured": ok, "note": note})
        return {
            "version": __version__,
            "chains": chains,
            "labels": {"count": len(service.labels), "snapshot": service.labels.snapshot_hash(), "by_chain_and_class": service.labels.stats()},
            "policy": {"version": service.policy.version, "snapshot": service.policy.snapshot},
            "intel_providers": [p for p, on in (("chainalysis-sanctions-api", bool(s.chainalysis_api_key)), ("etherscan-nametag-api", s.etherscan_nametags)) if on],
            "directory": [
                {"entity_id": e.entity_id, "display_name": e.display_name, "role": e.role, "channels": len(e.channels),
                 "fiu_ind_registered": e.fiu_ind_registered.value, "sahyog_onboarded": e.sahyog_onboarded.value}
                for e in service.directory.entries()
            ],
            "assets": [{"chain": t.asset.chain.value, "contract": t.asset.contract, "symbol": t.asset.symbol, "issuer": t.issuer} for t in service.registry.tokens()],
            "workers": {"embedded": s.embedded_workers, "monitor_interval_seconds": s.monitor_interval_seconds},
        }

    # ------------------------------------------------------------------ Sahyog

    @app.post("/v1/sahyog/reports", status_code=202, dependencies=[Depends(auth)])
    def sahyog_report(body: SahyogReport) -> dict:
        result = service.ingest(body)
        if result["case_id"] is None:
            raise HTTPException(422, {"message": "no traceable wallet in the report", **result})
        return result

    # ------------------------------------------------------------------ cases

    @app.post("/v1/cases", status_code=202, dependencies=[Depends(auth)])
    def create_case(body: CaseCreate) -> dict:
        subjects = body.subjects
        max_hops = body.max_hops
        if body.mode is DataMode.SYNTHETIC:
            subjects = subjects or [Subject(**x) for x in demo.subjects()]
            max_hops = max(max_hops, demo.MAX_HOPS)
        elif body.mode is DataMode.REPLAY:
            raise HTTPException(400, "replay is run from the CLI: anveshak replay <case_id>")
        if not subjects:
            raise HTTPException(422, "at least one subject is required")
        if body.mode is DataMode.LIVE and any(x.chain.family is ChainFamily.EVM for x in subjects) and not s.etherscan_api_key:
            raise HTTPException(400, "ETHERSCAN_API_KEY is not configured — EVM chains cannot be traced")
        reference = body.case_reference or (demo.CASE_REFERENCE if body.mode is DataMode.SYNTHETIC else None)
        if not reference:
            raise HTTPException(422, "case_reference is required")
        request = CaseRequest(
            case_reference=reference, subjects=tuple(subjects), directions=tuple(body.directions), max_hops=max_hops,
            max_branch=body.max_branch, max_expansions=body.max_expansions, since=body.since, until=body.until,
            follow_all_assets=body.follow_all_assets, include_unverified_assets=body.include_unverified_assets,
            follow_cross_chain=body.follow_cross_chain, requested_by=body.requested_by,
        )
        return {"case_id": service.submit(request, body.mode), "status": "queued"}

    @app.get("/v1/cases", dependencies=[Depends(auth)])
    def list_cases(limit: int = 50) -> list[dict]:
        return service.store.list(limit=min(max(limit, 1), 500))

    @app.get("/v1/cases/{case_id}", dependencies=[Depends(auth)])
    def get_case(case_id: str) -> dict:
        row = service.store.get(case_id)
        if row is None:
            raise HTTPException(404, "case not found")
        out = {k: row[k] for k in ("case_id", "case_reference", "data_mode", "status", "error", "findings_hash", "sahyog_reference", "created_at", "updated_at")}
        if row["status"] == "done":
            out["result"] = CaseResult.model_validate_json(row["result_json"]).model_dump(mode="json")
            out["approvals"] = service.store.approvals(case_id)
            out["callbacks"] = service.store.callbacks(case_id)
        return out

    @app.get("/v1/cases/{case_id}/report", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def get_report(case_id: str) -> HTMLResponse:
        result = load_result(case_id)
        path = s.reports_dir / f"{result.case_id}.html"
        if not path.exists():
            write_report(result, s.reports_dir)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/v1/cases/{case_id}/graph", dependencies=[Depends(auth)])
    def get_graph(case_id: str) -> dict:
        result = load_result(case_id)
        return build_graph(list(result.findings.traces), list(result.findings.crosschain_links))

    @app.get("/v1/cases/{case_id}/export/{fmt}", dependencies=[Depends(auth)])
    def export(case_id: str, fmt: str) -> Response:
        result = load_result(case_id)
        if fmt == "neo4j":
            return PlainTextResponse(to_cypher(result), headers={"Content-Disposition": f'attachment; filename="{case_id}.cypher"'})
        if fmt == "graphml":
            return Response(to_graphml(result), media_type="application/graphml+xml", headers={"Content-Disposition": f'attachment; filename="{case_id}.graphml"'})
        if fmt == "json":
            return Response(result.model_dump_json(indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{case_id}.json"'})
        raise HTTPException(404, "format must be neo4j, graphml or json")

    @app.get("/v1/cases/{case_id}/links", dependencies=[Depends(auth)])
    def case_links(case_id: str) -> list[dict]:
        if service.store.get(case_id) is None:
            raise HTTPException(404, "case not found")
        return service.store.other_cases_for(case_id)

    @app.post("/v1/cases/{case_id}/routing/{decision_id}/approve", dependencies=[Depends(auth)])
    def approve(case_id: str, decision_id: str, body: Approval) -> dict:
        result = load_result(case_id)
        if result.findings.data_mode is DataMode.SYNTHETIC:
            raise HTTPException(409, "synthetic demo cases cannot be submitted")
        decision = next((d for d in result.findings.routing if d.id == decision_id), None)
        if decision is None:
            raise HTTPException(404, "routing decision not found")
        if decision.status is not RouteStatus.READY_FOR_APPROVAL:
            raise HTTPException(409, f"decision status is {decision.status.value}; only ready_for_approval drafts can be approved")
        if any(a["decision_id"] == decision_id for a in service.store.approvals(case_id)):
            raise HTTPException(409, "already approved")
        officer = {"name": body.officer_name, "officer_id": body.officer_id, "note": body.note}
        receipt = DryRunSahyogGateway(s.var_dir / "outbox").submit(case_id, result.findings.request.case_reference, result.findings_hash, decision, officer)
        service.store.record_approval(case_id, decision_id, body.officer_name, body.officer_id, body.note, receipt)
        return {"decision_id": decision_id, "receipt": receipt}

    # ------------------------------------------------------------------ intelligence

    @app.get("/v1/detect/{address}", dependencies=[Depends(auth)])
    def detect(address: str) -> dict:
        return {"address": address, "chains": [c.value for c in detect_chains(address)]}

    @app.get("/v1/addresses/{chain}/{address}", dependencies=[Depends(auth)])
    def lookup(chain: Chain, address: str) -> dict:
        try:
            canonical = normalize(chain, address)
        except AddressError as exc:
            raise HTTPException(422, f"invalid {chain.value} address: {exc}") from exc
        attributor = Attributor(service.labels, aliases=service.directory.aliases, trust=service.trust)
        att = attributor.ownership(chain, canonical)
        risk = attributor.risk(chain, canonical)
        return {
            "chain": chain.value,
            "address": canonical,
            "attribution": att.model_dump(mode="json") if att else None,
            "risk": risk.model_dump(mode="json") if risk else None,
            "seen_in_cases": service.store.raw("SELECT case_id, role FROM sightings WHERE chain=? AND address=?", (chain.value, canonical)),
            "note": "label lookup only — no chain data fetched",
        }

    @app.post("/v1/attestations", status_code=201, dependencies=[Depends(auth)])
    def attest(body: AttestationIn) -> dict:
        try:
            label = attestation_label(body.chain, body.address, body.entity_id, body.entity_name, body.category, body.document_ref, body.as_of, body.source_class)
        except (AddressError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        path = s.var_dir / "labels" / "attestations.jsonl"
        existing = list(LabelStore.load([path]).all()) if path.exists() else []
        write_jsonl(path, [*existing, label])
        service.reload()
        return {"label": label.model_dump(mode="json"), "effect": "future cases grade this address A (G-A1) for this entity unless another source conflicts"}

    @app.get("/v1/alerts", dependencies=[Depends(auth)])
    def alerts(limit: int = 100, case_id: str | None = None, unacknowledged: bool = False) -> list[dict]:
        return service.store.alerts(limit=min(max(limit, 1), 1000), case_id=case_id, unacknowledged=unacknowledged)

    @app.post("/v1/alerts/{alert_id}/ack", dependencies=[Depends(auth)])
    def ack(alert_id: int) -> dict:
        if not service.store.acknowledge(alert_id):
            raise HTTPException(404, "alert not found")
        return {"alert_id": alert_id, "acknowledged": True}

    @app.get("/v1/watchlist", dependencies=[Depends(auth)])
    def watchlist() -> list[dict]:
        return service.store.watches(active_only=True)

    @app.post("/v1/watchlist", status_code=201, dependencies=[Depends(auth)])
    def add_watch(body: WatchIn) -> dict:
        try:
            address = normalize(body.chain, body.address)
        except AddressError as exc:
            raise HTTPException(422, str(exc)) from exc
        watch_id = service.store.watch(body.chain.value, address, body.case_id, body.reason)
        return {"watch_id": watch_id, "chain": body.chain.value, "address": address}

    @app.delete("/v1/watchlist/{watch_id}", dependencies=[Depends(auth)])
    def remove_watch(watch_id: int) -> dict:
        if not service.store.deactivate_watch(watch_id):
            raise HTTPException(404, "watch not found")
        return {"watch_id": watch_id, "active": False}

    @app.get("/v1/analytics", dependencies=[Depends(auth)])
    def analytics() -> dict:
        return service.analytics()

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
