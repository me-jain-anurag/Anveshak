import time

import pytest
from fastapi.testclient import TestClient

from anveshak.api import create_app


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def _wait(client, case_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/cases/{case_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("case did not finish")


def test_synthetic_case_lifecycle(client):
    case_id = client.post("/v1/cases", json={"mode": "synthetic"}).json()["case_id"]
    body = _wait(client, case_id)
    assert body["status"] == "done", body.get("error")
    assert body["result"]["findings"]["data_mode"] == "synthetic"
    report = client.get(f"/v1/cases/{case_id}/report")
    assert report.status_code == 200 and "SYNTHETIC — NOT EVIDENCE" in report.text
    graph = client.get(f"/v1/cases/{case_id}/graph").json()
    assert graph["nodes"] and graph["edges"]
    ready = next(d for d in body["result"]["findings"]["routing"] if d["status"] == "ready_for_approval")
    r = client.post(f"/v1/cases/{case_id}/routing/{ready['id']}/approve", json={"officer_name": "Test Officer", "officer_id": "T-1"})
    assert r.status_code == 409 and "synthetic" in r.json()["detail"]


def test_live_case_validation(client):
    r = client.post("/v1/cases", json={"mode": "live", "case_reference": "x", "subjects": [{"chain": "tron", "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"}]})
    assert r.status_code == 422  # bad checksum rejected before anything runs
    assert client.post("/v1/cases", json={"mode": "live", "case_reference": "x", "subjects": []}).status_code == 422


def test_address_lookup_and_attestation_feedback_loop(client):
    addr = "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg"
    before = client.get(f"/v1/addresses/tron/{addr}").json()
    assert before["attribution"] is None
    assert before["risk"]["flags"] == ["sanctioned"]  # shipped OFAC label
    r = client.post("/v1/attestations", json={"chain": "tron", "address": addr, "entity_id": "binance", "entity_name": "Binance",
                                              "document_ref": "Sahyog reply TEST-1 dated 2026-10-01", "as_of": "2026-10-01"})
    assert r.status_code == 201
    after = client.get(f"/v1/addresses/tron/{addr}").json()
    assert after["attribution"]["grade"] == "A" and after["attribution"]["rule"] == "G-A1"
    assert client.get("/v1/addresses/tron/not-an-address").status_code == 422


def test_api_token_enforced(settings):
    from dataclasses import replace

    c = TestClient(create_app(replace(settings, api_token="s3cret")))
    assert c.get("/v1/meta").status_code == 401
    assert c.get("/v1/meta", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert c.get("/v1/health").status_code == 200
