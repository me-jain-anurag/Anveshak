import json

import httpx
import pytest

from anveshak.errors import EvidenceCorrupted, EvidenceMissing, SourceError
from anveshak.evidence import EvidenceStore, LiveFetcher, ReplayFetcher, sha256_hex


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_live_fetch_records_exact_bytes_and_redacts_secret(tmp_path):
    body = b'{"ok": true, "n": 1}'
    store = EvidenceStore(tmp_path)
    fetcher = LiveFetcher(store, client=_client(lambda req: httpx.Response(200, content=body)))
    got = fetcher.get("https://api.example/x", params={"address": "abc", "apikey": "SUPER-SECRET"})
    assert got.evidence_id == sha256_hex(body)
    assert store.read(got.evidence_id) == body
    on_disk = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert "SUPER-SECRET" not in on_disk and "<redacted>" in on_disk


def test_replay_returns_recorded_response_without_network(tmp_path):
    store = EvidenceStore(tmp_path)
    LiveFetcher(store, client=_client(lambda req: httpx.Response(200, json={"v": 42}))).get("https://api.example/x", params={"apikey": "k1"})
    replay = ReplayFetcher(store)
    assert replay.get("https://api.example/x", params={"apikey": "different-key"}).data == {"v": 42}
    with pytest.raises(EvidenceMissing):
        replay.get("https://api.example/never-fetched")


def test_tampered_evidence_is_detected(tmp_path):
    store = EvidenceStore(tmp_path)
    got = LiveFetcher(store, client=_client(lambda req: httpx.Response(200, json={"v": 1}))).get("https://api.example/x")
    obj = next(p for p in tmp_path.rglob(got.evidence_id))
    obj.write_bytes(json.dumps({"v": 2}).encode())
    with pytest.raises(EvidenceCorrupted):
        ReplayFetcher(store).get("https://api.example/x")


def test_http_errors_raise_source_error(tmp_path, monkeypatch):
    monkeypatch.setattr("anveshak.evidence.time.sleep", lambda s: None)
    store = EvidenceStore(tmp_path)
    with pytest.raises(SourceError):
        LiveFetcher(store, client=_client(lambda req: httpx.Response(404))).get("https://api.example/x")
    with pytest.raises(SourceError, match="unavailable"):
        LiveFetcher(store, client=_client(lambda req: httpx.Response(503)), retries=1).get("https://api.example/x")
    with pytest.raises(SourceError, match="non-JSON"):
        LiveFetcher(store, client=_client(lambda req: httpx.Response(200, content=b"<html>"))).get("https://api.example/x")
