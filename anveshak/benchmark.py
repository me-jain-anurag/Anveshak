"""Ground-truth benchmark for "nearest VASP" (docs/benchmark.md).

Each case in benchmarks/cases/*.yaml is transcribed from a public document (court filing,
enforcement action) that names the VASP on one side of specific on-chain funds. The case
records the document URL, its sha256 as downloaded, the page/paragraph and a verbatim quote.
A case states only what its document states.

The runner traces each case's subject in the stated direction with the live engine
(recording evidence, so every run can be replayed) and scores the result:

  HIT@1      the nearest VASP (rank 1) is an expected entity
  HIT        an expected entity is among the nearest VASPs, but not rank 1
  WRONG      VASPs were reached, none of them expected
  NOT_FOUND  no VASP reached (the coverage reason is reported)
  SKIPPED    the chain cannot be traced with the current configuration (e.g. no API key)
  ERROR      the trace failed

Grades and confidence of correct vs. wrong attributions are reported separately, so the
benchmark also measures whether confidence separates right from wrong.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .case import CaseRequest, DataMode, Engine, Subject, chains_needing_since, evm_backend, live_fetcher, load_standard
from .chain import Chain, ChainFamily
from .config import Settings
from .domain import Direction
from .errors import AnveshakError
from .evidence import EvidenceStore, ReplayFetcher


class Source(BaseModel):
    publisher: str
    document: str
    url: str
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    retrieved: date
    locator: str
    quote: str


class Expected(BaseModel):
    entities: list[str] = Field(min_length=1)
    relation: str  # how the document relates the subject to the VASP


class BenchmarkCase(BaseModel):
    id: str
    title: str
    chain: Chain
    subject: str
    direction: Direction
    since: datetime | None = None
    until: datetime | None = None
    window_hours: int = 72  # RPC log-scan window, EVM chains without an indexer
    max_hops: int = 4
    max_expansions: int = 60
    claim_scope: str = "address"  # "address" or "cluster" (the document speaks of a group of addresses)
    expected: Expected
    sources: list[Source] = Field(min_length=1)
    ledger_check: str | None = None
    notes: str | None = None


def load_cases(directory: Path, only: list[str] | None = None) -> list[BenchmarkCase]:
    cases = [BenchmarkCase.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted(Path(directory).glob("*.yaml"))]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate benchmark case id")
    return [c for c in cases if not only or c.id in only]


def _skip_reason(settings: Settings, case: BenchmarkCase) -> str | None:
    if case.chain.family is ChainFamily.EVM:
        backend = evm_backend(settings, case.chain)
        if backend == "etherscan" and not settings.etherscan_api_key:
            return f"{case.chain.value}: no ETHERSCAN_API_KEY and no ANVESHAK_RPC_{case.chain.value.upper()} configured"
        if chains_needing_since(settings, [case.chain]) and case.since is None:
            return f"{case.chain.value}: RPC log scan needs `since` and the case has none"
    return None


def run_case(case: BenchmarkCase, settings: Settings, mode: DataMode, labels, registry, directory) -> dict:
    base = {"id": case.id, "chain": case.chain.value, "direction": case.direction.value, "expected": case.expected.entities, "claim_scope": case.claim_scope}
    reason = _skip_reason(settings, case)
    if reason:
        return {**base, "outcome": "SKIPPED", "detail": reason}
    s = replace(settings, logscan_window_hours=case.window_hours)
    store = EvidenceStore(s.evidence_dir)
    fetcher = ReplayFetcher(store) if mode is DataMode.REPLAY else live_fetcher(s, store)
    engine = Engine(mode, labels, registry, directory, settings=s, fetcher=fetcher)
    request = CaseRequest(
        case_reference=f"benchmark {case.id}", subjects=(Subject(chain=case.chain, address=case.subject),), directions=(case.direction,),
        since=case.since, until=case.until, max_hops=case.max_hops, max_expansions=case.max_expansions, follow_cross_chain=False,
    )
    try:
        result = engine.run(request, case_id=f"bench-{case.id}")
    except (AnveshakError, ValueError) as exc:
        return {**base, "outcome": "ERROR", "detail": f"{type(exc).__name__}: {exc}"}
    f = result.findings
    expected = {directory.canonical(e.lower()) for e in case.expected.entities}
    nearest = [n for a in f.analyses for n in a.nearest_vasps]
    reached = [
        {"rank": n.rank, "entity": directory.canonical(n.entity_id) if n.entity_id else None, "name": n.entity_name, "grade": n.grade.value if hasattr(n.grade, "value") else n.grade,
         "confidence": n.confidence, "hops": n.hops, "correct": bool(n.entity_id) and directory.canonical(n.entity_id) in expected}
        for n in nearest
    ]
    cov = f.traces[0].coverage if f.traces else None
    coverage = None
    if cov is not None:
        coverage = {
            "expanded": cov.addresses_expanded, "budget_exhausted": cov.budget_exhausted, "incomplete_histories": len(cov.incomplete_histories),
            "source_errors": list(cov.source_errors)[:3], "windowed": bool(cov.windowed_histories), "subject_note": cov.subject_note,
        }
    if not reached:
        outcome = "NOT_FOUND"
    elif reached[0]["correct"]:
        outcome = "HIT@1"
    elif any(r["correct"] for r in reached):
        outcome = "HIT"
    else:
        outcome = "WRONG"
    return {**base, "outcome": outcome, "reached": reached, "coverage": coverage, "findings_hash": result.findings_hash, "evidence_objects": len(f.evidence_ids)}


def summarise(results: list[dict]) -> dict:
    ran = [r for r in results if r["outcome"] not in ("SKIPPED", "ERROR")]
    n = len(ran)
    correct = [x for r in ran for x in r.get("reached", []) if x["correct"]]
    wrong = [x for r in ran for x in r.get("reached", []) if not x["correct"]]
    mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "cases": len(results),
        "ran": n,
        "skipped": sum(r["outcome"] == "SKIPPED" for r in results),
        "errors": sum(r["outcome"] == "ERROR" for r in results),
        "hit_at_1": sum(r["outcome"] == "HIT@1" for r in ran),
        "hit_at_any": sum(r["outcome"] in ("HIT@1", "HIT") for r in ran),
        "wrong_vasp_cases": sum(r["outcome"] == "WRONG" for r in ran),
        "not_found": sum(r["outcome"] == "NOT_FOUND" for r in ran),
        "chains": sorted({r["chain"] for r in ran}),
        "correct_attributions": {"count": len(correct), "mean_confidence": mean([x["confidence"] for x in correct]), "grades": sorted(x["grade"] for x in correct)},
        "wrong_attributions": {"count": len(wrong), "mean_confidence": mean([x["confidence"] for x in wrong]), "grades": sorted(x["grade"] for x in wrong)},
        "hops_to_correct": sorted(x["hops"] for x in correct),
    }


def render_markdown(cases: list[BenchmarkCase], results: list[dict], summary: dict, mode: DataMode, label_snapshot: str) -> str:
    by_id = {c.id: c for c in cases}
    lines = [
        f"<!-- generated by `anveshak benchmark` on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC; mode {mode.value}; label snapshot {label_snapshot[:16]} -->",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Cases | {summary['cases']} ({summary['ran']} traced, {summary['skipped']} skipped, {summary['errors']} errors) |",
        f"| Chains traced | {', '.join(summary['chains']) or '—'} |",
        f"| hit@1 (nearest VASP correct) | {summary['hit_at_1']} / {summary['ran']} |",
        f"| hit@any (expected VASP among those reached) | {summary['hit_at_any']} / {summary['ran']} |",
        f"| Wrong-VASP cases (only other VASPs reached) | {summary['wrong_vasp_cases']} |",
        f"| Not found (no VASP reached) | {summary['not_found']} |",
        f"| Correct attributions: count / mean confidence / grades | {summary['correct_attributions']['count']} / {summary['correct_attributions']['mean_confidence']} / {', '.join(summary['correct_attributions']['grades']) or '—'} |",
        f"| Other VASPs reached: count / mean confidence / grades | {summary['wrong_attributions']['count']} / {summary['wrong_attributions']['mean_confidence']} / {', '.join(summary['wrong_attributions']['grades']) or '—'} |",
        f"| Hops to the correct VASP | {', '.join(map(str, summary['hops_to_correct'])) or '—'} |",
        "",
        "| Case | Chain | Direction | Expected | Outcome | Reached (rank: entity grade/confidence, hops) | Coverage / reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c = by_id[r["id"]]
        reached = "; ".join(f"{x['rank']}: {x['name'] or 'conflict'} {x['grade']}/{x['confidence']}, {x['hops']} hop(s)" for x in r.get("reached", [])) or "—"
        cov = r.get("coverage") or {}
        if r["outcome"] in ("SKIPPED", "ERROR"):
            why = r["detail"]
        else:
            bits = [f"{cov.get('expanded')} expanded"]
            if cov.get("budget_exhausted"):
                bits.append("budget exhausted")
            if cov.get("incomplete_histories"):
                bits.append(f"{cov['incomplete_histories']} incomplete histories")
            if cov.get("windowed"):
                bits.append("window-limited (RPC log scan)")
            if cov.get("source_errors"):
                bits.append("source errors: " + " | ".join(cov["source_errors"]))
            if cov.get("subject_note"):
                bits.append(cov["subject_note"])
            why = "; ".join(bits)
        scope = " (cluster-level claim)" if c.claim_scope == "cluster" else ""
        lines.append(f"| {c.id}{scope} | {c.chain.value} | {c.direction.value} | {', '.join(c.expected.entities)} | **{r['outcome']}** | {reached} | {why} |")
    return "\n".join(lines) + "\n"


MARK_START = "<!-- BENCHMARK-RESULTS:START -->"
MARK_END = "<!-- BENCHMARK-RESULTS:END -->"


def run(settings: Settings, cases_dir: Path, mode: DataMode, only: list[str] | None, results_path: Path, doc_path: Path | None) -> dict:
    labels, registry, directory = load_standard(settings)
    cases = load_cases(cases_dir, only)
    results = []
    for case in cases:
        print(f"{case.id} {case.chain.value} {case.direction.value} {case.subject[:14]}… expected {case.expected.entities}", flush=True)
        r = run_case(case, settings, mode, labels, registry, directory)
        print(f"  -> {r['outcome']} {r.get('detail', '')}", flush=True)
        results.append(r)
    summary = summarise(results)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": mode.value, "label_snapshot": labels.snapshot_hash(), "summary": summary, "results": results}
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8", newline="\n")
    if doc_path is not None and doc_path.exists():
        text = doc_path.read_text(encoding="utf-8")
        if MARK_START in text and MARK_END in text:
            head, rest = text.split(MARK_START, 1)
            _, tail = rest.split(MARK_END, 1)
            table = render_markdown(cases, results, summary, mode, labels.snapshot_hash())
            doc_path.write_text(head + MARK_START + "\n" + table + MARK_END + tail, encoding="utf-8", newline="\n")
    return out
