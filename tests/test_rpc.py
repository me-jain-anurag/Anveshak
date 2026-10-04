"""Keyless EVM source: window-limited eth_getLogs scan over plain JSON-RPC (ADR-0021).

The fake node mimics what public endpoints were observed to do on 2026-10-03: early blocks
pruned ("pruned history unavailable"), a cap on the block range per eth_getLogs call, and
log objects shaped like the captured bsc-rpc.publicnode.com response (incl. blockTimestamp).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from anveshak.case import CaseRequest, DataMode, Engine, Subject, chains_needing_since, evm_backend, findings_hash
from anveshak.chain import Chain
from anveshak.chains.base import TRANSFER_TOPIC, VerificationStatus
from anveshak.chains.rpc import RpcLogSource
from anveshak.domain import Category, SourceClass
from anveshak.errors import ConfigError, SourceError
from anveshak.evidence import EvidenceStore, LiveFetcher, ReplayFetcher, canonical_json, redact_endpoint, sha256_hex
from anveshak.labels.store import LabelStore

from .conftest import label

RPC = "https://bsc.rpc.test"
USDT_BSC = "0x55d398326f99059ff775485246999027b3197955"
FAKE_TOKEN = "0x" + "fe" * 20
S, HOT, X = "0x" + "51" * 20, "0x" + "52" * 20, "0x" + "53" * 20
T0 = 1_780_000_000  # timestamp of block 0
BLOCK_TIME = 3
LATEST = 300_000
PRUNED_BELOW = 120_000


def ts_of(block: int) -> int:
    return T0 + BLOCK_TIME * block


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def log(tx: str, block: int, index: int, frm: str, to: str, amount: int, contract: str = USDT_BSC, with_ts: bool = True) -> dict:
    row = {
        "address": contract,
        "topics": ["0x" + TRANSFER_TOPIC, _topic(frm), _topic(to)],
        "data": "0x" + f"{amount:064x}",
        "blockNumber": hex(block),
        "transactionHash": tx,
        "transactionIndex": "0x1",
        "logIndex": hex(index),
        "removed": False,
    }
    if with_ts:
        row["blockTimestamp"] = hex(ts_of(block))
    return row


TX_IN, TX_OUT, TX_FAKE, TX_OLD = ("0x" + c * 64 for c in "abcd")
START_BLOCK = 150_000


class FakeNode:
    def __init__(self, max_range: int = 2000, tamper: bool = False):
        self.max_range = max_range
        self.tamper = tamper
        self.calls: list[str] = []
        self.logs = [
            log(TX_OLD, START_BLOCK - 10, 0, X, S, 7 * 10**18),  # before the window: must not appear
            log(TX_IN, START_BLOCK + 100, 3, X, S, 500 * 10**18),
            log(TX_OUT, START_BLOCK + 4_321, 0, S, HOT, 450 * 10**18, with_ts=False),
            log(TX_FAKE, START_BLOCK + 50, 1, X, S, 10**24, contract=FAKE_TOKEN),  # not a registry contract
        ]

    def _result(self, method: str, params: list):
        if method == "eth_blockNumber":
            return hex(LATEST)
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            if n < PRUNED_BELOW:
                return {"__error__": {"code": -32000, "message": f"pruned history unavailable: earliest available {PRUNED_BELOW}"}}
            return {"number": params[0], "timestamp": hex(ts_of(n))}
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if hi - lo + 1 > self.max_range:
                return {"__error__": {"code": -32005, "message": f"block range is too large, max {self.max_range}"}}
            topics = f["topics"]
            out = []
            for row in self.logs:
                b = int(row["blockNumber"], 16)
                if not lo <= b <= hi or row["address"] not in f["address"]:
                    continue
                if all(t is None or t == row["topics"][i] for i, t in enumerate(topics)):
                    out.append(row)
            return out
        if method == "eth_getTransactionReceipt":
            for row in self.logs:
                if row["transactionHash"] == params[0]:
                    r = dict(row)
                    if self.tamper:
                        r["data"] = "0x" + f"{int(row['data'], 16) + 1:064x}"
                    return {"transactionHash": params[0], "blockNumber": row["blockNumber"], "status": "0x1", "logs": [r]}
            return None
        if method == "eth_getCode":
            return "0x6080" if params[0] == HOT else "0x"
        if method == "eth_getTransactionCount":
            return "0x2" if params[0] == S else "0x0"
        if method == "eth_getBalance":
            return "0x0"
        if method == "eth_call":
            return hex(5) if params[0]["to"] == USDT_BSC and params[0]["data"].endswith(HOT[2:]) else "0x0"
        raise AssertionError(method)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(RPC)
        body = json.loads(request.content)
        self.calls.append(body["method"])
        result = self._result(body["method"], body["params"])
        if isinstance(result, dict) and "__error__" in result:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": result["__error__"]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _src(tmp_path, registry, node=None, hours=72, start_block=START_BLOCK, **kw):
    node = node or FakeNode()
    fetcher = LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(node)))
    since = datetime.fromtimestamp(ts_of(start_block), tz=timezone.utc)
    return RpcLogSource(Chain.BSC, fetcher, registry, rpc_url=RPC, window_start=since, window_hours=hours, **kw), node


def test_window_search_is_exact_despite_pruned_history(tmp_path, registry):
    src, node = _src(tmp_path, registry)
    first, last, end = src._window()
    assert first == START_BLOCK
    assert last == START_BLOCK + 72 * 3600 // BLOCK_TIME
    assert end == datetime.fromtimestamp(ts_of(last), tz=timezone.utc)
    # one block in between, not on a boundary: the first block at or after the time
    mid = datetime.fromtimestamp(ts_of(START_BLOCK) + 1, tz=timezone.utc)
    assert src._first_block_at_or_after(int(mid.timestamp()), LATEST) == START_BLOCK + 1
    assert node.calls.count("eth_getBlockByNumber") < 60


def test_window_is_capped_at_latest_block(tmp_path, registry):
    src, _ = _src(tmp_path, registry, start_block=LATEST - 100)
    first, last, end = src._window()
    assert (first, last) == (LATEST - 100, LATEST)
    assert end == datetime.fromtimestamp(ts_of(LATEST), tz=timezone.utc)


def test_window_before_pruned_horizon_names_the_fix(tmp_path, registry):
    src, _ = _src(tmp_path, registry, start_block=PRUNED_BELOW - 5_000)
    with pytest.raises(SourceError, match="ANVESHAK_RPC_BSC"):
        src.history(S)


def test_history_is_window_limited_and_registry_only(tmp_path, registry):
    src, node = _src(tmp_path, registry)
    h = src.history(S)
    by_tx = {t.tx_hash: t for t in h.transfers}
    assert set(by_tx) == {TX_IN, TX_OUT}  # TX_OLD before the window, TX_FAKE not a registry token
    assert by_tx[TX_OUT].sender == S and by_tx[TX_OUT].receiver == HOT and by_tx[TX_OUT].amount == 450 * 10**18
    assert by_tx[TX_OUT].asset.verified and by_tx[TX_OUT].asset.decimals == 18
    assert by_tx[TX_OUT].timestamp == datetime.fromtimestamp(ts_of(START_BLOCK + 4_321), tz=timezone.utc)  # via block lookup
    assert h.complete and h.window_start is not None and h.window_end is not None
    assert "native-coin transfers not visible" in h.note and "window-limited" in h.note
    # ranges above the node's cap were halved automatically, never silently skipped
    assert node.calls.count("eth_getLogs") > 2 * (72 * 3600 // BLOCK_TIME) // 5000


def test_request_budget_is_declared(tmp_path, registry):
    src, _ = _src(tmp_path, registry, max_requests=3)
    h = src.history(S)
    assert not h.complete and "budget" in h.note


def test_rpc_verification_and_tamper(tmp_path, registry):
    src, _ = _src(tmp_path, registry)
    t = {t.tx_hash: t for t in src.history(S).transfers}
    ok = src.verify(t[TX_OUT])
    assert ok.status is VerificationStatus.VERIFIED and ok.block_number == START_BLOCK + 4_321
    bad_src, _ = _src(tmp_path / "b", registry, FakeNode(tamper=True))
    bad = bad_src.verify(t[TX_OUT])
    assert bad.status is VerificationStatus.MISMATCH and "transfer_event" in bad.detail


def test_rpc_extras(tmp_path, registry):
    src, _ = _src(tmp_path, registry)
    assert src.is_contract(HOT) is True and src.is_contract(S) is False
    assert src.has_activity(S) is True  # nonce 2
    assert src.has_activity(HOT) is True  # USDT balance via balanceOf
    assert src.has_activity(X) is False
    assert src.tx_count(S) == 2 and src.tx_count(HOT) == 0  # nonce: transactions sent (R-BUSY-ACCOUNT)
    moved = src.tx_transfers(TX_OUT)
    assert [(m.sender, m.receiver) for m in moved] == [(S, HOT)]
    assert src.tx_transfers("0x" + "0" * 64) == []


def test_backend_selection(settings):
    assert evm_backend(settings, Chain.ETHEREUM) == "etherscan"  # free tier, key present
    assert evm_backend(settings, Chain.BSC) == "rpc"  # not on the free tier
    paid = replace(settings, etherscan_paid=True)
    assert evm_backend(paid, Chain.BSC) == "etherscan"
    assert chains_needing_since(settings, [Chain.BSC, Chain.TRON, Chain.ETHEREUM]) == [Chain.BSC]


def test_engine_requires_since_and_replays_exactly(tmp_path, registry, real_directory, settings):
    settings = replace(settings, evm_rpc_urls={**settings.evm_rpc_urls, "bsc": RPC})
    labels = LabelStore([label(Chain.BSC, HOT, SourceClass.ENTITY_ATTESTED, "https://www.binance.com/en/blog/por", entity="binance", category=Category.EXCHANGE)])
    store = EvidenceStore(settings.evidence_dir)
    fetcher = LiveFetcher(store, client=httpx.Client(transport=httpx.MockTransport(FakeNode())))
    no_since = CaseRequest(case_reference="R-0", subjects=(Subject(chain=Chain.BSC, address=S),))
    with pytest.raises(ConfigError, match="since"):
        Engine(DataMode.LIVE, labels, registry, real_directory, settings=settings, fetcher=fetcher).source(Chain.BSC).history(S)
    since = datetime.fromtimestamp(ts_of(START_BLOCK), tz=timezone.utc) + timedelta(seconds=1)
    request = no_since.model_copy(update={"since": since, "case_reference": "R-1"})
    live = Engine(DataMode.LIVE, labels, registry, real_directory, settings=settings, fetcher=fetcher).run(request)
    assert any(d.target_entity_id == "binance" for d in live.findings.routing)
    assert live.findings.traces[0].coverage.windowed_histories
    replayed = Engine(DataMode.REPLAY, labels, registry, real_directory, settings=settings, fetcher=ReplayFetcher(store)).run(request)
    assert findings_hash(replayed.findings.model_copy(update={"data_mode": DataMode.LIVE})) == live.findings_hash


KEY = "9f2c1e4b7a6d4c3e8b1a0f9e8d7c6b5a"


def test_endpoint_credentials_are_redacted():
    assert redact_endpoint(f"https://mainnet.infura.io/v3/{KEY}") == "https://mainnet.infura.io/v3/<redacted>"
    assert redact_endpoint("https://eth-mainnet.g.alchemy.com/v2/AbCdEfGhIjKlMnOpQrStUvWx") == "https://eth-mainnet.g.alchemy.com/v2/<redacted>"
    assert redact_endpoint(f"https://name.quiknode.pro/{KEY}/") == "https://name.quiknode.pro/<redacted>/"
    assert redact_endpoint("https://user:pa55@node.test/rpc?token=abc&x=1") == "https://<redacted>@node.test/rpc?token=<redacted>&x=1"
    for public in ("https://rpc.mevblocker.io", "https://arb1.arbitrum.io/rpc", "https://api.mainnet-beta.solana.com",
                   "https://api.trongrid.io/wallet/gettransactioninfobyid"):  # long method names are not tokens
        assert redact_endpoint(public) == public  # unchanged, so earlier evidence keeps its request keys
    once = redact_endpoint(f"https://u:p@node.test/v3/{KEY}?apikey=k")
    assert redact_endpoint(once) == once


def test_replay_uses_recorded_source_config_and_keys_never_reach_disk(tmp_path, registry, real_directory, settings):
    keyed = f"https://bsc.rpc.test/v3/{KEY}"
    live_settings = replace(settings, evm_rpc_urls={**settings.evm_rpc_urls, "bsc": keyed}, logscan_window_hours=96)
    labels = LabelStore([label(Chain.BSC, HOT, SourceClass.ENTITY_ATTESTED, "https://www.binance.com/en/blog/por", entity="binance", category=Category.EXCHANGE)])
    store = EvidenceStore(settings.evidence_dir)
    fetcher = LiveFetcher(store, client=httpx.Client(transport=httpx.MockTransport(FakeNode())))
    since = datetime.fromtimestamp(ts_of(START_BLOCK), tz=timezone.utc) + timedelta(seconds=1)
    request = CaseRequest(case_reference="R-2", subjects=(Subject(chain=Chain.BSC, address=S),), since=since)
    live = Engine(DataMode.LIVE, labels, registry, real_directory, settings=live_settings, fetcher=fetcher).run(request)
    recorded = live.findings.source_config
    assert [(c.chain, c.backend, c.endpoint, c.window_hours) for c in recorded.chains] == [(Chain.BSC, "rpc", "https://bsc.rpc.test/v3/<redacted>", 96)]
    assert KEY not in live.model_dump_json()
    from anveshak.report import render_report

    html = render_report(live)
    assert "JSON-RPC log scan" in html and "https://bsc.rpc.test/v3/&lt;redacted&gt;" in html and "96-hour window" in html and KEY not in html
    assert not any(KEY in f.read_text(encoding="utf-8", errors="ignore") for f in settings.evidence_dir.rglob("*") if f.is_file())

    # A machine configured differently (here: it would pick Etherscan for BSC, with a 24-hour window)
    # replays exactly, because the recorded configuration decides which requests are replayed.
    other = replace(settings, etherscan_paid=True, logscan_window_hours=24)
    assert evm_backend(other, Chain.BSC) == "etherscan"
    replayed = Engine(DataMode.REPLAY, labels, registry, real_directory, settings=other, fetcher=ReplayFetcher(store), source_config=recorded).run(request)
    assert findings_hash(replayed.findings.model_copy(update={"data_mode": DataMode.LIVE})) == live.findings_hash

    # Findings recorded before ADR-0025 have no source configuration and keep their original hash.
    legacy = live.findings.model_copy(update={"source_config": None})
    data = legacy.model_dump(mode="json")
    data.pop("source_config")
    assert findings_hash(legacy) == sha256_hex(canonical_json(data))
