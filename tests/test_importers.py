import json

from anveshak.chain import Chain
from anveshak.domain import Category, RiskFlag, SourceClass
from anveshak.labels.importers import attestation_label, parse_ofac_list, parse_tagpack

# Header/tag structure as in graphsense-tagpacks/packs/exchange-wallets-binance.yaml (fetched 2026-09-30).
PACK = b"""
title: Binance reserve wallets
creator: GraphSense Core Team
confidence: service_data
category: exchange
source: https://www.binance.com/en/blog/community/our-commitment-to-transparency-2895840147147652626
lastmod: 2022-11-16
actor: binance
tags:
- address: '34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo'
  currency: BTC
  label: binance reserve wallets BTC
- address: '0xbe0eb53f46cd790cd13851d5eff43d12404d33e8'
  currency: ETH
  label: binance reserve wallets ETH
- address: 'bnb1xxxx'
  currency: BEP2
- address: '0xnot-an-address'
  currency: ETH
- address: '0x28c6c06298d514db089934071355e5743bf21d60'
  currency: ETH
  confidence: web_crawl
  abuse: scam
"""


def test_tagpack_parsing_maps_confidence_and_skips_invalid():
    labels, skipped = parse_tagpack(PACK, "exchange-wallets-binance.yaml")
    assert len(labels) == 3
    btc = next(l for l in labels if l.chain is Chain.BITCOIN)
    assert (btc.entity_id, btc.category, btc.source_class) == ("binance", Category.EXCHANGE, SourceClass.ENTITY_ATTESTED)
    assert btc.as_of.isoformat() == "2022-11-16" and btc.dataset_ref.startswith("graphsense-tagpacks/packs/exchange-wallets-binance.yaml@sha256:")
    overridden = next(l for l in labels if l.address == "0x28c6c06298d514db089934071355e5743bf21d60")
    assert overridden.source_class is SourceClass.WEAK and overridden.risk_flags == (RiskFlag.SCAM,)
    assert skipped["unsupported network bep2"] == 1 and skipped["invalid address"] == 1


def test_ofac_list_applies_evm_address_to_all_evm_chains():
    raw = json.dumps(["0x8589427373D6D84E98730D7795D8f6f8731FDA16", "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg", "bogus"]).encode()
    labels, skipped = parse_ofac_list(raw, "USDT")
    assert all(l.source_class is SourceClass.AUTHORITY and l.risk_flags == (RiskFlag.SANCTIONED,) and l.category is None for l in labels)
    evm = {c for c in Chain if c.family.value == "evm"}
    assert {l.chain for l in labels} == evm | {Chain.TRON}
    assert sum(skipped.values()) == 1


def test_attestation_requires_document_reference():
    import datetime

    import pytest

    lab = attestation_label(Chain.TRON, "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg", "Binance", "Binance", Category.EXCHANGE, "Sahyog reply REF-1", datetime.date(2026, 10, 1))
    assert lab.source_class is SourceClass.ENTITY_ATTESTED and lab.entity_id == "binance"
    with pytest.raises(ValueError):
        attestation_label(Chain.TRON, "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg", "b", "B", Category.EXCHANGE, "  ", datetime.date(2026, 10, 1))
    with pytest.raises(ValueError):
        attestation_label(Chain.TRON, "TA3rH2A7iHnm6pKH8gr9cK1EZnShnmZdFg", "b", "B", Category.EXCHANGE, "doc", datetime.date(2026, 10, 1), SourceClass.CURATED)
