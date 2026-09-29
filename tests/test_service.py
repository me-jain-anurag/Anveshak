"""Service layer: queue, post-processing, watchlist monitor, cross-case links, ingestion, analytics."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from anveshak import demo
from anveshak.api import create_app
from anveshak.case import CaseRequest, DataMode, Engine, Subject
from anveshak.chain import Chain
from anveshak.chains.memory import MemorySource
from anveshak.service import SahyogReport, Service

from .conftest import xfer


@pytest.fixture
def service(settings):
    return Service(replace(settings, embedded_workers=0))


def test_queue_claims_each_case_once(service):
    ids = [service.submit(demo.request(), DataMode.SYNTHETIC) for _ in range(2)]
    first, second = service.store.claim_next("w1"), service.store.claim_next("w2")
    assert {first["case_id"], second["case_id"]} == set(ids)
    assert service.store.claim_next("w3") is None


def test_worker_runs_case_and_persists_alerts(service):
    case_id = service.submit(demo.request(), DataMode.SYNTHETIC)
    assert service.work_once("w") is True
    row = service.store.get(case_id)
    assert row["status"] == "done" and row["findings_hash"]
    rules = {a["rule"] for a in service.store.alerts(case_id=case_id)}
    assert {"A-FREEZE-OPPORTUNITY", "A-SANCTIONS"} <= rules
    assert service.store.watches() == []  # synthetic cases never feed the live watchlist


def test_watchlist_monitor_alerts_on_new_outgoing_transfer(service, registry, monkeypatch):
    usdt = registry.token(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT", 6)
    T = demo.tron_addr
    before = [xfer(usdt, T("x"), T("park"), 50_000_000, 0)]
    after = before + [xfer(usdt, T("park"), T("away"), 49_000_000, 10)]
    state = {"transfers": before}

    def fake_engine(mode):
        return Engine(DataMode.SYNTHETIC, service.labels, registry, service.directory, sources={Chain.TRON: MemorySource(Chain.TRON, state["transfers"])})

    monkeypatch.setattr(service, "engine", fake_engine)
    service.store.watch("tron", T("park"), None, "test")
    assert service.monitor_once() == []  # first pass sets the baseline
    state["transfers"] = after
    (alert_id,) = service.monitor_once()
    alert = next(a for a in service.store.alerts() if a["alert_id"] == alert_id)
    assert alert["rule"] == "A-WATCH-MOVEMENT" and alert["severity"] == "critical" and T("away") in alert["message"]
    assert service.store.list()[0]["case_reference"].startswith("watch / follow-up")  # follow-up case queued
    assert service.monitor_once() == []  # no duplicate alert for the same transfer


def test_cross_case_links_ignore_known_services(service):
    service.store.create("c1", "REF-1", "live", {})
    service.store.create("c2", "REF-2", "live", {})
    service.store.record_sightings("c1", [("tron", "TMULE", "path"), ("tron", "THOT", "vasp")])
    service.store.record_sightings("c2", [("tron", "TMULE", "subject"), ("tron", "THOT", "vasp")])
    links = service.store.other_cases_for("c2")
    assert {(l["address"], l["other_reference"]) for l in links} == {("TMULE", "REF-1"), ("THOT", "REF-1")}


def test_ingestion_detects_chains_and_rejects_invalid(service):
    report = SahyogReport(
        sahyog_reference="SAHYOG/2026/0001",
        wallets=["TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "not-a-wallet", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"],
    )
    out = service.ingest(report)
    assert out["case_id"] and out["subjects_traced"] == 3
    assert [a["chains"] for a in out["accepted"]] == [["tron"], ["bitcoin"], ["solana"]]
    assert len(out["rejected"]) == 2
    row = service.store.get(out["case_id"])
    assert row["sahyog_reference"] == "SAHYOG/2026/0001" and row["status"] == "queued"


def test_callback_refused_unless_allow_listed(service):
    result = demo.engine(service.registry, service.directory).run(demo.request())
    service.send_callback(result, "https://evil.example/hook", "S-1")
    service.send_callback(result, "http://sahyog.gov.in/hook", "S-1")
    assert [c["status"] for c in service.store.callbacks(result.case_id)] == ["refused", "refused"]


def test_analytics_summarises_cases(service):
    service.submit(demo.request(), DataMode.SYNTHETIC)
    service.work_once("w")
    a = service.analytics()
    assert a["cases"]["done"] == 1 and a["completed_by_mode"] == {"synthetic": 1}
    assert any(v["vasp"].startswith("Demo Exchange Alpha") for v in a["vasps_reached"])
    assert a["cross_chain_links"] == 1 and "peel_chain" in a["typologies"]


def test_api_new_endpoints(settings):
    client = TestClient(create_app(settings))
    case_id = client.post("/v1/cases", json={"mode": "synthetic"}).json()["case_id"]
    import time

    for _ in range(300):
        if client.get(f"/v1/cases/{case_id}").json()["status"] == "done":
            break
        time.sleep(0.1)
    assert client.get(f"/v1/cases/{case_id}/export/neo4j").text.startswith("// Anveshak case")
    assert "graphml" in client.get(f"/v1/cases/{case_id}/export/graphml").text
    assert client.get(f"/v1/cases/{case_id}/export/pdf").status_code == 404
    assert client.get("/v1/detect/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").json()["chains"] == ["tron"]
    assert client.get("/v1/analytics").json()["cases"]["done"] >= 1
    alerts = client.get("/v1/alerts", params={"case_id": case_id}).json()
    assert alerts and client.post(f"/v1/alerts/{alerts[0]['alert_id']}/ack").json()["acknowledged"]
    w = client.post("/v1/watchlist", json={"chain": "tron", "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "reason": "test watch"}).json()
    assert any(x["watch_id"] == w["watch_id"] for x in client.get("/v1/watchlist").json())
    assert client.delete(f"/v1/watchlist/{w['watch_id']}").json()["active"] is False
    bad = client.post("/v1/sahyog/reports", json={"sahyog_reference": "S-2", "wallets": ["nope"]})
    assert bad.status_code == 422


def test_old_database_is_migrated(tmp_path):
    import sqlite3

    from anveshak.storage import CaseStore

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cases (case_id TEXT PRIMARY KEY, case_reference TEXT NOT NULL, data_mode TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, findings_hash TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("INSERT INTO cases VALUES ('old', 'REF', 'live', 'done', '{}', NULL, NULL, NULL, 't', 't')")
    db.commit()
    db.close()
    store = CaseStore(path)
    store.create("new", "REF-2", "live", {}, sahyog_reference="S-9")
    assert store.get("new")["sahyog_reference"] == "S-9" and store.get("old")["case_reference"] == "REF"


def test_legacy_results_do_not_break_api(settings):
    client = TestClient(create_app(replace(settings, embedded_workers=0)))
    store = client.app.state.service.store
    store.create("legacy1", "OLD", "live", {})
    store.set_done("legacy1", json.dumps({"case_id": "legacy1", "findings": {"traces": []}}), "h")
    assert client.get("/v1/analytics").json()["legacy_cases_not_analysed"] == 1
    assert client.get("/v1/cases/legacy1").json()["status"] == "legacy"
    assert client.get("/v1/cases/legacy1/report").status_code == 409
