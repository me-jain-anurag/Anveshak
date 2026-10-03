# ADR-0015: Scalable architecture — queue, workers, monitor, and the path beyond one host

- Status: accepted
- Date: 2026-09-30

## Context

The PS asks for a "scalable architecture capable of handling large-volume blockchain transaction
analysis" and "real-time generation of investigative intelligence". The system must run on
a single laptop, and the design must not block growth.

## Decision

**Now (single host, horizontally scalable workers):**

- Cases are jobs in a **persistent queue** (SQLite in WAL mode). A worker claims a job with one
  atomic `UPDATE … RETURNING`, so no case runs twice. Stale running jobs are re-queued.
- `anveshak serve` runs the API plus N embedded workers. `anveshak worker` processes add capacity.
  `anveshak monitor` runs the watchlist loop. `docker-compose.yml` wires API, workers (scalable) and
  monitor onto a shared volume.
- Per-case **history cache** (`CachingSource`), per-host **rate limiting** and retry with backoff in the fetcher.
- **Real-time**:
  - Addresses still holding funds after a trace are put on a **watchlist**.
  - The monitor raises a critical alert on any new outgoing transfer and queues a follow-up trace.
  - Cross-case **sightings** raise alerts when unlabelled addresses recur across cases.
  - Sahyog callbacks (allow-listed hosts, HMAC-signed) push results when a case finishes.

**Next (multi-host):**

1. Swap SQLite for PostgreSQL. The queue claim becomes `SELECT … FOR UPDATE SKIP LOCKED`, with the same schema.
2. Move the evidence store to object storage (content-addressed keys map 1:1 to S3/MinIO).
3. Replace public APIs with self-hosted data: Bitcoin Core + electrs/Esplora, Erigon/Reth (EVM),
   java-tron, a Solana RPC provider or Geyser indexer. Bulk analytics can use cryo/Parquet or
   BigQuery public datasets. Adapters are already behind the `ChainSource` interface.
4. Shared rate limiter (Redis) across workers when they share API keys.

## Consequences

- One laptop runs the full system, and the same code scales out by adding worker processes.
- The single-host SQLite queue is the known ceiling. The migration path is documented above.
- CI (`.github/workflows/ci.yml`, added 2026-10-04) builds the image on every push and checks that the container reports healthy.

## Update 2026-10-04: deferred items

- **Push monitoring.** The PS lists real-time intelligence as an expected outcome. The watchlist polls (default every 10 minutes) and queues a follow-up trace on movement.
  * True push monitoring subscribes to new blocks or logs over websockets for every watched address. That needs self-hosted nodes per chain (step 3), because public endpoints rate-limit or drop long-lived subscriptions.
  * It is deferred until step 3 exists. The `ChainSource` interface and the monitor's alert path stay the same.
- **PostgreSQL.** Deferred. `storage.py` uses only portable SQL apart from `UPDATE … RETURNING` (supported by PostgreSQL) and the SQLite triggers that keep the outcome history and audit log append-only (PostgreSQL equivalent: a `BEFORE UPDATE OR DELETE` trigger raising an exception, or revoked privileges).
- **Rate limits** are per API process (ADR-0023). With several API replicas, the reverse proxy enforces the global limit.
