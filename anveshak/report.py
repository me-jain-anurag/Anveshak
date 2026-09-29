"""Investigation report (HTML, print-ready) rendered from a CaseResult with fixed templates.

No generated prose (ADR-0011): every sentence in the report is either template text or a
field of the findings. The HTML file's sha256 is written next to it (`.sha256`) so the
exact document handed over can be identified later; the findings hash inside it ties the
document to reproducible findings.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .case import CaseResult, DataMode
from .chain import Chain
from .chains.base import VerificationStatus
from .domain import EndpointKind, Grade
from .evidence import sha256_hex
from .routing import RouteStatus

_TEMPLATES = Path(__file__).parent / "templates"

GRADE_TEXT = {
    Grade.A: "Attested — named by the entity itself or by a public authority",
    Grade.B: "Corroborated — two or more curated sources with different primary sources agree; none disagree",
    Grade.C: "Single-source — one curated source, or weak sources only",
    Grade.X: "Conflicted — sources disagree about the owner; not routed automatically",
}

RULE_TEXT = {
    "G-A1": "Label from the entity itself (e.g. its published proof-of-reserves address list, or its reply to an LEA request).",
    "G-A2": "Label from a public authority (e.g. OFAC SDN list, court or seizure record).",
    "G-B1": "Two or more curated labels agree on the owner and cite different primary sources.",
    "G-C1": "Exactly one curated label (one primary source).",
    "G-C2": "Only weak labels (web crawl, heuristic, unknown provenance).",
    "G-X1": "Labels name different owners for the same address.",
    "D-COSPEND": "Bitcoin: spent in the same non-CoinJoin transaction as an A/B-attributed address (spending needs each input's key). Grade lowered one step.",
    "D-EVM-KEY": "EVM: A/B-attributed on another EVM chain and an externally-owned account here — the same key holder. Grade lowered one step.",
    "R-SWEEP": "Account chains: every qualifying transfer the address made after value arrived went to the same VASP — a deposit-address pattern.",
}

KIND_TEXT = {
    EndpointKind.VASP: "Entered a VASP's custody",
    EndpointKind.SERVICE: "Entered a non-VASP service",
    EndpointKind.COINJOIN_LIKE: "CoinJoin-like transaction",
    EndpointKind.UNLABELED_CONTRACT: "Unlabelled smart contract",
    EndpointKind.HIGH_ACTIVITY: "High-activity unlabelled address",
    EndpointKind.DORMANT: "Value stopped moving",
    EndpointKind.ORIGIN: "Origin of observed funds",
    EndpointKind.HOP_LIMIT: "Hop limit reached",
    EndpointKind.NOT_EXPANDED: "Search budget exhausted",
    EndpointKind.SOURCE_ERROR: "Data source error",
}


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))
    env.filters["short"] = lambda a: a if not a or len(a) <= 16 else f"{a[:8]}…{a[-6:]}"
    env.filters["dt"] = lambda d: d.strftime("%Y-%m-%d %H:%M:%S UTC") if d else ""
    env.globals.update(
        GRADE_TEXT=GRADE_TEXT,
        RULE_TEXT=RULE_TEXT,
        KIND_TEXT=KIND_TEXT,
        EndpointKind=EndpointKind,
        VerificationStatus=VerificationStatus,
        RouteStatus=RouteStatus,
        Chain=Chain,
    )
    return env


def render_report(result: CaseResult) -> str:
    f = result.findings
    verifications = {v.transfer_id: v for v in f.verifications}
    traces = []
    for trace in f.traces:
        by_id = {t.id: t for t in trace.transfers}
        att = {a.address: a for a in trace.attributions}
        detailed = [e for e in trace.endpoints if e.kind in (EndpointKind.VASP, EndpointKind.SERVICE, EndpointKind.DORMANT, EndpointKind.COINJOIN_LIKE, EndpointKind.UNLABELED_CONTRACT, EndpointKind.HIGH_ACTIVITY)]
        traces.append(
            {
                "trace": trace,
                "by_id": by_id,
                "attributions": att,
                "detailed": detailed,
                "others": [e for e in trace.endpoints if e not in detailed],
                "routing": [d for d in f.routing if d.chain == trace.chain and d.subject == trace.subject and d.direction == trace.params.direction],
            }
        )
    return _env().get_template("report.html.j2").render(
        result=result,
        findings=f,
        synthetic=f.data_mode is DataMode.SYNTHETIC,
        traces=traces,
        verifications=verifications,
    )


def write_report(result: CaseResult, out_dir: Path) -> tuple[Path, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_report(result).encode("utf-8")
    digest = sha256_hex(html)
    path = out_dir / f"{result.case_id}.html"
    path.write_bytes(html)
    (out_dir / f"{result.case_id}.html.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    (out_dir / f"{result.case_id}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path, digest
