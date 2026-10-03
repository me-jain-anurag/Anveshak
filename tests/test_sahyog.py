"""Sahyog as the client, end to end (ADR-0019), and per-client access control (ADR-0023).

The mock Sahyog portal (tools/mock_sahyog) drives the API in-process: report → case runs on
(mocked) live Tron data → signed callback with recommendations → intermediary ids → outcome
statuses → the VASP's reply recorded as an attestation → re-run → grade A, ready.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from anveshak.api import create_app
from anveshak.chain import Chain
from anveshak.clients import key_hash
from anveshak.domain import Category, SourceClass
from anveshak.evidence import EvidenceStore, LiveFetcher
from anveshak.labels.store import write_jsonl
from anveshak.service import Service
from tools.mock_sahyog.mock_sahyog import MockSahyog

from .conftest import label
from .test_adapters import HOT, S, TronFake

KEYS = {"sahyog": "k-sahyog", "inv-a": "k-inv-a", "inv-b": "k-inv-b", "auditor": "k-audit", "tiny": "k-tiny", "fenced": "k-fenced"}
SECRET = "callback-secret"


@pytest.fixture
def env(settings, monkeypatch, tmp_path):
    clients = {"clients": [
        {"client_id": "sahyog", "key_sha256": key_hash(KEYS["sahyog"]), "roles": ["sahyog"], "rate_limit_per_minute": 10_000},
        {"client_id": "inv-a", "key_sha256": key_hash(KEYS["inv-a"]), "roles": ["investigator"], "agency_id": "AG-A", "rate_limit_per_minute": 10_000},
        {"client_id": "inv-b", "key_sha256": key_hash(KEYS["inv-b"]), "roles": ["investigator"], "agency_id": "AG-B", "rate_limit_per_minute": 10_000},
        {"client_id": "auditor", "key_sha256": key_hash(KEYS["auditor"]), "roles": ["auditor"], "rate_limit_per_minute": 10_000},
        {"client_id": "tiny", "key_sha256": key_hash(KEYS["tiny"]), "roles": ["investigator"], "agency_id": "AG-A", "rate_limit_per_minute": 3},
        {"client_id": "fenced", "key_sha256": key_hash(KEYS["fenced"]), "roles": ["auditor"], "ip_allowlist": ["10.0.0.0/8"]},
    ]}
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(clients), encoding="utf-8")
    s = replace(settings, api_clients_file=path, callback_allowlist=("sahyog.test",), callback_secret=SECRET, embedded_workers=1, max_body_bytes=4096)
    # a weak (grade C) label for the exchange hot wallet the funds reach
    write_jsonl(s.var_dir / "labels" / "weak.jsonl", [label(Chain.TRON, HOT, SourceClass.WEAK, "https://forum.example/thread", entity="binance", category=Category.EXCHANGE)])
    monkeypatch.setattr(Service, "_live_fetcher", lambda self: LiveFetcher(EvidenceStore(self.settings.evidence_dir), client=httpx.Client(transport=httpx.MockTransport(TronFake()))))
    sent: list[tuple[str, bytes, dict]] = []
    monkeypatch.setattr("anveshak.service.httpx.post", lambda url, content, headers, timeout: sent.append((url, content, headers)) or httpx.Response(200))
    client = TestClient(create_app(s))
    yield client, sent
    client.app.state.stop.set()


def as_(client: TestClient, who: str) -> dict:
    return {"X-API-Key": KEYS[who]}


def test_sahyog_end_to_end(env):
    client, sent = env
    mock = MockSahyog(client, KEYS["sahyog"], SECRET)
    out = mock.report("SAHYOG/2026/0042", [S, "not-a-wallet"], agency_id="AG-A", callback_url="https://hooks.sahyog.test/anveshak")
    assert out["rejected"][0]["input"] == "not-a-wallet"
    case = mock.wait(out["case_id"], timeout=60)
    assert case["status"] == "done", case.get("error")

    # 1. the signed callback carries the recommendations
    url, raw, headers = sent[0]
    assert url == "https://hooks.sahyog.test/anveshak"
    cb = mock.verify_callback(raw, headers)
    with pytest.raises(ValueError):
        mock.verify_callback(raw.replace(b"SAHYOG", b"SAHY0G"), headers)
    rec = next(r for r in cb["recommendations"] if r["request_type"] == "disclosure")
    assert rec["schema"] == "anveshak.recommendation/v1" and rec["intermediary"]["entity_id"] == "binance"
    assert rec["readiness"] == "analyst_review" and rec["attribution"]["grade"] == "C"
    assert rec["intermediary"]["sahyog_intermediary_id"] is None and rec["alternative_channels"]  # Binance's LE portal

    # 2. Sahyog uploads its intermediary list: exact matches only
    up = client.put("/v1/directory/sahyog-intermediaries", headers=as_(client, "sahyog"), json={"intermediaries": [
        {"sahyog_intermediary_id": "SAH-INT-0007", "name": "binance"}, {"sahyog_intermediary_id": "SAH-INT-0099", "name": "Binanse"}]}).json()
    assert up["entities"] == {"binance": "SAH-INT-0007"} and [u["name"] for u in up["unmatched"]] == ["Binanse"]
    rec = next(r for r in mock.recommendations(out["case_id"]) if r["request_type"] == "disclosure")
    assert rec["intermediary"]["sahyog_intermediary_id"] == "SAH-INT-0007" and rec["intermediary"]["on_sahyog"]

    # 3. outcomes reported back, append-only
    mock.status(out["case_id"], rec["recommendation_id"], "submitted", "SR-1")
    hist = mock.status(out["case_id"], rec["recommendation_id"], "responded", "SR-1")["history"]
    assert [h["status"] for h in hist] == ["submitted", "responded"]
    assert client.post(f"/v1/cases/{out['case_id']}/recommendations/nope/status", headers=as_(client, "sahyog"), json={"status": "submitted"}).status_code == 404
    store = client.app.state.service.store
    with pytest.raises(sqlite3.DatabaseError):
        store._db.execute("UPDATE recommendation_status SET status='complied'")
    assert client.get("/v1/analytics", headers=as_(client, "sahyog")).json()["recommendation_outcomes"] == {"responded": 1}

    # 4. the VASP's reply confirms its hot wallet → re-run → grade A, ready for approval in Sahyog
    reply = mock.reply(rec, HOT, "confirms", "RPLY-77", date.today().isoformat())
    assert reply["label"]["dataset_ref"] == "sahyog-reply:RPLY-77"
    new_id = mock.rerun(out["case_id"])
    new = mock.wait(new_id, timeout=60)
    assert new["parent_case_id"] == out["case_id"] and new["agency_id"] == "AG-A"
    rec2 = next(r for r in mock.recommendations(new_id) if r["request_type"] == "disclosure")
    assert rec2["attribution"]["grade"] == "A" and "G-A1" in rec2["attribution"]["rules"]
    assert rec2["readiness"] == "ready_for_approval"
    assert client.post(f"/v1/cases/{new_id}/routing/{rec2['recommendation_id']}/approve", headers=as_(client, "sahyog"),
                       json={"officer_name": "X Officer", "officer_id": "X-1"}).status_code == 403  # no approve role, and approvals live in Sahyog

    # 5. agency scoping
    assert client.get(f"/v1/cases/{new_id}", headers=as_(client, "inv-a")).status_code == 200
    assert client.get(f"/v1/cases/{new_id}", headers=as_(client, "inv-b")).status_code == 404
    assert [c["case_id"] for c in client.get("/v1/cases", headers=as_(client, "inv-b")).json()] == []

    # 6. the audit log saw all of it, and is intact
    audit = client.get("/v1/audit", headers=as_(client, "auditor"), params={"case_id": out["case_id"]}).json()
    assert {"sahyog"} <= {a["client_id"] for a in audit}
    assert client.get("/v1/audit/verify", headers=as_(client, "auditor")).json()["intact"] is True
    assert client.get("/v1/audit", headers=as_(client, "inv-a")).status_code == 403
    with pytest.raises(sqlite3.DatabaseError):
        store._db.execute("DELETE FROM audit_log")


def test_denial_reply_removes_attribution(env):
    client, _ = env
    mock = MockSahyog(client, KEYS["sahyog"], SECRET)
    out = mock.report("SAHYOG/2026/0043", [S], agency_id="AG-A")
    mock.wait(out["case_id"], timeout=60)
    rec = next(r for r in mock.recommendations(out["case_id"]) if r["request_type"] == "disclosure")
    reply = mock.reply(rec, HOT, "denies", "RPLY-78", date.today().isoformat())
    assert "G-N1" in reply["effect"]
    new_id = mock.rerun(out["case_id"])
    mock.wait(new_id, timeout=60)
    assert not [r for r in mock.recommendations(new_id) if r["intermediary"]["entity_id"] == "binance"]


def test_cross_agency_sightings_are_anonymised(env):
    client, _ = env
    mock = MockSahyog(client, KEYS["sahyog"], SECRET)
    a = mock.report("SAHYOG/A", [S], agency_id="AG-A")
    mock.wait(a["case_id"], timeout=60)
    b = mock.report("SAHYOG/B", [S], agency_id="AG-B")
    mock.wait(b["case_id"], timeout=60)
    alerts = client.get("/v1/alerts", headers=as_(client, "inv-b"), params={"case_id": b["case_id"]}).json()
    cross = [x for x in alerts if x["rule"] == "A-CROSS-CASE"]
    assert cross and all("another agency" in x["message"] and "SAHYOG/A" not in x["message"] for x in cross)
    links = client.get(f"/v1/cases/{b['case_id']}/links", headers=as_(client, "inv-b")).json()
    assert links and all(l["other_case_id"] is None for l in links)
    full = client.get(f"/v1/cases/{b['case_id']}/links", headers=as_(client, "sahyog")).json()
    assert {l["other_case_id"] for l in full} == {a["case_id"]}
    seen = client.get(f"/v1/addresses/tron/{S}", headers=as_(client, "inv-b")).json()
    assert len(seen["seen_in_cases"]) == 1 and seen["seen_in_other_agency_cases"] == 1


def test_screen_is_synchronous_and_offline(env):
    client, _ = env
    r = client.post("/v1/screen", headers=as_(client, "inv-a"), json={"addresses": [
        "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg", "not-an-address", {"chain": "tron", "address": HOT}]}).json()
    by = {x["input"]: x for x in r["results"]}
    assert by["TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg"]["risk_flags"] == ["sanctioned"]
    assert by["not-an-address"]["valid"] is False
    assert by[HOT]["attribution"]["entity_id"] == "binance" and by[HOT]["attribution"]["grade"] == "C"
    assert "no chain data" in r["note"]


def test_client_limits(env):
    client, _ = env
    assert client.get("/v1/meta").status_code == 401
    assert client.get("/v1/meta", headers={"X-API-Key": "wrong"}).status_code == 401
    meta = client.get("/v1/meta", headers=as_(client, "inv-a")).json()
    assert meta["client"]["agency_id"] == "AG-A" and meta["auth_mode"] == "per-client keys"
    assert client.get("/v1/meta", headers=as_(client, "fenced")).status_code == 403  # not from 10.0.0.0/8
    codes = [client.get("/v1/meta", headers=as_(client, "tiny")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    big = client.post("/v1/screen", headers=as_(client, "inv-a"), json={"addresses": ["T" * 150] * 60})
    assert big.status_code == 413
    assert client.post("/v1/attestations", headers=as_(client, "inv-a"), json={}).status_code == 403  # investigators cannot record replies
    assert client.put("/v1/directory/sahyog-intermediaries", headers=as_(client, "inv-a"), json={"intermediaries": []}).status_code == 403


def test_scoped_client_without_agency_is_rejected():
    from anveshak.clients import ApiClient

    with pytest.raises(ValueError, match="agency"):
        ApiClient(client_id="x1", key_sha256="0" * 64, roles=("investigator",))
