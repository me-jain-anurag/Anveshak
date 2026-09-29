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
