"""End-to-end behaviour on the synthetic scenario, routing policy, and safety interlocks."""

import pytest

from anveshak import demo
from anveshak.case import CaseRequest, DataMode, Engine, Subject
from anveshak.chain import Chain
from anveshak.chains.memory import MemorySource
from anveshak.domain import Direction, EndpointKind, Grade
from anveshak.labels.store import LabelLoadError, LabelStore, write_jsonl
from anveshak.report import render_report
from anveshak.routing import RequestType, RouteStatus


@pytest.fixture
def demo_engine(registry, real_directory):
    return demo.engine(registry, real_directory)


@pytest.fixture
def demo_request():
    return demo.request()


def _routes(result, request_type=RequestType.DISCLOSURE):
    return {(d.chain, d.direction, d.target_entity_id or d.target_name): d for d in result.findings.routing if d.request_type is request_type}


def test_demo_scenario_outcomes(demo_engine, demo_request):
    r = demo_engine.run(demo_request)
    routes = _routes(r)
    alpha_out = routes[(Chain.TRON, Direction.OUT, "demo-alpha")]
    assert alpha_out.status is RouteStatus.READY_FOR_APPROVAL and alpha_out.grade is Grade.A
    assert alpha_out.confidence >= 80 and alpha_out.confidence_band == "high"
    assert demo.T["deposit-alpha"] in alpha_out.addresses  # deposit address identified (R-SWEEP)
    assert routes[(Chain.TRON, Direction.IN, "demo-alpha")].status is RouteStatus.READY_FOR_APPROVAL  # funding withdrawal
    assert routes[(Chain.TRON, Direction.OUT, "demo-beta")].status is RouteStatus.ANALYST_REVIEW  # grade C
    assert routes[(Chain.BITCOIN, Direction.OUT, "demo-alpha")].grade is Grade.B  # D-COSPEND
    blocked = [d for d in r.findings.routing if d.status is RouteStatus.BLOCKED]
    assert {d.request_type for d in blocked} == {RequestType.DISCLOSURE, RequestType.FREEZE}
    assert all(d.grade is Grade.X and d.confidence == 0 for d in blocked)
    freeze = _routes(r, RequestType.FREEZE)[(Chain.TRON, Direction.OUT, "demo-alpha")]
    assert freeze.status is RouteStatus.READY_FOR_APPROVAL  # confidence above the freeze threshold
    issuer = [d for d in r.findings.routing if d.request_type is RequestType.ISSUER_FREEZE]
    assert len(issuer) == 1 and issuer[0].target_entity_id == "tether" and issuer[0].status is RouteStatus.ANALYST_REVIEW

    btc_out = next(t for t in r.findings.traces if t.chain is Chain.BITCOIN and t.params.direction is Direction.OUT)
    assert {e.kind for e in btc_out.endpoints} == {EndpointKind.VASP, EndpointKind.COINJOIN_LIKE}


def test_demo_analysis_outputs(demo_engine, demo_request):
    r = demo_engine.run(demo_request)
    tron_out = next(a for a in r.findings.analyses if a.chain == "tron" and a.direction == "out")
    # nearest VASP answer: ranked by hops, then confidence
    alpha = tron_out.nearest_vasps[0]  # resolved owners rank first, nearest first
    assert alpha.entity_id == "demo-alpha" and alpha.hops == 3 and alpha.deposit_address == demo.T["deposit-alpha"]
    assert alpha.confidence >= 80
    disputed = tron_out.nearest_vasps[-1]  # the conflicted address is 2 hops away but listed last, scored 0
    assert disputed.grade is Grade.X and disputed.hops == 2 and disputed.confidence == 0
    kinds = {h.rule for h in tron_out.typologies}
    assert {"T-PEEL", "T-PASS", "T-MIXER"} <= kinds
    roles = {(p.address, rt.role.value) for p in tron_out.profiles for rt in p.roles}
    assert (demo.T["deposit-alpha"], "deposit_address") in roles and (demo.T["alpha-hot"], "hot_wallet") in roles
    assert any(c.kind == "entity" and c.entity_id == "demo-alpha" for c in tron_out.clusters)
    tron_risk = next(x for x in r.findings.subject_risks if x.chain == "tron")
    assert tron_risk.level in ("medium", "high", "severe") and tron_risk.score > 0
    assert any(a.rule == "A-FREEZE-OPPORTUNITY" for a in tron_out.alerts)
    btc_out = next(a for a in r.findings.analyses if a.chain == "bitcoin" and a.direction == "out")
    subject_cluster = next(c for c in btc_out.clusters if c.kind == "multi_input" and c.contains_subject)
    assert {m.address for m in subject_cluster.members} == {demo.B["suspect"], demo.B["suspect-2"]}


def test_confidence_items_sum_to_score(demo_engine, demo_request):
    r = demo_engine.run(demo_request)
    for a in r.findings.analyses:
        for s in a.confidences:
            if s.hard_rule is None:
                assert s.score == sum(i.points for i in s.items)
            else:
                assert s.score == 0


def test_findings_hash_is_reproducible(demo_engine, demo_request):
    a, b = demo_engine.run(demo_request), demo_engine.run(demo_request)
    assert a.case_id != b.case_id
    assert a.findings_hash == b.findings_hash


def test_verification_mismatch_blocks_routing(registry, real_directory, demo_request):
    sources = demo.sources(registry)
    tron = sources[Chain.TRON]
    sweep = next(t for t in tron._transfers if t.receiver == demo.T["alpha-hot"] and t.sender == demo.T["deposit-alpha"])
    sources[Chain.TRON] = MemorySource(Chain.TRON, tron._transfers, balances=tron._balances, tampered={sweep.id: sweep.model_copy(update={"amount": 1})})
    engine = Engine(DataMode.SYNTHETIC, demo.labels(), registry, demo.directory(real_directory), sources=sources)
    routes = _routes(engine.run(demo_request))
    alpha_out = routes[(Chain.TRON, Direction.OUT, "demo-alpha")]
    assert alpha_out.status is RouteStatus.BLOCKED
    assert "MISMATCH" in alpha_out.reasons[0]


def test_ready_requires_directory_channel(registry, real_directory, demo_request):
    no_channels = demo.directory(real_directory)
    entries = [e.model_copy(update={"channels": ()}) if e.entity_id == "demo-alpha" else e for e in no_channels.entries()]
    from anveshak.directory import VaspDirectory

    engine = Engine(DataMode.SYNTHETIC, demo.labels(), registry, VaspDirectory(entries), sources=demo.sources(registry))
    route = _routes(engine.run(demo_request))[(Chain.TRON, Direction.OUT, "demo-alpha")]
    assert route.status is RouteStatus.ANALYST_REVIEW and "contact channel" in route.reasons[0]


def test_synthetic_labels_refused_by_live_loader(tmp_path):
    path = tmp_path / "x.jsonl"
    write_jsonl(path, list(demo.labels().all()))
    with pytest.raises(LabelLoadError, match="synthetic"):
        LabelStore.load([path])


def test_invalid_subject_address_rejected():
    with pytest.raises(ValueError):
        CaseRequest(case_reference="x", subjects=(Subject(chain=Chain.TRON, address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"),))


def test_report_is_watermarked_and_has_no_probabilities(demo_engine, demo_request):
    html = render_report(demo_engine.run(demo_request))
    assert "SYNTHETIC — NOT EVIDENCE" in html
    assert "%" not in html.replace("100%", "")  # no percentages / probabilities anywhere
    assert "G-A1" in html and "D-COSPEND" in html and "R-SWEEP" in html


def test_cross_chain_continuation(demo_engine, demo_request):
    r = demo_engine.run(demo_request)
    (link,) = r.findings.crosschain_links
    assert link.rule == "X-THORCHAIN" and link.to_chain is Chain.BITCOIN and link.destination_confirmed is True
    (cont,) = r.findings.continuations
    trace = r.findings.traces[cont.trace_index]
    assert trace.chain is Chain.BITCOIN and trace.subject == demo.B["thor-out"]
    vasp = next(e for e in trace.endpoints if e.kind is EndpointKind.VASP)
    assert vasp.attribution.entity_id == "demo-alpha" and vasp.attribution.rule == "D-COSPEND"
    via = [d for d in r.findings.routing if d.chain is Chain.BITCOIN and d.subject == demo.B["thor-out"]]
    assert via and all(d.via and "thorchain" in d.via for d in via)
    tron_out = next(a for a in r.findings.analyses if a.chain == "tron" and a.direction == "out")
    assert any(h.rule == "T-CHAINHOP" for h in tron_out.typologies)
