# Sahyog integration guide

**Sahyog is the client.** The Sahyog portal (or an agency's case system) calls Anveshak:

1. It reports wallets.
2. It receives routing recommendations.
3. The officer approves and sends the request **inside Sahyog**.
4. Sahyog reports back what happened and records the intermediary's reply.

Anveshak never sends anything to an intermediary (ADR-0019). Interactive OpenAPI docs are at
`/docs` on the running API. A runnable mock of the Sahyog side is in
`tools/mock_sahyog/mock_sahyog.py`.

```
Sahyog ──POST /v1/sahyog/reports──────────────────▶ Anveshak  (queue, trace, attribute)
Sahyog ◀──signed callback (recommendations)──────── Anveshak
Sahyog ──GET  /v1/cases/{id}/recommendations──────▶
Sahyog ──POST …/recommendations/{rid}/status──────▶ (submitted, acknowledged, responded, …)
Sahyog ──POST /v1/attestations (confirms/denies)──▶ (VASP reply → label, G-A1 / G-N1)
Sahyog ──POST /v1/cases/{id}/rerun────────────────▶ (same request, with the new evidence)
```

## Authentication and scope (ADR-0023)

Each caller has its own key, sent as `X-API-Key: <key>`.

* **Creating keys:** `anveshak clients add --client-id sahyog-portal --role sahyog --ip 10.20.0.0/16`
  prints the key once and stores only its sha256 in `var/api_clients.yaml`
  (`ANVESHAK_API_CLIENTS_FILE`).
* **Sahyog client** (role `sahyog`): sees every agency's cases. It passes `agency_id` in each
  report, and that agency id scopes who else can read the case.
* **Agency dashboards** (roles `investigator` / `supervisor`, with `agency_id`): see only their
  own agency's cases. Matches in another agency's cases appear only as anonymised counts
  ("contact the I4C coordinator").
* **Auditors** (role `auditor`): read everything, plus `GET /v1/audit` and `/v1/audit/verify`.
  The audit log is append-only and hash-chained.
* **Limits:** per-client rate limit (429), per-client IP allowlist (403), and a request-body
  cap of `ANVESHAK_MAX_BODY_BYTES` (413).

**Transport security belongs to the reverse proxy.** Terminate TLS there and authenticate the
Sahyog link with mutual TLS or OAuth 2.0 client credentials. Example (nginx):

```nginx
server {
  listen 443 ssl;
  ssl_certificate      /etc/anveshak/tls/server.crt;
  ssl_certificate_key  /etc/anveshak/tls/server.key;
  ssl_client_certificate /etc/anveshak/tls/sahyog-ca.crt;   # mutual TLS for the Sahyog link
  ssl_verify_client on;
  client_max_body_size 1m;
  limit_req zone=anveshak burst=50;
  location / { proxy_pass http://127.0.0.1:8000; proxy_set_header X-Forwarded-For $remote_addr; }
}
```

## 1. Report wallets

`POST /v1/sahyog/reports`

```json
{
  "sahyog_reference": "SAHYOG/2026/000123",
  "agency": "Cyber Crime PS, Bengaluru",
  "agency_id": "KA-CYB-BLR",
  "officer": "Inspector A. Kumar",
  "wallets": ["TXYZ…", "0xabc…", "bc1q…"],
  "directions": ["out", "in"],
  "since": "2026-09-01T00:00:00+05:30",
  "max_hops": 5,
  "callback_url": "https://sahyog.example.gov.in/hooks/anveshak"
}
```

* The chain is detected from each wallet's format and checksum. For `0x` addresses, the EVM
  chains in `ANVESHAK_EVM_CHAINS` are probed for activity.
* Invalid inputs are **rejected with a reason**, never corrected.
* `since` (the incident time) is **required** for EVM chains traced by RPC log scan (BNB Chain
  without a paid Etherscan plan, ADR-0021). Without it those chains are skipped, with a note.

The response is `202` with `case_id`, `accepted`, `rejected` and `subjects_traced`. If no wallet
is traceable, the response is `422`.

## 2. Screen addresses instantly (optional)

`POST /v1/screen` with `{"addresses": ["T…", {"chain": "ethereum", "address": "0x…"}]}`.

* Per address and chain it returns: attribution (entity, grade, rule), risk flags, cases of
  your own agency, and a count of other agencies' cases.
* It uses labels and the case database only. No chain call is made, so it answers at once.

## 3. Receive recommendations

When a case finishes, Anveshak POSTs to `callback_url`. Two conditions apply:

* the host must be listed in `ANVESHAK_CALLBACK_ALLOWLIST`;
* the URL must use https. Plain http is accepted only for a loopback test receiver.

```json
{
  "schema": "anveshak.case-callback/v1",
  "case_id": "3f9c…",
  "sahyog_reference": "SAHYOG/2026/000123",
  "case_reference": "SAHYOG/2026/000123 — Cyber Crime PS, Bengaluru",
  "findings_hash": "…",
  "nearest_vasps": [ … ],
  "recommendations": [ …anveshak.recommendation/v1… ]
}
```

The header `X-Anveshak-Signature: sha256=<hex>` is an HMAC-SHA256 of the raw body, keyed with
`ANVESHAK_CALLBACK_SECRET`. Verify it before trusting the body:

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, request.headers["X-Anveshak-Signature"])
```

The same list is available at any time from `GET /v1/cases/{id}/recommendations`.

### Recommendation schema `anveshak.recommendation/v1`

| Field | Meaning |
|---|---|
| `recommendation_id` | stable id (the routing decision id) |
| `request_type` | `disclosure` (KYC / records), `freeze` (account freeze), `issuer_freeze` (token-level, e.g. Tether) |
| `readiness` | `ready_for_approval`, `analyst_review` or `blocked`; `reasons` says why |
| `intermediary.entity_id / display_name / role / jurisdiction` | who the request is for |
| `intermediary.sahyog_intermediary_id`, `on_sahyog` | Sahyog's own id for the intermediary, if it is on Sahyog (see §4) |
| `alternative_channels[]` | verified channels outside Sahyog (name, URL, source, date checked) |
| `subject` | chain, address and direction traced |
| `accounts[]` | the intermediary's addresses (incl. deposit addresses) on the path |
| `transactions[]` | the transactions to cite, with explorer links |
| `attribution` | grade (A/B/C/X), rules, confidence and band (rubric points, not probabilities) |
| `via` | how value reached this subject across chains, if it did |
| `draft_text` | the request text for the officer to review |
| `status`, `status_history[]` | outcomes reported back (§5) |
| `data_mode`, `findings_hash`, `report_path` | provenance of the findings |

No personal data appears in a recommendation.

## 4. Map Sahyog intermediary ids

`PUT /v1/directory/sahyog-intermediaries`

```json
{"intermediaries": [
  {"sahyog_intermediary_id": "SAH-INT-0007", "name": "Binance"},
  {"sahyog_intermediary_id": "SAH-INT-0042", "name": "WazirX", "entity_id": "wazirx"}
]}
```

* Matching is **exact only**: directory entity id, display name or alias, ignoring case, or an
  explicit `entity_id`.
* The response lists the `entities` mapped, plus anything `unmatched` or `ambiguous`. Those are
  never guessed: fix the name or pass `entity_id`.
* The mapping is stored under `var/` with its sha256 and uploader, and is visible in
  `GET /v1/meta` → `directory`.

## 5. Report outcomes

`POST /v1/cases/{id}/recommendations/{recommendation_id}/status`

```json
{"status": "submitted", "sahyog_request_id": "SR-2026-88812", "note": "sent by IO", "reported_by": "Sahyog"}
```

* **Statuses:** `submitted`, `acknowledged`, `responded`, `complied`, `partially_complied`,
  `declined`, `funds_frozen`, `not_pursued`, `closed`.
* The history is append-only. It shows in the dashboard and in `GET /v1/analytics` →
  `recommendation_outcomes` and `outcomes_by_intermediary`.

## 6. Record the intermediary's reply

`POST /v1/attestations`

```json
{"chain": "tron", "address": "T…", "entity_id": "binance", "entity_name": "Binance",
 "document_ref": "Sahyog reply RPLY-77 dated 2026-10-02 from Binance", "as_of": "2026-10-02",
 "polarity": "confirms", "sahyog_reply_id": "RPLY-77"}
```

* **`confirms`:** future cases grade the address A (rule G-A1).
* **`denies`:** labels naming that entity are disregarded for that address (rule G-N1,
  ADR-0020). A later confirmation supersedes the denial. A same-day contradiction is grade X.
* Only the document reference is stored, never personal data from the reply.
* `POST /v1/cases/{id}/rerun` re-runs the same request with the new evidence as a new case
  (`parent_case_id` links them). The original findings and hash are unchanged.

## 7. Standalone pilots without Sahyog

`POST /v1/cases/{id}/routing/{decision_id}/approve` is disabled by default (403).

* With `ANVESHAK_STANDALONE_APPROVALS=1`, a client with the `approve` permission (supervisor
  or admin) can approve `ready_for_approval` drafts.
* The approved payload and its SHA-256 are written to `var/outbox/` for manual upload.

## 8. Alerts and watchlist

* `GET /v1/alerts?unacknowledged=true`, `POST /v1/alerts/{id}/ack`.
* `GET/POST/DELETE /v1/watchlist`. Addresses still holding traced funds are watched
  automatically. A movement raises a `critical` A-WATCH-MOVEMENT alert and queues a
  follow-up trace.
* All alerts are scoped by agency.

## Try it end to end

```bash
anveshak clients add --client-id sahyog-mock --role sahyog
ANVESHAK_CALLBACK_ALLOWLIST=127.0.0.1 ANVESHAK_CALLBACK_SECRET=s3cret anveshak serve
python tools/mock_sahyog/mock_sahyog.py --key <key> --callback-secret s3cret --wallet <tron or btc wallet> --reply confirms
```
