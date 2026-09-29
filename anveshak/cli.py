"""Command line interface.

    anveshak demo                                   run the SYNTHETIC scenario, write a report
    anveshak trace --chain tron --address T... --case-ref FIR-123/2026
    anveshak replay <case_id>                       re-run from stored evidence, compare findings hash
    anveshak labels import graphsense|ofac          refresh public label datasets
    anveshak labels stats | lookup | attest
    anveshak serve                                  HTTP API + dashboard
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime

from . import __version__, demo
from .attribution import Attributor
from .case import CaseRequest, CaseResult, DataMode, Engine, Subject, ensure_dirs, load_standard
from .chain import Chain
from .config import load_settings
from .domain import Category, Direction, SourceClass
from .errors import AnveshakError
from .labels.importers import DEFAULT_GRAPHSENSE_PACKS, attestation_label, import_graphsense, import_ofac
from .labels.store import LabelStore, write_jsonl
from .report import write_report
from .storage import CaseStore


def _directions(value: str) -> tuple[Direction, ...]:
    return {"out": (Direction.OUT,), "in": (Direction.IN,), "both": (Direction.OUT, Direction.IN)}[value]


def _print_summary(result: CaseResult, report_path, report_hash: str) -> None:
    f = result.findings
    banner = "  [SYNTHETIC — NOT EVIDENCE]" if f.data_mode is DataMode.SYNTHETIC else ""
    print(f"case {result.case_id}  ref {f.request.case_reference}  mode {f.data_mode.value}{banner}")
    for trace in f.traces:
        print(f"\n{trace.chain.value} {trace.subject} — {'funds out' if trace.params.direction is Direction.OUT else 'funding in'}")
        if not trace.endpoints:
            print(f"  (no endpoints: {trace.coverage.subject_note or 'nothing to follow'})")
        for e in trace.endpoints:
            a = e.attribution
            who = f"{a.entity_name or 'CONFLICT'} [{a.grade.value} {a.rule}]" if a else ""
            amount = e.bottleneck.formatted if e.bottleneck else ""
            print(f"  {e.kind.value:18} hops={e.hops}  {e.address}  {who}  {amount}")
        c = trace.coverage
        print(
            f"  coverage: expanded {c.addresses_expanded}, excluded time-order {c.excluded_time_order} / dust {c.excluded_dust} / "
            f"unverified-token {c.excluded_unverified_asset} / other-asset {c.excluded_other_asset}"
            + (", BUDGET EXHAUSTED" if c.budget_exhausted else "")
            + (f", {len(c.incomplete_histories)} incomplete histories" if c.incomplete_histories else "")
            + (f", {len(c.source_errors)} source errors" if c.source_errors else "")
        )
    if f.routing:
        print("\nrouting drafts:")
        for d in f.routing:
            print(f"  {d.status.value:20} {d.request_type.value:14} {d.target_name}  ({d.chain.value}, {d.direction.value}, grade {d.grade.value if d.grade else '-'})")
    print(f"\nfindings hash  {result.findings_hash}")
    print(f"report         {report_path}\nreport sha256  {report_hash}")


def cmd_demo(args) -> int:
    settings = load_settings()
    ensure_dirs(settings)
    labels, registry, directory = load_standard(settings)
    request = demo.request()
    result = demo.engine(registry, directory).run(request)
    path, digest = write_report(result, settings.reports_dir)
    CaseStore(settings.db_path).create(result.case_id, request.case_reference, "synthetic", request.model_dump(mode="json"))
    CaseStore(settings.db_path).set_done(result.case_id, result.model_dump_json(), result.findings_hash)
    _print_summary(result, path, digest)
    return 0


def cmd_trace(args) -> int:
    settings = load_settings()
    ensure_dirs(settings)
    labels, registry, directory = load_standard(settings)
    chain = Chain(args.chain)
    request = CaseRequest(
        case_reference=args.case_ref,
        subjects=tuple(Subject(chain=chain, address=a) for a in args.address),
        directions=_directions(args.direction),
        max_hops=args.max_hops,
        max_branch=args.max_branch,
        max_expansions=args.max_expansions,
        since=datetime.fromisoformat(args.since) if args.since else None,
        until=datetime.fromisoformat(args.until) if args.until else None,
        follow_all_assets=args.follow_all_assets,
        requested_by=args.requested_by,
    )
    engine = Engine(DataMode.LIVE, labels, registry, directory, settings=settings)
    result = engine.run(request)
    path, digest = write_report(result, settings.reports_dir)
    store = CaseStore(settings.db_path)
    store.create(result.case_id, request.case_reference, "live", request.model_dump(mode="json"))
    store.set_done(result.case_id, result.model_dump_json(), result.findings_hash)
    _print_summary(result, path, digest)
    return 0


def cmd_replay(args) -> int:
    settings = load_settings()
    path = settings.reports_dir / f"{args.case_id}.json"
    if not path.exists():
        print(f"no stored findings at {path}", file=sys.stderr)
        return 2
    original = CaseResult.model_validate_json(path.read_text(encoding="utf-8"))
    if original.findings.data_mode is not DataMode.LIVE:
        print("only live cases can be replayed from evidence", file=sys.stderr)
        return 2
    labels, registry, directory = load_standard(settings)
    problems = []
    if labels.snapshot_hash() != original.findings.label_snapshot:
        problems.append("label set differs from the one used originally (label snapshot hash mismatch)")
    if directory.snapshot_hash() != original.findings.directory_snapshot:
        problems.append("VASP directory differs from the one used originally")
    engine = Engine(DataMode.REPLAY, labels, registry, directory, settings=settings)
    replayed = engine.run(original.findings.request, case_id=original.case_id)
    # Replay runs in REPLAY mode; compare with the mode field normalised.
    replay_findings = replayed.findings.model_copy(update={"data_mode": DataMode.LIVE})
    from .case import findings_hash

    same = findings_hash(replay_findings) == original.findings_hash
    print(f"original findings hash  {original.findings_hash}")
    print(f"replayed findings hash  {findings_hash(replay_findings)}")
    for p in problems:
        print(f"note: {p}")
    print("RESULT: " + ("MATCH — findings reproduced exactly from stored evidence" if same else "DIFFERENT"))
    return 0 if same else 1


def cmd_labels(args) -> int:
    settings = load_settings()
    root = settings.data_dir / "labels" / "imported"
    if args.labels_cmd == "import":
        if args.dataset == "graphsense":
            manifest = import_graphsense(root / "graphsense", packs=args.pack or DEFAULT_GRAPHSENSE_PACKS)
        else:
            manifest = import_ofac(root / "ofac")
        for f in manifest["files"]:
            print(json.dumps(f))
        return 0
    labels, _, directory = load_standard(settings)
    if args.labels_cmd == "stats":
        print(f"{len(labels)} labels, snapshot {labels.snapshot_hash()}")
        for k, v in labels.stats().items():
            print(f"  {k:32} {v}")
        return 0
    if args.labels_cmd == "lookup":
        chain = Chain(args.chain)
        from .addresses import normalize

        address = normalize(chain, args.address)
        attributor = Attributor(labels, aliases=directory.aliases)
        att, risk = attributor.ownership(chain, address), attributor.risk(chain, address)
        print(att.explanation if att else "no ownership attribution")
        if risk:
            print("risk flags: " + ", ".join(risk.flags))
        return 0
    if args.labels_cmd == "attest":
        label = attestation_label(
            Chain(args.chain), args.address, args.entity_id, args.entity_name, Category(args.category),
            args.document_ref, date.fromisoformat(args.as_of), SourceClass(args.source_class),
        )
        path = settings.var_dir / "labels" / "attestations.jsonl"
        existing = list(LabelStore.load([path]).all()) if path.exists() else []
        write_jsonl(path, [*existing, label])
        print(f"recorded: {label.chain.value} {label.address} -> {label.entity_name} ({label.source_class.value}) in {path}")
        return 0
    return 2


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="anveshak", description="Evidence-first attribution of crypto wallets to the nearest VASP.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="run the SYNTHETIC demonstration scenario").set_defaults(func=cmd_demo)

    t = sub.add_parser("trace", help="trace subject address(es) with live data")
    t.add_argument("--chain", required=True, choices=[c.value for c in Chain])
    t.add_argument("--address", required=True, action="append", help="repeat for several subjects on the same chain")
    t.add_argument("--case-ref", required=True)
    t.add_argument("--direction", choices=["out", "in", "both"], default="both")
    t.add_argument("--max-hops", type=int, default=5)
    t.add_argument("--max-branch", type=int, default=20)
    t.add_argument("--max-expansions", type=int, default=150)
    t.add_argument("--since", help="ISO-8601 with timezone, e.g. 2026-09-01T00:00:00+05:30")
    t.add_argument("--until")
    t.add_argument("--follow-all-assets", action="store_true")
    t.add_argument("--requested-by")
    t.set_defaults(func=cmd_trace)

    r = sub.add_parser("replay", help="re-run a live case from stored evidence and compare findings hashes")
    r.add_argument("case_id")
    r.set_defaults(func=cmd_replay)

    lab = sub.add_parser("labels", help="label datasets")
    lsub = lab.add_subparsers(dest="labels_cmd", required=True)
    imp = lsub.add_parser("import")
    imp.add_argument("dataset", choices=["graphsense", "ofac"])
    imp.add_argument("--pack", action="append", help="GraphSense pack file name (repeatable); default: curated list")
    lsub.add_parser("stats")
    lk = lsub.add_parser("lookup")
    lk.add_argument("--chain", required=True, choices=[c.value for c in Chain])
    lk.add_argument("--address", required=True)
    at = lsub.add_parser("attest", help="record a VASP's written confirmation of an address")
    at.add_argument("--chain", required=True, choices=[c.value for c in Chain])
    at.add_argument("--address", required=True)
    at.add_argument("--entity-id", required=True)
    at.add_argument("--entity-name", required=True)
    at.add_argument("--category", default="exchange", choices=[c.value for c in Category])
    at.add_argument("--document-ref", required=True, help="reference of the reply/document, e.g. 'Sahyog reply REF-123 dated 2026-10-02'")
    at.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    at.add_argument("--source-class", default="entity_attested", choices=["entity_attested", "authority"])
    lab.set_defaults(func=cmd_labels)

    s = sub.add_parser("serve", help="run the HTTP API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AnveshakError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # validation errors (bad address, bad parameters)
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
