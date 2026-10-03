"""Cross-chain resolvers against response shapes captured from the live APIs on 2026-10-03
(tests/fixtures/crosschain; large signed-VAA blobs trimmed)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from anveshak.chain import Chain
from anveshak.chains.memory import MemorySource
from anveshak.crosschain import AcrossResolver, CrossChainLink, LayerZeroResolver, ThorchainResolver, WormholeResolver, recipient_from_destination
from anveshak.case import DataMode, Engine
from anveshak.domain import Category, SourceClass
from anveshak.errors import EvidenceMissing, SourceError
from anveshak.evidence import EvidenceStore, LiveFetcher, ReplayFetcher
from anveshak.labels.importers import parse_thorchain_inbound
from anveshak.labels.store import LabelStore

from .conftest import xfer

FIX = Path(__file__).parent / "fixtures" / "crosschain"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fetcher_for(tmp_path, routes: dict[str, tuple[int, object]]):
    """routes: substring of the URL → (status, JSON body). Unmatched → 404 with a JSON error."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, (status, body) in routes.items():
            if key in url:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"message": "not found"})

    store = EvidenceStore(tmp_path)
    return LiveFetcher(store, client=httpx.Client(transport=httpx.MockTransport(handler)), retries=0), store


# --------------------------------------------------------------------------- Wormhole

SOL_TX = "4aAnwxNUZ4TaHSehrDuxUFqD1ZiMFuAUjZD2LK7zieJ5rdiA1UayWuvg87v1dSW57kmjB1GMGhyLPZfNawVRhLYV"


def test_wormhole_token_bridge_link(tmp_path):
    fetcher, _ = fetcher_for(tmp_path, {"txHash=" + SOL_TX: (200, fixture("wormholescan_operations_sol_to_bsc.json"))})
    links = WormholeResolver(fetcher).resolve(Chain.SOLANA, SOL_TX, "5i1xVD5x84iA7TpT64iHGUrmNLF8qaT4AwqVLATLU9kp", "ep1")
    assert len(links) == 1
    link = links[0]
    assert link.rule == "X-WORMHOLE" and link.to_chain is Chain.BSC
    assert link.to_address == "0x6c656b4b6042a0c477dcc969d6e0ea40d83d97ad"  # normalised from the checksummed form
    assert link.to_tx == "0xb97f1a9463b4215606b606b46079fc15df50e3bc69315790fcef061385bbca74"
    assert link.status == "completed" and "PORTAL_TOKEN_BRIDGE" in link.memo
    assert "toAddress" in link.recipient_basis and link.evidence_id


def test_wormhole_skips_other_transactions_and_undecoded_messages(tmp_path):
    ops = fixture("wormholescan_operations_mixed.json")
    evm_tx = "0x787bda0b2d408d3b0e87b4067f54e0d45f5811fcf0cddfdbbe3b1cab6089db44"  # generic message, no decoded destination
    fetcher, _ = fetcher_for(tmp_path, {"operations": (200, ops)})
    r = WormholeResolver(fetcher)
    assert r.resolve(Chain.AVALANCHE, evm_tx, "0x" + "11" * 20, "ep") == []
    # the Solana -> NEAR transfer in the same feed: destination chain not traceable, kept with its code
    sol = "uC75XRv8EVrbQawwd1dbhuMx8fPVwFMssp6ErmNB3n7YCehQYQXKBYYeKqbsdEg8ixwxY2rDxqYesF7U6DrLpKA"
    [near] = r.resolve(Chain.SOLANA, sol, "6bKoLDvB5zbSdtticwgg4oncTm4s3e9DwhJtxZJyhhcv", "ep")
    assert near.to_chain is None and near.to_chain_code == "near" and near.to_address == "rawcat8888.near"


def test_wormhole_unknown_tx(tmp_path):
    fetcher, _ = fetcher_for(tmp_path, {"operations": (200, {"operations": []})})
    assert WormholeResolver(fetcher).resolve(Chain.ETHEREUM, "0x" + "1" * 64, "0x" + "11" * 20, "ep") == []


# --------------------------------------------------------------------------- LayerZero

LZ_TX = "0xaf5953b261e677b3e6faffbccdb90e30fcf33433e66d57d3cbea92feff919a52"


def test_layerzero_link_leaves_recipient_to_destination(tmp_path):
    msg = fixture("layerzero_message_arb_to_celo.json")
    # same message, but delivered to a chain this system traces (BSC) — the shape is identical
    msg["data"][0]["pathway"]["receiver"]["chain"] = "bsc"
    fetcher, _ = fetcher_for(tmp_path, {"/v1/messages/tx/" + LZ_TX: (200, msg)})
    [link] = LayerZeroResolver(fetcher).resolve(Chain.ARBITRUM, LZ_TX, "0x8c0e8acb7e813b623282a478624e32f437fc6dd6", "ep")
    assert link.rule == "X-LAYERZERO" and link.to_chain is Chain.BSC and link.status == "DELIVERED"
    assert link.to_address == "" and link.recipient_exclude == ("0xf10e161027410128e63e75d0200fb6d34b2db243",)
    assert link.to_tx == "0x98c9893f139b9afa68a39f9213148419635bcf10eb627e4e324c617667506e79"


def test_layerzero_404_is_no_link_and_replays(tmp_path):
    fetcher, store = fetcher_for(tmp_path, {})
    tx = "0x" + "1" * 64
    assert LayerZeroResolver(fetcher).resolve(Chain.ETHEREUM, tx, "0x" + "11" * 20, "ep") == []
    # the 404 itself is evidence: replay gives the same answer without network
    assert LayerZeroResolver(ReplayFetcher(store)).resolve(Chain.ETHEREUM, tx, "0x" + "11" * 20, "ep") == []
    with pytest.raises(EvidenceMissing):
        LayerZeroResolver(ReplayFetcher(store)).resolve(Chain.ETHEREUM, "0x" + "2" * 64, "0x" + "11" * 20, "ep")


def test_unaccepted_error_status_still_fails(tmp_path):
    fetcher, _ = fetcher_for(tmp_path, {"operations": (403, {"error": "forbidden"})})
    with pytest.raises(SourceError):
        WormholeResolver(fetcher).resolve(Chain.ETHEREUM, "0x" + "1" * 64, "0x" + "11" * 20, "ep")


def _lz_link(**kw) -> CrossChainLink:
    base = dict(
        protocol="layerzero", rule="X-LAYERZERO", from_chain=Chain.ARBITRUM, from_tx=LZ_TX, from_address="0x" + "11" * 20,
        endpoint_id="ep", to_chain=Chain.BSC, to_chain_code="bsc", to_address="", to_tx="0x" + "d" * 64, asset_in="USDT0",
        asset_out="USDT0", amount_out="", memo="", status="DELIVERED", evidence_id="e", recipient_exclude=("0x" + "f1" * 20,),
    )
    return CrossChainLink(**(base | kw))


def test_recipient_from_destination(registry):
    usdt = registry.token(Chain.BSC, "0x55d398326f99059ff775485246999027b3197955", "", 0)
    zero, oapp, user, other = "0x" + "0" * 40, "0x" + "f1" * 20, "0x" + "a1" * 20, "0x" + "b2" * 20
    link = _lz_link()
    mint = xfer(usdt, zero, user, 10**18, 0, name="m")
    via_oapp = xfer(usdt, oapp, user, 10**18, 0, name="o")
    assert recipient_from_destination(link, [mint])[0] == user
    assert recipient_from_destination(link, [xfer(usdt, zero, oapp, 1, 0, name="x"), via_oapp])[0] == user
    who, why = recipient_from_destination(link, [mint, xfer(usdt, zero, other, 1, 0, name="y")])
    assert who is None and "2 candidate receivers" in why
    assert recipient_from_destination(link, [])[0] is None


def test_engine_resolves_layerzero_recipient_from_destination_tx(registry, real_directory):
    usdt = registry.token(Chain.BSC, "0x55d398326f99059ff775485246999027b3197955", "", 0)
    user = "0x" + "a1" * 20
    dest_tx = xfer(usdt, "0x" + "0" * 40, user, 5 * 10**18, 10, name="dest")
    link = _lz_link(to_tx=dest_tx.tx_hash)
    engine = Engine(DataMode.SYNTHETIC, LabelStore([]), registry, real_directory, sources={Chain.BSC: MemorySource(Chain.BSC, [dest_tx])})
    done = engine._confirm_destination(link)
    assert done.to_address == user and done.destination_confirmed is True
    assert "unique token receiver" in done.recipient_basis and done.amount_out
    ambiguous = MemorySource(Chain.BSC, [dest_tx, xfer(usdt, "0x" + "0" * 40, "0x" + "b2" * 20, 1, 10, name="dest")])
    engine2 = Engine(DataMode.SYNTHETIC, LabelStore([]), registry, real_directory, sources={Chain.BSC: ambiguous})
    unresolved = engine2._confirm_destination(_lz_link(to_tx=dest_tx.tx_hash))
    assert unresolved.to_address == "" and unresolved.destination_confirmed is None and "not resolved" in unresolved.destination_detail


# --------------------------------------------------------------------------- Across

ACROSS_TX = "0x09a412f5cf79caf6521542d1a45237133210cddac97171e51a038477a585e966"


def test_across_deposit_link(tmp_path):
    dep = fixture("across_deposit_soneium_to_base.json")
    # Soneium (1868) is not a traced chain; the same record with Base as origin exercises the lookup
    dep["deposit"]["originChainId"] = "10"
    fetcher, _ = fetcher_for(tmp_path, {"depositTxnRef=" + ACROSS_TX: (200, dep)})
    [link] = AcrossResolver(fetcher).resolve(Chain.OPTIMISM, ACROSS_TX, "0xa6d51779b45bdf1c5b6bd0fe86be2c2d9481e69d", "ep")
    assert link.rule == "X-ACROSS" and link.to_chain is Chain.BASE and link.status == "filled"
    assert link.to_address == "0xa6d51779b45bdf1c5b6bd0fe86be2c2d9481e69d" and link.recipient_basis.startswith("Across deposit record")
    assert link.to_tx == "0xa56fb5869ca479b144146244a3d765c1b504ae544e340787a848342a280c0ae6"
    assert link.amount_out == "19969571" and link.asset_out == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    # a different origin chain for the same hash is not this deposit
    assert AcrossResolver(fetcher).resolve(Chain.ARBITRUM, ACROSS_TX, "0x" + "11" * 20, "ep") == []


def test_across_refund_and_404(tmp_path):
    dep = fixture("across_deposit_soneium_to_base.json")
    dep["deposit"] |= {"originChainId": "10", "status": "refunded"}
    fetcher, _ = fetcher_for(tmp_path, {"depositTxnRef=" + ACROSS_TX: (200, dep)})
    assert AcrossResolver(fetcher).resolve(Chain.OPTIMISM, ACROSS_TX, "0x" + "11" * 20, "ep") == []
    assert AcrossResolver(fetcher).resolve(Chain.OPTIMISM, "0x" + "1" * 64, "0x" + "11" * 20, "ep") == []


# --------------------------------------------------------------------------- THORChain


def test_thorchain_resolver_on_captured_midgard_actions(tmp_path):
    actions = fixture("midgard_btc.json")
    fetcher, _ = fetcher_for(tmp_path, {"/v2/actions": (200, actions)})
    r = ThorchainResolver(fetcher, base_url="https://midgard.test")
    btc_in = "847b8cf4c11350cc54189a0098491dfbcd8b9047b29130a3559851e531ebad13"
    [to_tron] = r.resolve(Chain.BITCOIN, btc_in, "bc1qexample", "ep")
    assert to_tron.to_chain is Chain.TRON and to_tron.to_address == "TVEayVhxeoS3YVT9NVzHnfN2mBp9afYSYn"
    assert to_tron.to_tx == "0e680ca6df9ef6dbc4409deab88756413fe198ee53404d1c3e7bdb542696600e" and "memo" in to_tron.recipient_basis
    # an internal leg (THOR.RUNE) never leaves THORChain and is skipped
    ltc_in = "13f912a4791843a4c3ba711bebe34172d6280cbee937b954c02e333788b36fc9"
    assert [l.to_chain for l in r.resolve(Chain.BITCOIN, ltc_in, "x", "ep")] == []  # LTC inbound tx is not a BTC tx in this record


def test_thorchain_trade_asset_swap_has_no_external_leg(tmp_path):
    fetcher, _ = fetcher_for(tmp_path, {"/v2/actions": (200, fixture("midgard_swap.json"))})
    tx = "6183937b021f6e4366268cd1b84d3386c321c9ca10acc8fcb050ed31180ed338"
    assert ThorchainResolver(fetcher, base_url="https://midgard.test").resolve(Chain.ETHEREUM, tx, "0x" + "11" * 20, "ep") == []


def test_thorchain_inbound_labels():
    raw = (FIX / "thorchain_inbound_addresses.json").read_bytes()
    labels, skipped = parse_thorchain_inbound(raw, "https://thornode.test/thorchain/inbound_addresses", date(2026, 10, 3))
    by = {(l.chain, l.text.split(" (")[0]) for l in labels}
    assert (Chain.BITCOIN, "THORChain inbound vault") in by and (Chain.BASE, "THORChain router contract") in by
    assert all(l.category is Category.BRIDGE and l.source_class is SourceClass.CURATED and l.entity_id == "thorchain" for l in labels)
    assert all(l.as_of == date(2026, 10, 3) and l.dataset_ref.startswith("thornode/inbound_addresses@sha256:") for l in labels)
    assert any(k.startswith("chain ") for k in skipped)  # e.g. BCH, DOGE, LTC — not traced here


def test_resolvers_declare_their_chains():
    assert Chain.BITCOIN in ThorchainResolver.chains and Chain.BITCOIN not in WormholeResolver.chains
    assert Chain.TRON in LayerZeroResolver.chains and Chain.SOLANA in WormholeResolver.chains
    assert Chain.BITCOIN not in AcrossResolver.chains and Chain.SOLANA not in AcrossResolver.chains
