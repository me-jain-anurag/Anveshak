# ADR-0023: Per-client API keys, roles, agency scoping, limits and an append-only audit log

- Status: accepted
- Date: 2026-10-04

## Context

The API had one shared token. In deployment, several parties call it: the Sahyog portal,
investigators of different agencies, and auditors. An agency must not see another agency's
cases, but deconfliction ("this wallet is in someone else's case too") is valuable. Access to
investigation data must be accountable, and India's DPDP Act 2023 expects purpose limitation
and access records.

## Decision

- **Clients**, configured in `api_clients.yaml` (`ANVESHAK_API_CLIENTS_FILE`).
  * Each client has an id, the sha256 of its key, roles, an optional `agency_id`, an optional
    IP allowlist (CIDR) and a rate limit per minute.
  * `anveshak clients add` generates a key, prints it once and stores only its hash.
  * Keys are compared in constant time.
  * Fallbacks: the legacy `ANVESHAK_API_TOKEN` becomes one `admin` client. With neither
    configured, the API is open as a local admin. That is development only, and `/v1/meta`
    reports it.
- **Roles → permissions:**
  * `sahyog`: create cases, read all cases, record outcomes and replies, screen, upload
    intermediary ids.
  * `investigator`: create and read **own agency** cases, screen, watchlist.
  * `supervisor`: investigator + replies, outcomes and standalone approvals.
  * `auditor`: read-only across agencies, plus the audit log.
  * `admin`: everything.

  Agency-scoped roles must have an `agency_id`; configuration fails otherwise.
- **Agency scoping:**
  * Cases carry `agency_id`: the client's own, or for Sahyog the one in the report.
  * Scoped clients get 404 for another agency's case, alert, watch or link. The API never
    confirms that such a case exists.
  * **Cross-agency sightings are anonymised.** The alert says the address "also appears in N
    case(s) of another agency — contact the I4C coordinator for deconfliction". Links and
    address lookups give counts only.
  * Clients with cross-agency read rights (Sahyog, auditor) see the full picture.
- **Limits:**
  * A per-client rate limit (sliding minute, per API process) returns 429.
  * A per-client IP allowlist returns 403.
  * Request bodies are capped at `ANVESHAK_MAX_BODY_BYTES` (default 1 MiB, 413). The cap is
    checked against Content-Length and counted while streaming.
- **Audit log:**
  * Every `/v1` call except health is recorded, including refused ones: time, client, agency,
    source IP, method, path, status and case id.
  * Each entry stores `entry_hash = sha256(prev_hash + entry)`. SQLite triggers reject UPDATE
    and DELETE.
  * Auditors read it with `GET /v1/audit` and check the chain with `GET /v1/audit/verify`.
  * Request bodies are not logged, so no personal data gets in.
- **At the reverse proxy, not in the app:**
  * TLS; mutual TLS or OAuth 2.0 client credentials for the Sahyog link;
  * WAF rules;
  * global rate limits across replicas.

  Sample nginx settings are in docs/sahyog-integration.md.

## DPDP Act 2023 notes

* Anveshak processes pseudonymous blockchain data and investigation metadata. It never stores
  KYC replies: an attestation keeps only the document reference.
* Data processed for law-enforcement purposes may fall under the Act's exemption for prevention
  and investigation of offences. The audit log, agency scoping and minimal retention support
  accountability either way.
* Retention periods are a deployment policy. Cases, evidence and logs live under `var/` and can
  be archived per the agency's rules.

## Consequences

- Multi-agency deployment is possible without separate instances.
- Rate limits are per process. Multi-replica deployments rely on the proxy for global limits
  (ADR-0015).
