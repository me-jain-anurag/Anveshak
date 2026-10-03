"""Routing recommendations for the Sahyog portal — schema `anveshak.recommendation/v1` (ADR-0019).

Sahyog is the client: it sends reported wallets in, and gets back, per case, one
recommendation per (target intermediary, request type). A recommendation is the routing
decision of the case plus what Sahyog needs to act on it:

  * `intermediary.sahyog_intermediary_id` — Sahyog's own id for the intermediary, when the
    Sahyog operator has uploaded its intermediary list (`PUT /v1/directory/sahyog-intermediaries`)
    and the entity matched it *exactly* (directory entity id, display name or alias,
    case-insensitive). No fuzzy matching: a wrong recipient is worse than none.
  * `alternative_channels` — verified contact channels outside Sahyog (e.g. the VASP's own
    law-enforcement portal) for intermediaries not onboarded on Sahyog.
  * `status` / `status_history` — what happened after the recommendation, reported back by
    Sahyog (`POST /v1/cases/{id}/recommendations/{rid}/status`), append-only.

Recommendations contain no personal data: only addresses, transactions, the evidence basis
and the draft request text. The officer approves and sends inside Sahyog.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from .case import CaseResult
from .directory import VaspDirectory
from .evidence import canonical_json, sha256_hex

SCHEMA = "anveshak.recommendation/v1"


class OutcomeStatus(StrEnum):
    """What Sahyog reports back about a recommendation (append-only history)."""

    SUBMITTED = "submitted"  # the officer approved and Sahyog sent the request
    ACKNOWLEDGED = "acknowledged"  # the intermediary acknowledged receipt
    RESPONDED = "responded"  # a reply was received (record its content with /v1/attestations)
    COMPLIED = "complied"
    PARTIALLY_COMPLIED = "partially_complied"
    DECLINED = "declined"
    FUNDS_FROZEN = "funds_frozen"
    NOT_PURSUED = "not_pursued"  # the officer decided not to send it
    CLOSED = "closed"


class StatusUpdate(BaseModel):
    status: OutcomeStatus
    sahyog_request_id: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)
    reported_by: str | None = Field(default=None, max_length=200)


class SahyogIntermediary(BaseModel):
    sahyog_intermediary_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    entity_id: str | None = Field(default=None, max_length=60)  # optional explicit mapping to a directory entity


class IntermediaryUpload(BaseModel):
    intermediaries: list[SahyogIntermediary] = Field(max_length=5000)


class IntermediaryMap:
    """Sahyog intermediary ids by directory entity id, from the operator's upload (var/)."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {"entities": {}, "unmatched": [], "uploaded_at": None, "sha256": None}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def ids(self) -> dict[str, str]:
        return self.load()["entities"]

    def upload(self, body: IntermediaryUpload, directory: VaspDirectory, uploaded_by: str) -> dict:
        names: dict[str, str] = {}
        for e in directory.entries():
            for n in (e.entity_id, e.display_name, *e.aliases):
                names[n.strip().lower()] = e.entity_id
        entities: dict[str, str] = {}
        unmatched: list[dict] = []
        ambiguous: list[dict] = []
        for item in body.intermediaries:
            if item.entity_id:
                entity = directory.get(item.entity_id)
                target = entity.entity_id if entity else None
            else:
                target = names.get(item.name.strip().lower())
            if target is None:
                unmatched.append(item.model_dump())
            elif target in entities and entities[target] != item.sahyog_intermediary_id:
                ambiguous.append({**item.model_dump(), "entity_id": target, "already_mapped_to": entities[target]})
            else:
                entities[target] = item.sahyog_intermediary_id
        raw = canonical_json(body.model_dump(mode="json"))
        doc = {
            "entities": entities,
            "unmatched": unmatched,
            "ambiguous": ambiguous,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "uploaded_by": uploaded_by,
            "sha256": sha256_hex(raw),
            "count": len(body.intermediaries),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
        return doc


def build_recommendations(
    result: CaseResult,
    directory: VaspDirectory,
    intermediary_ids: dict[str, str],
    statuses: list[dict],
    sahyog_reference: str | None = None,
) -> list[dict]:
    f = result.findings
    history: dict[str, list[dict]] = {}
    for s in statuses:
        history.setdefault(s["recommendation_id"], []).append(
            {k: s[k] for k in ("status", "sahyog_request_id", "note", "reported_by", "client_id", "recorded_at")}
        )
    out = []
    for d in f.routing:
        entry = directory.get(d.target_entity_id) if d.target_entity_id else None
        sahyog_id = intermediary_ids.get(entry.entity_id) if entry else None
        hist = history.get(d.id, [])
        out.append(
            {
                "schema": SCHEMA,
                "recommendation_id": d.id,
                "case_id": result.case_id,
                "case_reference": f.request.case_reference,
                "sahyog_reference": sahyog_reference,
                "data_mode": f.data_mode.value,
                "findings_hash": result.findings_hash,
                "request_type": d.request_type.value,
                "readiness": d.status.value,
                "reasons": list(d.reasons),
                "intermediary": {
                    "entity_id": d.target_entity_id,
                    "display_name": d.target_name,
                    "role": d.target_role,
                    "sahyog_intermediary_id": sahyog_id,
                    "on_sahyog": sahyog_id is not None,
                    "jurisdiction": d.jurisdiction,
                },
                "alternative_channels": [c.model_dump(mode="json") for c in d.channels],
                "subject": {"chain": d.chain.value, "address": d.subject, "direction": d.direction.value},
                "accounts": list(d.addresses),
                "transactions": [t.model_dump(mode="json") for t in d.transactions],
                "attribution": {"grade": d.grade.value if d.grade else None, "rules": list(d.attribution_rules), "confidence": d.confidence, "confidence_band": d.confidence_band},
                "via": d.via,
                "draft_text": d.draft_text,
                "report_path": f"/v1/cases/{result.case_id}/report",
                "status": hist[-1]["status"] if hist else None,
                "status_history": hist,
            }
        )
    return out
