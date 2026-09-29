"""HTTP API — the interface the Sahyog portal (or an investigator dashboard) talks to.

    POST /v1/cases                                   queue a case (live or synthetic)
    GET  /v1/cases                                   list cases
    GET  /v1/cases/{id}                              status + findings
    GET  /v1/cases/{id}/report                       HTML report
    GET  /v1/cases/{id}/graph                        fund-flow graph (Cytoscape elements)
    POST /v1/cases/{id}/routing/{decision}/approve   officer approval → Sahyog gateway (dry-run)
    GET  /v1/addresses/{chain}/{address}             label-only attribution + risk lookup
    POST /v1/attestations                            record a VASP confirmation (grade A label)
    GET  /v1/meta                                    configuration, label snapshot, directory

If ANVESHAK_API_TOKEN is set, every /v1 route except /v1/health requires `X-API-Key`.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, demo
from .addresses import AddressError, normalize
from .attribution import Attributor
from .case import CaseRequest, CaseResult, DataMode, Engine, Subject, ensure_dirs, load_standard
from .chain import Chain, ChainFamily
from .config import Settings, load_settings
from .domain import Category, Direction, SourceClass
from .gateway import DryRunSahyogGateway
from .graph import build_graph
from .labels.importers import attestation_label
from .labels.store import LabelStore, write_jsonl
from .report import write_report
from .routing import RouteStatus
from .storage import CaseStore

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


class _State:
    def __init__(self, settings: Settings):
        self.settings = settings
        ensure_dirs(settings)
        self.store = CaseStore(settings.db_path)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self.lock:
            self.labels, self.registry, self.directory = load_standard(self.settings)

    def engine(self, mode: DataMode) -> Engine:
        if mode is DataMode.SYNTHETIC:
            return demo.engine(self.registry, self.directory)
        return Engine(DataMode.LIVE, self.labels, self.registry, self.directory, settings=self.settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    state = _State(settings or load_settings())
    app = FastAPI(title="Anveshak — VASP attribution engine", version=__version__)
    app.state.anveshak = state

    def auth(x_api_key: str | None = Header(default=None)) -> None:
        token = state.settings.api_token
        if token and x_api_key != token:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")

    def load_result(case_id: str) -> CaseResult:
        row = state.store.get(case_id)
        if row is None:
            raise HTTPException(404, "case not found")
        if row["status"] != "done":
            raise HTTPException(409, f"case is {row['status']}")
        return CaseResult.model_validate_json(row["result_json"])

    def run_case(case_id: str, mode: DataMode, request: CaseRequest) -> None:
        state.store.set_running(case_id)
        try:
            result = state.engine(mode).run(request, case_id=case_id)
            write_report(result, state.settings.reports_dir)
            state.store.set_done(case_id, result.model_dump_json(), result.findings_hash)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as the case error
            state.store.set_failed(case_id, f"{type(exc).__name__}: {exc}")

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/meta", dependencies=[Depends(auth)])
    def meta() -> dict:
        s = state.settings
        chains = []
        for chain in Chain:
            if chain.family is ChainFamily.EVM:
                ok = bool(s.etherscan_api_key)
                note = "Etherscan API V2" + ("" if ok else " — ETHERSCAN_API_KEY not set")
                if chain is Chain.BSC:
                    note += " (BSC is not on Etherscan's free tier)"
            elif chain is Chain.TRON:
                ok, note = True, "TronGrid" + (" (with API key)" if s.trongrid_api_key else " (no key: rate-limited)")
            else:
                ok, note = True, f"Esplora at {s.esplora_base_url}"
            chains.append({"chain": chain.value, "configured": ok, "note": note})
        return {
            "version": __version__,
            "chains": chains,
            "labels": {"count": len(state.labels), "snapshot": state.labels.snapshot_hash(), "by_chain_and_class": state.labels.stats()},
            "directory": [
                {"entity_id": e.entity_id, "display_name": e.display_name, "role": e.role, "channels": len(e.channels)}
                for e in state.directory.entries()
            ],
            "assets": [{"chain": t.asset.chain.value, "contract": t.asset.contract, "symbol": t.asset.symbol} for t in state.registry.tokens()],
        }

    @app.post("/v1/cases", status_code=202, dependencies=[Depends(auth)])
    def create_case(body: CaseCreate) -> dict:
        subjects = body.subjects
        if body.mode is DataMode.SYNTHETIC:
            subjects = subjects or [Subject(**s) for s in demo.subjects()]
            body = body.model_copy(update={"max_hops": max(body.max_hops, demo.MAX_HOPS)})
        elif body.mode is DataMode.REPLAY:
            raise HTTPException(400, "replay is run from the CLI: anveshak replay <case_id>")
        if not subjects:
            raise HTTPException(422, "at least one subject is required")
        if body.mode is DataMode.LIVE and any(s.chain.family is ChainFamily.EVM for s in subjects) and not state.settings.etherscan_api_key:
            raise HTTPException(400, "ETHERSCAN_API_KEY is not configured — EVM chains cannot be traced")
        reference = body.case_reference or (demo.CASE_REFERENCE if body.mode is DataMode.SYNTHETIC else None)
        if not reference:
            raise HTTPException(422, "case_reference is required")
        request = CaseRequest(
            case_reference=reference,
            subjects=tuple(subjects),
            directions=tuple(body.directions),
            max_hops=body.max_hops,
            max_branch=body.max_branch,
            max_expansions=body.max_expansions,
            since=body.since,
            until=body.until,
            follow_all_assets=body.follow_all_assets,
            include_unverified_assets=body.include_unverified_assets,
            requested_by=body.requested_by,
        )
        case_id = uuid.uuid4().hex
        state.store.create(case_id, reference, body.mode.value, request.model_dump(mode="json"))
        state.executor.submit(run_case, case_id, body.mode, request)
        return {"case_id": case_id, "status": "queued"}

    @app.get("/v1/cases", dependencies=[Depends(auth)])
    def list_cases(limit: int = 50) -> list[dict]:
        return state.store.list(limit=min(max(limit, 1), 500))

    @app.get("/v1/cases/{case_id}", dependencies=[Depends(auth)])
    def get_case(case_id: str) -> dict:
        row = state.store.get(case_id)
        if row is None:
            raise HTTPException(404, "case not found")
        out = {k: row[k] for k in ("case_id", "case_reference", "data_mode", "status", "error", "findings_hash", "created_at", "updated_at")}
        if row["status"] == "done":
            out["result"] = CaseResult.model_validate_json(row["result_json"]).model_dump(mode="json")
            out["approvals"] = state.store.approvals(case_id)
        return out

    @app.get("/v1/cases/{case_id}/report", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def get_report(case_id: str) -> HTMLResponse:
        result = load_result(case_id)
        path = state.settings.reports_dir / f"{result.case_id}.html"
        if not path.exists():
            write_report(result, state.settings.reports_dir)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/v1/cases/{case_id}/graph", dependencies=[Depends(auth)])
    def get_graph(case_id: str) -> dict:
        return build_graph(list(load_result(case_id).findings.traces))

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
        if any(a["decision_id"] == decision_id for a in state.store.approvals(case_id)):
            raise HTTPException(409, "already approved")
        officer = {"name": body.officer_name, "officer_id": body.officer_id, "note": body.note}
        gateway = DryRunSahyogGateway(state.settings.var_dir / "outbox")
        receipt = gateway.submit(case_id, result.findings.request.case_reference, result.findings_hash, decision, officer)
        state.store.record_approval(case_id, decision_id, body.officer_name, body.officer_id, body.note, receipt)
        return {"decision_id": decision_id, "receipt": receipt}

    @app.get("/v1/addresses/{chain}/{address}", dependencies=[Depends(auth)])
    def lookup(chain: Chain, address: str) -> dict:
        try:
            canonical = normalize(chain, address)
        except AddressError as exc:
            raise HTTPException(422, f"invalid {chain.value} address: {exc}") from exc
        attributor = Attributor(state.labels, aliases=state.directory.aliases)
        att = attributor.ownership(chain, canonical)
        risk = attributor.risk(chain, canonical)
        return {
            "chain": chain.value,
            "address": canonical,
            "attribution": att.model_dump(mode="json") if att else None,
            "risk": risk.model_dump(mode="json") if risk else None,
            "note": "label lookup only — no chain data fetched",
        }

    @app.post("/v1/attestations", status_code=201, dependencies=[Depends(auth)])
    def attest(body: AttestationIn) -> dict:
        try:
            label = attestation_label(body.chain, body.address, body.entity_id, body.entity_name, body.category, body.document_ref, body.as_of, body.source_class)
        except (AddressError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        path = state.settings.var_dir / "labels" / "attestations.jsonl"
        existing = list(LabelStore.load([path]).all()) if path.exists() else []
        write_jsonl(path, [*existing, label])
        state.reload()
        return {"label": label.model_dump(mode="json"), "effect": "future cases grade this address A (G-A1) for this entity unless another source conflicts"}

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
