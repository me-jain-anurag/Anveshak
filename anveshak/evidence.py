"""Content-addressed evidence store and the HTTP fetchers that feed it (ADR-0004).

Every byte received from a blockchain data source is stored exactly as received, under its
sha256. Parsed records carry that hash (`Transfer.evidence_id`), so any fact in a report
can be traced back to the raw response it came from, and the integrity of that response
can be checked at any time (`EvidenceStore.read` re-hashes on every read).

A `ReplayFetcher` answers requests only from the store. Re-running a case in replay mode
must reproduce the findings hash exactly — this is tested.

Secrets (API keys, and credentials inside JSON-RPC endpoint URLs) are redacted before a request
is recorded; they never reach disk (ADR-0025).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .errors import EvidenceCorrupted, EvidenceMissing, SourceError

SECRET_PARAMS = frozenset({"apikey", "api_key", "key", "token", "access_token"})
SECRET_HEADERS = frozenset({"tron-pro-api-key", "authorization", "x-api-key", "api-key"})


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def redact_params(params: dict[str, Any] | None) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in SECRET_PARAMS else str(v)) for k, v in sorted((params or {}).items())}


_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]{20,}")


def _is_token(segment: str) -> bool:
    # Provider keys are long random strings: hex or mixed-case base62. Method names such as
    # TronGrid's `gettransactioninfobyid` are long too, but have neither digits nor mixed case.
    return bool(_TOKEN_CHARS.fullmatch(segment)) and (any(c.isdigit() for c in segment) or (segment.lower() != segment and segment.upper() != segment))


def redact_endpoint(url: str) -> str:
    """A POST endpoint URL with any credential in it masked.

    Hosted node providers put the key in the URL itself: in the path (Infura `/v3/<key>`,
    Alchemy `/v2/<key>`, QuickNode `/<token>/`), in userinfo or in a query parameter. Only
    POST endpoints are treated this way: a GET URL carries addresses and transaction ids in
    its path, which are not secrets and must stay distinct. URLs without credentials come
    back unchanged, so evidence recorded earlier keeps its request keys.
    """
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "<redacted>@" + netloc.rsplit("@", 1)[1]
    path = "/".join("<redacted>" if _is_token(s) else s for s in parts.path.split("/"))
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(k.lower() in SECRET_PARAMS for k, _ in pairs):
            query = urlencode([(k, "<redacted>" if k.lower() in SECRET_PARAMS else v) for k, v in pairs], safe="<>")
    return urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def host_of(url: str) -> str:
    """host[:port] without any userinfo, for messages and pacing."""
    return urlsplit(url).netloc.rsplit("@", 1)[-1]


def recorded_url(method: str, url: str) -> str:
    return redact_endpoint(url) if method == "POST" else url


def request_key(method: str, url: str, params: dict[str, Any] | None, body: Any) -> str:
    return sha256_hex(canonical_json({"method": method, "url": recorded_url(method, url), "params": redact_params(params), "body": body}))


@dataclass(frozen=True)
class Fetched:
    data: Any
    evidence_id: str
    status: int = 200  # only differs from 200 when the caller accepted that status (e.g. 404 = "not found")


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._objects = self.root / "objects"
        self._requests = self.root / "requests"
        self._lock = threading.Lock()

    def _object_path(self, evidence_id: str) -> Path:
        return self._objects / evidence_id[:2] / evidence_id

    def put(self, raw: bytes, request: dict[str, Any], status_code: int) -> str:
        evidence_id = sha256_hex(raw)
        path = self._object_path(evidence_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(raw)
            meta_path = path.with_suffix(".meta.json")
            if not meta_path.exists():
                meta = {
                    "evidence_id": evidence_id,
                    "request": request,
                    "status_code": status_code,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": len(raw),
                }
                meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
            self._requests.mkdir(parents=True, exist_ok=True)
            key = request["request_key"]
            (self._requests / f"{key}.json").write_text(
                json.dumps({"evidence_id": evidence_id, "request": request, "status_code": status_code}, indent=2, sort_keys=True)
            )
        return evidence_id

    def read(self, evidence_id: str) -> bytes:
        path = self._object_path(evidence_id)
        if not path.exists():
            raise EvidenceMissing(f"evidence object {evidence_id} not found")
        raw = path.read_bytes()
        if sha256_hex(raw) != evidence_id:
            raise EvidenceCorrupted(f"evidence object {evidence_id} fails its sha256 check")
        return raw

    def meta(self, evidence_id: str) -> dict[str, Any] | None:
        path = self._object_path(evidence_id).with_suffix(".meta.json")
        return json.loads(path.read_text()) if path.exists() else None

    def lookup(self, key: str) -> str | None:
        record = self.lookup_record(key)
        return record["evidence_id"] if record else None

    def lookup_record(self, key: str) -> dict[str, Any] | None:
        path = self._requests / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())


class Fetcher(Protocol):
    used: list[str]

    def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, accept: tuple[int, ...] = ()) -> Fetched: ...

    def post(self, url: str, body: Any, headers: dict[str, str] | None = None) -> Fetched: ...


class _Tracking:
    def __init__(self) -> None:
        self.used: list[str] = []
        self._seen: set[str] = set()

    def _track(self, evidence_id: str) -> None:
        if evidence_id not in self._seen:
            self._seen.add(evidence_id)
            self.used.append(evidence_id)


def _parse_json(raw: bytes, url: str, status: int = 200) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        if status != 200:
            return None  # an accepted error status (e.g. 404) with a non-JSON body
        raise SourceError(f"{host_of(url)} returned a non-JSON response") from exc


class LiveFetcher(_Tracking):
    """Fetches over HTTPS, records every response in the evidence store."""

    def __init__(
        self,
        store: EvidenceStore,
        client: httpx.Client | None = None,
        min_interval: dict[str, float] | None = None,
        retries: int = 4,
        timeout: float = 30.0,
    ):
        super().__init__()
        self.store = store
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "anveshak/0.1"})
        self.min_interval = min_interval or {}
        self.retries = retries
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def _throttle(self, host: str) -> None:
        interval = self.min_interval.get(host, 0.0)
        if not interval:
            return
        with self._lock:
            wait = self._last_call.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    def _request(self, method: str, url: str, params: dict | None, body: Any, headers: dict | None, accept: tuple[int, ...] = ()) -> Fetched:
        host = host_of(url)
        last_error: str = ""
        response: httpx.Response | None = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                response = self.client.request(method, url, params=params, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                response = None
            else:
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
                last_error = f"HTTP {response.status_code}"
            if attempt < self.retries:
                time.sleep(min(2**attempt, 16))
        if response is None or response.status_code in (429, 500, 502, 503, 504):
            raise SourceError(f"{host} unavailable after {self.retries + 1} attempts ({last_error})")
        if response.status_code >= 400 and response.status_code not in accept:
            raise SourceError(f"{host} returned HTTP {response.status_code}")
        raw = response.content
        data = _parse_json(raw, url, response.status_code)
        request = {
            "request_key": request_key(method, url, params, body),
            "method": method,
            "url": recorded_url(method, url),
            "params": redact_params(params),
            "body": body,
            "headers": sorted(k for k in (headers or {}) if k.lower() not in SECRET_HEADERS),
        }
        evidence_id = self.store.put(raw, request, response.status_code)
        self._track(evidence_id)
        return Fetched(data, evidence_id, response.status_code)

    def get(self, url, params=None, headers=None, accept=()) -> Fetched:
        return self._request("GET", url, params, None, headers, accept)

    def post(self, url, body, headers=None) -> Fetched:
        return self._request("POST", url, None, body, headers)


class ReplayFetcher(_Tracking):
    """Answers only from the evidence store. No network access at all."""

    def __init__(self, store: EvidenceStore):
        super().__init__()
        self.store = store

    def _replay(self, method: str, url: str, params: dict | None, body: Any, accept: tuple[int, ...] = ()) -> Fetched:
        key = request_key(method, url, params, body)
        record = self.store.lookup_record(key)
        if record is None:
            raise EvidenceMissing(f"no recorded response for {method} {recorded_url(method, url)} {redact_params(params)}")
        evidence_id = record["evidence_id"]
        status = int(record.get("status_code", 200))
        if status >= 400 and status not in accept:
            raise SourceError(f"{host_of(url)} returned HTTP {status} (recorded)")
        raw = self.store.read(evidence_id)
        self._track(evidence_id)
        return Fetched(_parse_json(raw, url, status), evidence_id, status)

    def get(self, url, params=None, headers=None, accept=()) -> Fetched:
        return self._replay("GET", url, params, None, accept)

    def post(self, url, body, headers=None) -> Fetched:
        return self._replay("POST", url, None, body)
