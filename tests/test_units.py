"""Unit tests: source trust, scoring arithmetic, typology detectors, intel providers, exports."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
import pytest

from anveshak import demo
from anveshak.chain import Chain
from anveshak.chains.base import Verification, VerificationStatus
from anveshak.chains.memory import MemorySource
from anveshak.attribution import Attributor, grade_labels
from anveshak.config import DATA_DIR
from anveshak.domain import Category, Direction, Endpoint, EndpointKind, Grade, RiskFlag, SourceClass
from anveshak.errors import SourceError
from anveshak.evidence import EvidenceStore, LiveFetcher
from anveshak.exports import to_cypher, to_graphml
from anveshak.intel import ChainalysisSanctions, EtherscanNametags
from anveshak.labels.store import LabelStore
from anveshak.policy import ScoringPolicy
from anveshak.scoring import score_endpoint
from anveshak.sourcetrust import SourceTrust, matches
from anveshak.tracer import TraceParams, Tracer
from anveshak.typologies import detect

from .conftest import label, xfer

E = Chain.ETHEREUM
A1 = "0x" + "a1" * 20
POLICY = ScoringPolicy.load(DATA_DIR / "scoring_policy.yaml")


@pytest.fixture
def trust():
    return SourceTrust.load(DATA_DIR / "authorities.yaml", {"binance": ("binance.com",), "okex": ("twitter.com/okx",)})


# --------------------------------------------------------------------------- source trust

def test_domain_and_path_matching():
    assert matches("https://www.binance.com/en/blog/x", "binance.com")
    assert matches("https://sub.binance.com/x", "binance.com")
    assert not matches("https://binance.com.evil.example/x", "binance.com")
    assert matches("https://twitter.com/okx/status/1", "twitter.com/okx")
    assert not matches("https://twitter.com/okx_fake/status/1", "twitter.com/okx")


def test_entity_attested_requires_official_channel(trust):
    official = label(E, A1, SourceClass.ENTITY_ATTESTED, "https://www.binance.com/en/blog/por", entity="binance")
    news = label(E, A1, SourceClass.ENTITY_ATTESTED, "https://www.coindesk.com/some-article", entity="binance")
    assert grade_labels(E, A1, (official,), trust=trust).grade is Grade.A
    downgraded = grade_labels(E, A1, (news,), trust=trust)
    assert (downgraded.grade, downgraded.rule) == (Grade.C, "G-C1") and "coindesk.com" in downgraded.trust_notes[0]


def test_authority_requires_allow_listed_domain(trust):
    ofac = label(E, A1, SourceClass.AUTHORITY, "https://home.treasury.gov/news/press-releases/x")
    blog = label(E, A1, SourceClass.AUTHORITY, "https://some-blog.example/ofac-list")
    assert grade_labels(E, A1, (ofac,), trust=trust).rule == "G-A2"
    assert grade_labels(E, A1, (blog,), trust=trust).rule == "G-C1"


# --------------------------------------------------------------------------- scoring

def _endpoint(att, hops=2, path=("t1", "t2"), role=None):
    return Endpoint(kind=EndpointKind.VASP, chain=E, address=A1, hops=hops, path=path, attribution=att, adjacent_address="0x" + "b2" * 20, adjacent_role=role)


def _verified(*ids, status=VerificationStatus.VERIFIED):
    return {i: Verification(transfer_id=i, status=status, method="t") for i in ids}


def test_confidence_points_are_itemised_and_sum():
    att = grade_labels(E, A1, (label(E, A1, SourceClass.ENTITY_ATTESTED, "https://ex.example"),))
    s = score_endpoint(_endpoint(att, role="deposit-address pattern (R-SWEEP): ..."), _verified("t1", "t2"), POLICY)
    assert [(i.component, i.points) for i in s.items] == [("attribution", 60), ("path", 25), ("proximity", 8), ("corroboration", 5)]
    assert s.score == 98 and s.band == "high"


def test_curated_sources_accumulate_to_cap():
    labels = tuple(label(E, A1, SourceClass.CURATED, f"https://list{i}.example") for i in range(5))
    s = score_endpoint(_endpoint(grade_labels(E, A1, labels)), _verified("t1", "t2"), POLICY)
    assert s.items[0].points == 50  # 30 + 15 + 15 ... capped at 50


def test_hard_rules_zero_the_score():
    conflict = grade_labels(E, A1, (label(E, A1, SourceClass.CURATED, "https://a.example"), label(E, A1, SourceClass.CURATED, "https://b.example", entity="ex2")))
    assert score_endpoint(_endpoint(conflict), _verified("t1", "t2"), POLICY).score == 0
    good = grade_labels(E, A1, (label(E, A1, SourceClass.ENTITY_ATTESTED, "https://ex.example"),))
    mismatch = {**_verified("t1"), **_verified("t2", status=VerificationStatus.MISMATCH)}
    s = score_endpoint(_endpoint(good), mismatch, POLICY)
    assert s.score == 0 and s.hard_rule and all(not i.counted for i in s.items)


def test_unverifiable_path_scores_less_than_verified():
    att = grade_labels(E, A1, (label(E, A1, SourceClass.ENTITY_ATTESTED, "https://ex.example"),))
    full = score_endpoint(_endpoint(att), _verified("t1", "t2"), POLICY).score
    partial = score_endpoint(_endpoint(att), {**_verified("t1"), **_verified("t2", status=VerificationStatus.UNVERIFIABLE)}, POLICY).score
    assert partial == full - 15


# --------------------------------------------------------------------------- typologies

def _trace(registry, transfers, direction=Direction.OUT, subject=None):
    src = MemorySource(Chain.TRON, transfers)
    return Tracer(src, Attributor(LabelStore()), registry).trace(subject or demo.tron_addr("s"), TraceParams(direction=direction))


def test_fan_out_and_fan_in(registry):
    usdt = registry.token(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT", 6)
    T = demo.tron_addr
    fan_out = [xfer(usdt, T("s"), T(f"m{i}"), 100_000_000, i) for i in range(6)]
    assert any(h.rule == "T-FANOUT" for h in detect(_trace(registry, fan_out), POLICY))
    fan_in = [xfer(usdt, T(f"v{i}"), T("s"), 100_000_000, i) for i in range(6)]
    assert any(h.rule == "T-FANIN" for h in detect(_trace(registry, fan_in, Direction.IN), POLICY))
    few = [xfer(usdt, T("s"), T(f"m{i}"), 100_000_000, i) for i in range(3)]
    assert not any(h.rule == "T-FANOUT" for h in detect(_trace(registry, few), POLICY))


# --------------------------------------------------------------------------- intel providers

def _fetcher(tmp_path, handler):
    return LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_chainalysis_provider_parses_and_is_strict(tmp_path, trust):
    ok = {"identifications": [{"category": "sanctions", "name": "SANCTIONS: OFAC SDN X", "description": "d", "url": "https://home.treasury.gov/news/x"}]}
    labels = ChainalysisSanctions(_fetcher(tmp_path, lambda r: httpx.Response(200, json=ok)), "k").lookup(E, A1)
    assert labels[0].risk_flags == (RiskFlag.SANCTIONED,) and labels[0].dataset_ref.startswith("evidence:")
    assert trust.effective(labels[0], None)[0] is SourceClass.AUTHORITY
    assert ChainalysisSanctions(_fetcher(tmp_path / "e", lambda r: httpx.Response(200, json={"identifications": []})), "k").lookup(E, A1) == []
    with pytest.raises(SourceError):
        ChainalysisSanctions(_fetcher(tmp_path / "b", lambda r: httpx.Response(200, json={"unexpected": 1})), "k").lookup(E, A1)


def test_etherscan_nametag_provider(tmp_path):
    payload = {"status": "1", "message": "OK", "result": [{"address": A1, "nametag": "Coinbase 10", "labels": ["Coinbase", "Exchange"], "labels_slug": ["coinbase", "exchange"]}]}
    (lab,) = EtherscanNametags(_fetcher(tmp_path, lambda r: httpx.Response(200, json=payload)), "k").lookup(E, A1)
    assert (lab.entity_id, lab.category, lab.source_class) == ("coinbase", Category.EXCHANGE, SourceClass.CURATED)
    assert lab.primary_source == "https://etherscan.io/address/" + A1
    assert EtherscanNametags(None, "k").lookup(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t") == []


# --------------------------------------------------------------------------- exports

def test_exports_cover_transfers_and_cross_chain(registry, real_directory):
    result = demo.engine(registry, real_directory).run(demo.request())
    cypher = to_cypher(result)
    assert "MERGE (a:Address" in cypher and ":CROSS_CHAIN" in cypher and ":TRANSFER" in cypher
    root = ET.fromstring(to_graphml(result).split("\n", 1)[1])
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    assert len(root.findall(".//g:node", ns)) > 10 and len(root.findall(".//g:edge", ns)) > 10
