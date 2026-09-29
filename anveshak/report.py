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
    "R-SWEEP-TARGET": "Receives a sweep from a deposit-pattern address — the VASP's hot / collection wallet.",
    "R-CONSOLIDATION": "Bitcoin: received from outside, then spent together with a VASP's addresses — a deposit address.",
    "R-LABEL-ROLE": "Role (hot / cold / reserve / deposit wallet) stated in the label text itself.",
    "C-MULTI-INPUT": "Bitcoin: addresses spent together in a non-CoinJoin transaction share a controller (Meiklejohn et al. 2013).",
    "X-THORCHAIN": "Cross-chain link from THORChain's own record of a swap (inbound and outbound transaction ids), confirmed on the destination chain.",
    "T-PEEL": "Peel chain: consecutive transfers, each smaller than the last but keeping most of it.",
    "T-PASS": "Rapid pass-through: most of a received amount forwarded within minutes.",
    "T-FANOUT": "Fan-out: one address pays many distinct addresses within a short window.",
    "T-FANIN": "Fan-in: many distinct addresses pay one address within a short window.",
    "T-MIXER": "Value entered a mixer.",
    "T-COINJOIN": "Value entered a CoinJoin-like transaction.",
    "T-CHAINHOP": "Value crossed to another chain through a bridge or swap service.",
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
    links = {link.id: link for link in f.crosschain_links}
    continued = {c.trace_index: links.get(c.link_id) for c in f.continuations}
    traces = []
    for index, (trace, analysis) in enumerate(zip(f.traces, f.analyses)):
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
                "analysis": analysis,
                "scores": {sc.endpoint_id: sc for sc in analysis.confidences},
                "flow_risks": {fr.scope.split(":", 1)[1]: fr for fr in analysis.flow_risks},
                "continuation": continued.get(index),
            }
        )
    return _env().get_template("report.html.j2").render(
        result=result,
        findings=f,
        synthetic=f.data_mode is DataMode.SYNTHETIC,
        traces=traces,
        verifications=verifications,
        alerts=sorted({(a.severity.value, a.rule, a.message): a for an in f.analyses for a in an.alerts}.values(), key=lambda a: (["critical", "high", "medium", "info"].index(a.severity.value), a.rule)),
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
