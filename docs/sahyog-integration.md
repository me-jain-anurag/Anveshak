# Sahyog integration guide

How the Sahyog portal (or any case-management system) talks to Anveshak. Interactive OpenAPI
docs are served at `/docs` by the running API.

## Authentication

Set `ANVESHAK_API_TOKEN` on the server. Every `/v1` route except `/v1/health` then requires:

```
X-API-Key: <token>
```

## 1. Report wallets

`POST /v1/sahyog/reports`

```json
{
  "sahyog_reference": "SAHYOG/2026/000123",
  "agency": "Cyber Crime PS, Bengaluru",
  "officer": "Inspector A. Kumar",
  "wallets": ["TXYZ…", "0xabc…", "bc1q…", "7xKX…"],
  "directions": ["out", "in"],
  "since": "2026-09-01T00:00:00+05:30",
  "max_hops": 5,
  "callback_url": "https://sahyog.example.gov.in/hooks/anveshak"
}
```

- The chain is detected from each wallet's format and checksum. For `0x` addresses, the EVM
  chains in `ANVESHAK_EVM_CHAINS` are probed for activity.
- Invalid inputs are **rejected with a reason**, never corrected.

`202 Accepted`

```json
{
  "case_id": "3f9c…",
  "accepted": [{"input": "TXYZ…", "chains": ["tron"]}, {"input": "0xabc…", "chains": ["ethereum", "polygon"], "notes": []}],
  "rejected": [{"input": "7xKX…", "reason": "not a valid address on any supported chain (format/checksum)"}],
  "subjects_traced": 3
}
```

`422` if no wallet in the report is traceable (the body lists the rejections).

## 2. Follow the case

`GET /v1/cases/{case_id}` → `status`: `queued` | `running` | `done` | `failed`.

When `done`, `result.findings` contains:

| Field | Meaning |
|---|---|
| `analyses[i].nearest_vasps` | ranked nearest VASPs per subject and direction: entity, hops, grade, confidence, deposit address |
| `analyses[i].confidences` | itemised confidence score per VASP / service endpoint |
| `subject_risks`, `analyses[i].flow_risks` | wallet and flow risk, itemised |
| `analyses[i].typologies`, `.clusters`, `.profiles`, `.alerts` | typologies, clusters, roles and tags, alerts |
| `crosschain_links`, `continuations` | cross-chain movements and the traces that followed them |
| `routing` | disclosure / freeze / issuer-freeze drafts with status, reasons and draft text |
| `traces`, `verifications`, `evidence_ids` | the underlying facts and their verification |

Other views of the same case: `GET /v1/cases/{id}/report` (HTML), `/graph`, `/export/neo4j`,
`/export/graphml`, `/export/json`, `/links` (other cases sharing addresses).

## 3. Callback (optional)

When a case finishes, Anveshak POSTs a summary to `callback_url`. It only does so if the URL uses
https **and** its host is listed in `ANVESHAK_CALLBACK_ALLOWLIST`.

```json
{
  "case_id": "3f9c…",
  "sahyog_reference": "SAHYOG/2026/000123",
  "case_reference": "SAHYOG/2026/000123 — Cyber Crime PS, Bengaluru",
  "findings_hash": "…",
  "nearest_vasps": [ … ],
  "routing": [ … drafts without draft_text … ]
}
```

Header `X-Anveshak-Signature: sha256=<hex>`: HMAC-SHA256 of the raw body, keyed with the API
token. Verify it before trusting the body:

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(TOKEN.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Anveshak-Signature"])
```

Delivery attempts are recorded and visible in `GET /v1/cases/{id}` → `callbacks`.

## 4. Officer approval and submission

`POST /v1/cases/{case_id}/routing/{decision_id}/approve`

```json
{"officer_name": "Inspector A. Kumar", "officer_id": "KA-CYB-0421", "note": "approved after review"}
```

- Only `ready_for_approval` drafts can be approved. Synthetic cases never can.
- The approved payload goes to the Sahyog gateway. Until I4C publishes its API, the dry-run gateway
  writes it (and its SHA-256) to `var/outbox/` for manual upload.

## 5. Feed VASP replies back

When a VASP confirms that an address is theirs, record it:

`POST /v1/attestations`

```json
{"chain": "tron", "address": "T…", "entity_id": "binance", "entity_name": "Binance",
 "document_ref": "Sahyog reply REF-123 dated 2026-10-02", "as_of": "2026-10-02"}
```

Future cases grade that address A (rule G-A1).

## 6. Alerts and watchlist

- `GET /v1/alerts?unacknowledged=true` and `POST /v1/alerts/{id}/ack`
- `GET /v1/watchlist`, `POST /v1/watchlist`, `DELETE /v1/watchlist/{id}`. Addresses still holding
  traced funds are added automatically. Movement raises a `critical` A-WATCH-MOVEMENT alert and
  queues a follow-up trace.
