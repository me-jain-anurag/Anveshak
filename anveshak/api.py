"""HTTP API — the interface the Sahyog portal (and agency dashboards) call. Sahyog is the client.

  Sahyog integration (ADR-0019)
    POST /v1/sahyog/reports                              reported wallets → auto-detected chains → queued case
    GET  /v1/cases/{id}/recommendations                  routing recommendations (schema anveshak.recommendation/v1)
    POST /v1/cases/{id}/recommendations/{rid}/status     what happened to a recommendation (append-only)
    PUT  /v1/directory/sahyog-intermediaries             Sahyog's intermediary ids (exact-match mapping)
    POST /v1/screen                                      synchronous label/risk/sightings screen, no chain calls
    POST /v1/attestations                                record a VASP reply: confirms or denies an address
  Cases
    POST /v1/cases                                       queue a case (live or synthetic)
    GET  /v1/cases | /v1/cases/{id}                      list / status + findings
    POST /v1/cases/{id}/rerun                            queue the same request again (e.g. after a reply)
    GET  /v1/cases/{id}/report | graph | export/{fmt}    HTML report, graph, Neo4j/GraphML/JSON exports
    GET  /v1/cases/{id}/links                            other cases sharing addresses (other agencies anonymised)
    POST /v1/cases/{id}/routing/{decision}/approve       standalone pilots only (ANVESHAK_STANDALONE_APPROVALS)
  Intelligence and oversight
    GET  /v1/addresses/{chain}/{address}, /v1/detect/{address}
    GET  /v1/alerts, POST /v1/alerts/{id}/ack; GET/POST/DELETE /v1/watchlist; GET /v1/analytics
    GET  /v1/audit, /v1/audit/verify                     append-only, hash-chained access log (auditors)
    GET  /v1/meta, /v1/health

Authentication and scope (ADR-0023): each client has its own key (`X-API-Key`), roles,
optionally an agency (cases of other agencies are invisible to it), an IP allowlist and a
rate limit. Request bodies are limited to ANVESHAK_MAX_BODY_BYTES. TLS (and mTLS / OAuth for
the Sahyog link) terminate at the reverse proxy in front of this service.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, demo
from .addresses import AddressError, detect_chains, normalize
from .attribution import Attributor
from .case import CaseRequest, CaseResult, DataMode, Subject, chains_needing_since, evm_backend
from .chain import ETHERSCAN_FREE_TIER, Chain, ChainFamily
from .clients import ApiClient, RateLimiter
from .config import Settings, load_settings
from .domain import Category, Direction, SourceClass
from .exports import to_cypher, to_graphml
from .gateway import DryRunSahyogGateway
from .graph import build_graph
from .labels.importers import attestation_label
from .labels.store import LabelStore, write_jsonl
from .recommendations import IntermediaryUpload, StatusUpdate
from .report import write_report
from .routing import RouteStatus
from .service import SahyogReport, Service
from .storage import ALL

STATIC = Path(__file__).parent / "static"
CASE_PATH = re.compile(r"^/v1/cases/([0-9a-f]{32}|[A-Za-z0-9_-]{1,64})(/|$)")


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
    agency_id: str | None = Field(default=None, max_length=60)  # only honoured for cross-agency clients


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
    polarity: str = Field(default="confirms", pattern="^(confirms|denies)$")
    sahyog_reply_id: str | None = Field(default=None, max_length=120)


class ScreenItem(BaseModel):
    chain: Chain | None = None
    address: str = Field(min_length=1, max_length=200)


class ScreenIn(BaseModel):
    addresses: list[str | ScreenItem] = Field(min_length=1, max_length=100)


class WatchIn(BaseModel):
    chain: Chain
    address: str
    reason: str = Field(min_length=3, max_length=300)
    case_id: str | None = None


class BodyLimit:
    """Rejects request bodies above `max_bytes` (Content-Length, or counted while streaming)."""

    class _TooLarge(Exception):
        pass

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            return await self._reject(send)
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise BodyLimit._TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyLimit._TooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send):
        body = b'{"detail":"request body too large"}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or load_settings())
    s = service.settings
    app = FastAPI(title="Anveshak — VASP attribution engine", version=__version__)
    app.state.service = service
    stop = threading.Event()
    app.state.stop = stop
    limiter = RateLimiter()
    for i in range(max(0, s.embedded_workers)):
        threading.Thread(target=service.worker_loop, args=(f"api-{uuid.uuid4().hex[:6]}-{i}", stop, 0.2), daemon=True).start()
    if s.monitor_interval_seconds > 0:
        threading.Thread(target=service.monitor_loop, args=(stop,), daemon=True).start()

    # ------------------------------------------------------------------ auth, scope, audit

    def require(permission: str | None = None):
        def dependency(request: Request, x_api_key: str | None = Header(default=None)) -> ApiClient:
            client = service.clients.authenticate(x_api_key)
            if client is None:
                raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")
            request.state.client = client
            if not client.ip_allowed(request.client.host if request.client else None):
                raise HTTPException(status_code=403, detail="request address not in this client's IP allowlist")
            if not limiter.allow(client):
                raise HTTPException(status_code=429, detail=f"rate limit of {client.rate_limit_per_minute}/minute exceeded")
            if permission and not client.can(permission):
                raise HTTPException(status_code=403, detail=f"client {client.client_id} lacks permission {permission}")
            return client

        return dependency

    def scope(client: ApiClient):
        return ALL if client.sees_all_agencies else client.agency_id

    def visible_case(case_id: str, client: ApiClient) -> dict:
        row = service.store.get(case_id)
        if row is None or not (client.sees_all_agencies or row.get("agency_id") == client.agency_id):
            raise HTTPException(404, "case not found")  # never confirm another agency's case exists
        return row

    def load_result(case_id: str, client: ApiClient) -> CaseResult:
        row = visible_case(case_id, client)
        if row["status"] != "done":
            raise HTTPException(409, f"case is {row['status']}")
        try:
            return CaseResult.model_validate_json(row["result_json"])
        except ValidationError as exc:
            raise HTTPException(409, "case was produced by an older engine version; re-run it to use this view") from exc

    def reader(client: ApiClient) -> None:
        if not (client.can("case:read") or client.sees_all_agencies):
            raise HTTPException(403, f"client {client.client_id} cannot read cases")

    @app.middleware("http")
    async def audit(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/v1/") and path != "/v1/health":
            client = getattr(request.state, "client", None)
            m = CASE_PATH.match(path)
            try:
                service.store.audit(
                    client.client_id if client else "unauthenticated", client.agency_id if client else None,
                    request.client.host if request.client else None, request.method, path, response.status_code, m.group(1) if m else None,
                )
            except Exception:  # noqa: BLE001 — never fail a request on audit write; the gap shows in /v1/audit/verify counts
                pass
        return response

    app.add_middleware(BodyLimit, max_bytes=s.max_body_bytes)

    # ------------------------------------------------------------------ meta

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/meta")
    def meta(client: ApiClient = Depends(require())) -> dict:
        chains = []
        for chain in Chain:
            if chain.family is ChainFamily.EVM:
                if evm_backend(s, chain) == "rpc":
                    ok, note = True, f"public JSON-RPC log scan ({s.evm_rpc_urls[chain.value]}): verified tokens only, window-limited — requires the incident time"
                else:
                    ok = bool(s.etherscan_api_key)
                    note = "Etherscan API V2" + ("" if ok else " — ETHERSCAN_API_KEY not set")
                    if chain not in ETHERSCAN_FREE_TIER and not s.etherscan_paid:
                        note += " (not on Etherscan's free tier)"
            elif chain is Chain.TRON:
                ok, note = True, "TronGrid" + (" (with API key)" if s.trongrid_api_key else " (no key: rate-limited)")
            elif chain is Chain.SOLANA:
                ok, note = True, f"JSON-RPC at {s.solana_rpc_url}"
            else:
                ok, note = True, f"Esplora at {s.esplora_base_url}"
            chains.append({"chain": chain.value, "name": chain.display_name, "configured": ok, "note": note})
        mapping = service.intermediaries.load()
        return {
            "version": __version__,
            "client": {"client_id": client.client_id, "roles": list(client.roles), "agency_id": client.agency_id, "permissions": sorted(client.permissions)},
            "auth_mode": "open (development: no keys configured)" if service.clients.open_mode else ("per-client keys" if s.clients_path.exists() else "single token"),
            "standalone_approvals": s.standalone_approvals,
            "crosschain_resolvers": [r for r in s.crosschain_resolvers],
            "chains": chains,
            "labels": {"count": len(service.labels), "snapshot": service.labels.snapshot_hash(), "by_chain_and_class": service.labels.stats()},
            "policy": {"version": service.policy.version, "snapshot": service.policy.snapshot},
            "intel_providers": [p for p, on in (("chainalysis-sanctions-api", bool(s.chainalysis_api_key)), ("etherscan-nametag-api", s.etherscan_nametags)) if on],
            "directory": [
                {"entity_id": e.entity_id, "display_name": e.display_name, "role": e.role, "channels": len(e.channels),
                 "fiu_ind_registered": e.fiu_ind_registered.value, "sahyog_onboarded": e.sahyog_onboarded.value,
                 "sahyog_intermediary_id": mapping["entities"].get(e.entity_id)}
                for e in service.directory.entries()
            ],
            "assets": [{"chain": t.asset.chain.value, "contract": t.asset.contract, "symbol": t.asset.symbol, "issuer": t.issuer} for t in service.registry.tokens()],
            "workers": {"embedded": s.embedded_workers, "monitor_interval_seconds": s.monitor_interval_seconds},
        }

    # ------------------------------------------------------------------ Sahyog

    @app.post("/v1/sahyog/reports", status_code=202)
    def sahyog_report(body: SahyogReport, client: ApiClient = Depends(require("case:create"))) -> dict:
        agency = body.agency_id if client.sees_all_agencies else client.agency_id
        result = service.ingest(body, agency_id=agency)
        if result["case_id"] is None:
            raise HTTPException(422, {"message": "no traceable wallet in the report", **result})
        return result

    @app.get("/v1/cases/{case_id}/recommendations")
    def recommendations(case_id: str, client: ApiClient = Depends(require())) -> dict:
        reader(client)
        result = load_result(case_id, client)
        recs = service.recommendations(case_id, result)
        return {"schema": "anveshak.recommendation/v1", "case_id": case_id, "findings_hash": result.findings_hash, "recommendations": recs}

    @app.post("/v1/cases/{case_id}/recommendations/{recommendation_id}/status", status_code=201)
    def recommendation_status(case_id: str, recommendation_id: str, body: StatusUpdate, client: ApiClient = Depends(require("recommendation:status"))) -> dict:
        result = load_result(case_id, client)
        if result.findings.data_mode is DataMode.SYNTHETIC:
            raise HTTPException(409, "synthetic demo cases have no real outcomes; nothing is recorded for them")
        if not any(d.id == recommendation_id for d in result.findings.routing):
            raise HTTPException(404, "recommendation not found in this case")
        seq = service.store.add_status(case_id, recommendation_id, body.status.value, body.sahyog_request_id, body.note, body.reported_by, client.client_id)
        history = [x for x in service.store.statuses(case_id) if x["recommendation_id"] == recommendation_id]
        return {"recommendation_id": recommendation_id, "seq": seq, "status": body.status.value, "history": history}

    @app.get("/v1/directory/sahyog-intermediaries")
    def get_intermediaries(client: ApiClient = Depends(require("intel:read"))) -> dict:
        return service.intermediaries.load()

    @app.put("/v1/directory/sahyog-intermediaries")
    def put_intermediaries(body: IntermediaryUpload, client: ApiClient = Depends(require("directory:write"))) -> dict:
        doc = service.intermediaries.upload(body, service.directory, client.client_id)
        return {k: doc[k] for k in ("entities", "unmatched", "ambiguous", "count", "sha256", "uploaded_at")}

    @app.post("/v1/screen")
    def screen(body: ScreenIn, client: ApiClient = Depends(require("screen"))) -> dict:
        items = [(None, x) if isinstance(x, str) else (x.chain, x.address) for x in body.addresses]
        return {"results": service.screen(items, agency=scope(client)), "note": "labels and case database only — no chain data fetched"}

    # ------------------------------------------------------------------ cases

    @app.post("/v1/cases", status_code=202)
    def create_case(body: CaseCreate, client: ApiClient = Depends(require("case:create"))) -> dict:
        subjects = body.subjects
        max_hops = body.max_hops
        if body.mode is DataMode.SYNTHETIC:
            subjects = subjects or [Subject(**x) for x in demo.subjects()]
            max_hops = max(max_hops, demo.MAX_HOPS)
        elif body.mode is DataMode.REPLAY:
            raise HTTPException(400, "replay is run from the CLI: anveshak replay <case_id>")
        if not subjects:
            raise HTTPException(422, "at least one subject is required")
        if body.mode is DataMode.LIVE:
            needs_key = [x.chain.value for x in subjects if x.chain.family is ChainFamily.EVM and evm_backend(s, x.chain) == "etherscan" and not s.etherscan_api_key]
            if needs_key:
                raise HTTPException(400, f"ETHERSCAN_API_KEY is not configured — cannot trace {', '.join(sorted(set(needs_key)))}")
            needs_since = chains_needing_since(s, {x.chain for x in subjects})
            if needs_since and body.since is None:
                raise HTTPException(422, f"`since` (incident time) is required for {', '.join(sorted(c.value for c in needs_since))}: these chains are traced by a window-limited public RPC log scan")
        reference = body.case_reference or (demo.CASE_REFERENCE if body.mode is DataMode.SYNTHETIC else None)
        if not reference:
            raise HTTPException(422, "case_reference is required")
        request = CaseRequest(
            case_reference=reference, subjects=tuple(subjects), directions=tuple(body.directions), max_hops=max_hops,
            max_branch=body.max_branch, max_expansions=body.max_expansions, since=body.since, until=body.until,
            follow_all_assets=body.follow_all_assets, include_unverified_assets=body.include_unverified_assets,
            follow_cross_chain=body.follow_cross_chain, requested_by=body.requested_by,
        )
        agency = body.agency_id if client.sees_all_agencies else client.agency_id
        return {"case_id": service.submit(request, body.mode, agency_id=agency), "status": "queued"}

    @app.get("/v1/cases")
    def list_cases(limit: int = 50, client: ApiClient = Depends(require())) -> list[dict]:
        reader(client)
        return service.store.list(limit=min(max(limit, 1), 500), agency=scope(client))

    @app.get("/v1/cases/{case_id}")
    def get_case(case_id: str, client: ApiClient = Depends(require())) -> dict:
        reader(client)
        row = visible_case(case_id, client)
        keys = ("case_id", "case_reference", "data_mode", "status", "error", "findings_hash", "sahyog_reference", "agency_id", "parent_case_id", "created_at", "updated_at")
        out = {k: row.get(k) for k in keys}
        if row["status"] == "done":
            try:
                out["result"] = CaseResult.model_validate_json(row["result_json"]).model_dump(mode="json")
            except ValidationError:
                out["status"] = "legacy"
                out["error"] = "produced by an older engine version — re-run the case; the stored JSON and report remain unchanged on disk"
            out["approvals"] = service.store.approvals(case_id)
            out["callbacks"] = service.store.callbacks(case_id)
            out["recommendation_status"] = service.store.statuses(case_id)
        return out

    @app.post("/v1/cases/{case_id}/rerun", status_code=202)
    def rerun(case_id: str, client: ApiClient = Depends(require("case:create"))) -> dict:
        visible_case(case_id, client)
        return {"case_id": service.rerun(case_id), "parent_case_id": case_id, "status": "queued"}

    @app.get("/v1/cases/{case_id}/report", response_class=HTMLResponse)
    def get_report(case_id: str, client: ApiClient = Depends(require())) -> HTMLResponse:
        reader(client)
        result = load_result(case_id, client)
        path = s.reports_dir / f"{result.case_id}.html"
        if not path.exists():
            write_report(result, s.reports_dir)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/v1/cases/{case_id}/graph")
    def get_graph(case_id: str, client: ApiClient = Depends(require())) -> dict:
        reader(client)
        result = load_result(case_id, client)
        return build_graph(list(result.findings.traces), list(result.findings.crosschain_links))

    @app.get("/v1/cases/{case_id}/export/{fmt}")
    def export(case_id: str, fmt: str, client: ApiClient = Depends(require())) -> Response:
        reader(client)
        result = load_result(case_id, client)
        if fmt == "neo4j":
            return PlainTextResponse(to_cypher(result), headers={"Content-Disposition": f'attachment; filename="{case_id}.cypher"'})
        if fmt == "graphml":
            return Response(to_graphml(result), media_type="application/graphml+xml", headers={"Content-Disposition": f'attachment; filename="{case_id}.graphml"'})
        if fmt == "json":
            return Response(result.model_dump_json(indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{case_id}.json"'})
        raise HTTPException(404, "format must be neo4j, graphml or json")

    @app.get("/v1/cases/{case_id}/links")
    def case_links(case_id: str, client: ApiClient = Depends(require())) -> list[dict]:
        reader(client)
        row = visible_case(case_id, client)
        out = []
        for link in service.store.other_cases_for(case_id):
            if client.sees_all_agencies or link["other_agency_id"] == row.get("agency_id"):
                out.append({k: v for k, v in link.items() if k != "other_agency_id"})
            else:
                out.append({"chain": link["chain"], "address": link["address"], "role": link["role"], "other_role": link["other_role"],
                            "other_case_id": None, "other_reference": "case of another agency — contact the I4C coordinator for deconfliction"})
        return out

    @app.post("/v1/cases/{case_id}/routing/{decision_id}/approve")
    def approve(case_id: str, decision_id: str, body: Approval, client: ApiClient = Depends(require("approve"))) -> dict:
        if not s.standalone_approvals:
            raise HTTPException(403, "approvals happen in Sahyog: the officer approves and sends there, and Sahyog reports the outcome via "
                                     "POST /v1/cases/{id}/recommendations/{rid}/status. Set ANVESHAK_STANDALONE_APPROVALS=1 only for standalone pilots.")
        result = load_result(case_id, client)
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

    @app.get("/v1/detect/{address}")
    def detect(address: str, client: ApiClient = Depends(require("intel:read"))) -> dict:
        return {"address": address, "chains": [c.value for c in detect_chains(address)]}

    @app.get("/v1/addresses/{chain}/{address}")
    def lookup(chain: Chain, address: str, client: ApiClient = Depends(require("intel:read"))) -> dict:
        try:
            canonical = normalize(chain, address)
        except AddressError as exc:
            raise HTTPException(422, f"invalid {chain.value} address: {exc}") from exc
        attributor = Attributor(service.labels, aliases=service.directory.aliases, trust=service.trust)
        att = attributor.ownership(chain, canonical)
        risk = attributor.risk(chain, canonical)
        rows = service.store.raw(
            "SELECT s.case_id, s.role, c.agency_id FROM sightings s JOIN cases c ON c.case_id = s.case_id WHERE s.chain=? AND s.address=?", (chain.value, canonical)
        )
        own = [{"case_id": r["case_id"], "role": r["role"]} for r in rows if client.sees_all_agencies or r["agency_id"] == client.agency_id]
        return {
            "chain": chain.value,
            "address": canonical,
            "attribution": att.model_dump(mode="json") if att else None,
            "risk": risk.model_dump(mode="json") if risk else None,
            "seen_in_cases": own,
            "seen_in_other_agency_cases": len(rows) - len(own),
            "note": "label lookup only — no chain data fetched",
        }

    @app.post("/v1/attestations", status_code=201)
    def attest(body: AttestationIn, client: ApiClient = Depends(require("attest"))) -> dict:
        denies = body.polarity == "denies"
        try:
            label = attestation_label(
                body.chain, body.address, body.entity_id, body.entity_name, body.category, body.document_ref, body.as_of, body.source_class,
                denies=denies, reply_id=body.sahyog_reply_id,
            )
        except (AddressError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        path = s.var_dir / "labels" / "attestations.jsonl"
        existing = list(LabelStore.load([path]).all()) if path.exists() else []
        write_jsonl(path, [*existing, label])
        service.reload()
        effect = (
            f"future cases no longer attribute this address to {body.entity_name} (G-N1) unless a later confirmation supersedes this denial"
            if denies else "future cases grade this address A (G-A1) for this entity unless another source conflicts"
        )
        return {"label": label.model_dump(mode="json"), "effect": effect, "rerun": "POST /v1/cases/{id}/rerun to apply it to an existing case"}

    @app.get("/v1/alerts")
    def alerts(limit: int = 100, case_id: str | None = None, unacknowledged: bool = False, client: ApiClient = Depends(require("alerts:read"))) -> list[dict]:
        return service.store.alerts(limit=min(max(limit, 1), 1000), case_id=case_id, unacknowledged=unacknowledged, agency=scope(client))

    @app.post("/v1/alerts/{alert_id}/ack")
    def ack(alert_id: int, client: ApiClient = Depends(require("alerts:read"))) -> dict:
        alert = service.store.alert(alert_id)
        if alert is None or not (client.sees_all_agencies or alert["case_agency_id"] == client.agency_id):
            raise HTTPException(404, "alert not found")
        service.store.acknowledge(alert_id)
        return {"alert_id": alert_id, "acknowledged": True}

    @app.get("/v1/watchlist")
    def watchlist(client: ApiClient = Depends(require("watchlist"))) -> list[dict]:
        return service.store.watches(active_only=True, agency=scope(client))

    @app.post("/v1/watchlist", status_code=201)
    def add_watch(body: WatchIn, client: ApiClient = Depends(require("watchlist"))) -> dict:
        try:
            address = normalize(body.chain, body.address)
        except AddressError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.case_id:
            visible_case(body.case_id, client)
        watch_id = service.store.watch(body.chain.value, address, body.case_id, body.reason, client.agency_id)
        return {"watch_id": watch_id, "chain": body.chain.value, "address": address}

    @app.delete("/v1/watchlist/{watch_id}")
    def remove_watch(watch_id: int, client: ApiClient = Depends(require("watchlist"))) -> dict:
        watch = service.store.get_watch(watch_id)
        if watch is None or not (client.sees_all_agencies or watch["agency_id"] == client.agency_id):
            raise HTTPException(404, "watch not found")
        service.store.deactivate_watch(watch_id)
        return {"watch_id": watch_id, "active": False}

    @app.get("/v1/analytics")
    def analytics(client: ApiClient = Depends(require())) -> dict:
        reader(client)
        return service.analytics(agency=scope(client))

    # ------------------------------------------------------------------ oversight

    @app.get("/v1/audit")
    def audit_log(limit: int = 200, client_id: str | None = None, case_id: str | None = None, client: ApiClient = Depends(require("audit:read"))) -> list[dict]:
        return service.store.audit_entries(limit=min(max(limit, 1), 5000), client_id=client_id, case_id=case_id)

    @app.get("/v1/audit/verify")
    def audit_verify(client: ApiClient = Depends(require("audit:read"))) -> dict:
        return service.store.verify_audit()

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.exception_handler(ValidationError)
    def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors(include_url=False)})

    return app
