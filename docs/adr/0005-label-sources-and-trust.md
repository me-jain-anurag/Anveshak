# ADR-0005: Label sources, source classes, and the source-trust check

- Status: accepted
- Date: 2026-09-30

## Context

Attribution depends on labels ("this address is Binance's"). Public datasets differ greatly in
reliability, and their own metadata can overstate it. For example, a GraphSense pack marked
`service_data` cites a CoinDesk news article, and another cites a third-party analytics
dashboard (see `docs/references.md`).

## Decision

1. Every `Label` records `source_id` (dataset), `source_class`, `primary_source` (a
   dereferenceable URL or document reference), `as_of` and `dataset_ref` (file plus sha256 of the
   bytes imported). A record without a primary source is rejected at import.
2. Four source classes: `entity_attested`, `authority`, `curated`, `weak`. Importers map dataset
   metadata to classes with explicit tables (e.g. GraphSense `confidence` ids). Their numeric
   "levels" are deliberately ignored.
3. **Source-trust check** (`sourcetrust.py`). A claimed class is re-checked against who actually published it:
   - `entity_attested` counts only if the primary source is on an **official channel** of that entity
     (`official_sources` in `data/vasp_directory.yaml`, domain or domain/path). The exception is an
     investigator record of a formal document (e.g. a VASP's reply to a Sahyog request).
   - `authority` counts only if the primary source is on an **allow-listed authority domain** (`data/authorities.yaml`).
   - Otherwise the label is treated as `curated`, and the report shows why.
4. **Corroboration requires independent primary sources.** Two datasets copying the same page are
   one source (`Label.independence_key`).
5. **Conflicts are never resolved automatically**: labels naming different owners give grade X.
   Dataset-specific ids for the same entity are reconciled only through explicit directory aliases.
6. **Feedback loop.** `anveshak labels attest` and `POST /v1/attestations` record a VASP's written
   confirmation as an `entity_attested` label with the document reference. Future cases grade that address A.

## Consequences

- Imported data ships with provenance (`MANIFEST.json` per dataset: URL, sha256, fetch time,
  kept and skipped counts with reasons).
- Some datasets' self-described confidence is downgraded. That is intended.
- Synthetic demo labels are marked `synthetic` and refused by the live label loader.
