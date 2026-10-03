"""Mock Sahyog portal — exercises Anveshak exactly the way the real portal is expected to.

Sahyog is the client (docs/sahyog-integration.md). This script plays its part:

  1. receive a case callback on a local HTTP endpoint and check its HMAC signature
  2. report wallets            POST /v1/sahyog/reports
  3. read recommendations      GET  /v1/cases/{id}/recommendations
  4. report outcomes           POST /v1/cases/{id}/recommendations/{rid}/status
  5. record the VASP's reply   POST /v1/attestations   (confirms / denies, with the reply id)
  6. re-run the case           POST /v1/cases/{id}/rerun

Usage (against a running server whose ANVESHAK_CALLBACK_ALLOWLIST contains 127.0.0.1):

    python tools/mock_sahyog/mock_sahyog.py --api http://127.0.0.1:8000 --key <sahyog client key> \\
        --wallet TXYZ... --reference SAHYOG/2026/0042 --agency-id DL-IFSO --since 2026-09-01T00:00:00Z

No real Sahyog data is used; the reply recorded in step 5 is the operator's choice
(--reply confirms|denies|none). The class `MockSahyog` is also used by the test suite.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx


class MockSahyog:
    def __init__(self, client: httpx.Client, key: str | None = None, callback_secret: str = ""):
        self.client = client
        self.headers = {"X-API-Key": key} if key else {}
        self.secret = callback_secret
        self.callbacks: queue.Queue[dict] = queue.Queue()

    # ---------------------------------------------------------------- calls

    def _call(self, method: str, path: str, **kw) -> dict:
        r = self.client.request(method, path, headers=self.headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def report(self, reference: str, wallets: list[str], agency_id: str | None = None, since: str | None = None, callback_url: str | None = None) -> dict:
        body = {"sahyog_reference": reference, "wallets": wallets, "agency_id": agency_id, "since": since, "callback_url": callback_url}
        return self._call("POST", "/v1/sahyog/reports", json={k: v for k, v in body.items() if v is not None})

    def wait(self, case_id: str, timeout: float = 600) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            case = self._call("GET", f"/v1/cases/{case_id}")
            if case["status"] in ("done", "failed"):
                return case
            time.sleep(0.2)
        raise TimeoutError(case_id)

    def recommendations(self, case_id: str) -> list[dict]:
        return self._call("GET", f"/v1/cases/{case_id}/recommendations")["recommendations"]

    def status(self, case_id: str, recommendation_id: str, status: str, sahyog_request_id: str | None = None, note: str | None = None) -> dict:
        body = {"status": status, "sahyog_request_id": sahyog_request_id, "note": note, "reported_by": "mock-sahyog"}
        return self._call("POST", f"/v1/cases/{case_id}/recommendations/{recommendation_id}/status", json=body)

    def reply(self, rec: dict, address: str, polarity: str, reply_id: str, as_of: str) -> dict:
        i = rec["intermediary"]
        body = {
            "chain": rec["subject"]["chain"], "address": address, "entity_id": i["entity_id"], "entity_name": i["display_name"],
            "document_ref": f"Sahyog reply {reply_id} dated {as_of} from {i['display_name']}", "as_of": as_of,
            "polarity": polarity, "sahyog_reply_id": reply_id,
        }
        return self._call("POST", "/v1/attestations", json=body)

    def rerun(self, case_id: str) -> str:
        return self._call("POST", f"/v1/cases/{case_id}/rerun")["case_id"]

    # ---------------------------------------------------------------- callbacks

    def verify_callback(self, raw: bytes, headers: dict) -> dict:
        """Check `X-Anveshak-Signature: sha256=<hmac>` over the exact body bytes."""
        if self.secret:
            expected = "sha256=" + hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
            got = {k.lower(): v for k, v in headers.items()}.get("x-anveshak-signature", "")
            if not hmac.compare_digest(expected, got):
                raise ValueError("callback signature does not match")
        body = json.loads(raw)
        self.callbacks.put(body)
        return body

    def serve_callbacks(self, port: int) -> HTTPServer:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    mock.verify_callback(raw, dict(self.headers))
                    self.send_response(200)
                except ValueError:
                    self.send_response(401)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--key", help="the Sahyog client's API key (omit for an open development server)")
    p.add_argument("--callback-secret", default="", help="ANVESHAK_CALLBACK_SECRET of the server")
    p.add_argument("--callback-port", type=int, default=8765)
    p.add_argument("--wallet", action="append", required=True)
    p.add_argument("--reference", default=f"MOCK-SAHYOG/{time.strftime('%Y%m%d-%H%M%S')}")
    p.add_argument("--agency-id")
    p.add_argument("--since")
    p.add_argument("--reply", choices=["confirms", "denies", "none"], default="none", help="simulate the VASP's reply to the first recommendation")
    args = p.parse_args()

    mock = MockSahyog(httpx.Client(base_url=args.api, timeout=60), args.key, args.callback_secret)
    server = mock.serve_callbacks(args.callback_port)
    callback = f"http://127.0.0.1:{args.callback_port}/anveshak-callback"
    sent = mock.report(args.reference, args.wallet, args.agency_id, args.since, callback)
    print(f"case {sent['case_id']} queued; accepted {sent['accepted']}; rejected {sent['rejected']}")
    case = mock.wait(sent["case_id"])
    print(f"case status: {case['status']} {case.get('error') or ''}")
    try:
        cb = mock.callbacks.get(timeout=30)
        print(f"callback received and verified: {len(cb['recommendations'])} recommendation(s), findings {cb['findings_hash'][:16]}…")
    except queue.Empty:
        print("no callback received (is 127.0.0.1 in ANVESHAK_CALLBACK_ALLOWLIST?)")
    recs = mock.recommendations(sent["case_id"])
    for r in recs:
        i = r["intermediary"]
        print(f"  {r['request_type']:13} {r['readiness']:18} {i['display_name']} (Sahyog id {i['sahyog_intermediary_id'] or '—'}) grade {r['attribution']['grade']} conf {r['attribution']['confidence']}")
    if recs and args.reply != "none":
        first = next((r for r in recs if r["request_type"] == "disclosure"), recs[0])
        mock.status(sent["case_id"], first["recommendation_id"], "submitted", sahyog_request_id="MOCK-REQ-1")
        mock.status(sent["case_id"], first["recommendation_id"], "responded", sahyog_request_id="MOCK-REQ-1")
        for address in first["accounts"]:
            mock.reply(first, address, args.reply, "MOCK-REPLY-1", time.strftime("%Y-%m-%d"))
        new_id = mock.rerun(sent["case_id"])
        mock.wait(new_id)
        for r in mock.recommendations(new_id):
            print(f"  after reply: {r['request_type']:13} {r['readiness']:18} {r['intermediary']['display_name']} grade {r['attribution']['grade']}")
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
