"""Hand-off of officer-approved requests to the Sahyog portal (ADR-0010).

I4C has not published a public API specification for Sahyog. Until it is available, the
only implementation is `DryRunSahyogGateway`, which writes the exact payload that would be
submitted to an outbox folder, hashed, for the officer to upload manually. Replace it with a
real client behind the same `submit` signature once the specification is obtained.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .evidence import canonical_json, sha256_hex
from .routing import RoutingDecision


class SahyogGateway(Protocol):
    def submit(self, case_id: str, case_reference: str, findings_hash: str, decision: RoutingDecision, officer: dict) -> dict: ...


class DryRunSahyogGateway:
    def __init__(self, outbox: Path):
        self.outbox = Path(outbox)

    def submit(self, case_id: str, case_reference: str, findings_hash: str, decision: RoutingDecision, officer: dict) -> dict:
        payload = {
            "schema": "anveshak.sahyog-request.v0 (draft — pending I4C specification)",
            "case_id": case_id,
            "case_reference": case_reference,
            "findings_hash": findings_hash,
            "approved_by": officer,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision.model_dump(mode="json"),
        }
        raw = canonical_json(payload)
        digest = sha256_hex(raw)
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"{case_id}_{decision.id}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"mode": "dry-run", "path": str(path), "payload_sha256": digest, "submitted": False}
