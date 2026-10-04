"""Chain adapters against payloads shaped exactly like the real APIs (captured 2026-09-30 from
TronGrid and Esplora; Etherscan per its published samples), served through httpx.MockTransport.
Includes the end-to-end guarantee: a live run replayed from its evidence reproduces the findings hash."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from anveshak import demo
from anveshak.addresses import tron_to_hex
from anveshak.case import CaseRequest, DataMode, Engine, Subject, findings_hash
from anveshak.chain import Chain
from anveshak.chains.base import TRANSFER_TOPIC, VerificationStatus
from anveshak.chains.bitcoin import EsploraSource
from anveshak.chains.evm import EtherscanSource
from anveshak.chains.tron import TronGridSource
from anveshak.domain import Category, SourceClass, TransferKind
from anveshak.errors import SourceError
from anveshak.evidence import EvidenceStore, LiveFetcher, ReplayFetcher
from anveshak.labels.store import LabelStore

from .conftest import label

USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TS = 1788250000000  # ms
S, A, B, C, HOT = (demo.tron_addr(n) for n in ("ad-s", "ad-a", "ad-b", "ad-c", "ad-hot"))
TX1, TX2, TX3, TX4 = (f"{i:064x}" for i in (0xA1, 0xA2, 0xA3, 0xA4))


def _h(addr: str) -> str:
    return tron_to_hex(addr)


def trc20_row(tx, frm, to, value, ts, contract=USDT_TRON, symbol="USDT"):
    return {"transaction_id": tx, "token_info": {"symbol": symbol, "address": contract, "decimals": 6, "name": "Tether USD"},
            "block_timestamp": ts, "from": frm, "to": to, "type": "Transfer", "value": str(value)}


def native_row(tx, frm, to, amount, ts, block, ok=True, kind="TransferContract"):
    return {"ret": [{"contractRet": "SUCCESS" if ok else "REVERT", "fee": 0}], "txID": tx, "blockNumber": block, "block_timestamp": ts,
            "raw_data": {"contract": [{"parameter": {"value": {"amount": amount, "owner_address": _h(frm), "to_address": _h(to)}}, "type": kind}]}}


def txinfo(tx, frm, to, value, ts, block, contract=USDT_TRON):
    return {"id": tx, "blockNumber": block, "blockTimeStamp": ts, "contract_address": _h(contract), "receipt": {"result": "SUCCESS"},
            "log": [{"address": _h(contract)[2:], "topics": [TRANSFER_TOPIC, "0" * 24 + _h(frm)[2:], "0" * 24 + _h(to)[2:]], "data": f"{value:064x}"}]}


class TronFake:
    """Minimal TronGrid: S receives 100 USDT from A, pays 60 USDT to HOT, gets a fake token and TRX."""

    def __init__(self, tamper_amount: bool = False):
        self.tamper = tamper_amount
        self.trc20 = {
            S: [trc20_row(TX1, A, S, 100_000_000, TS), trc20_row(TX2, S, HOT, 60_000_000, TS + 60_000),
                trc20_row(TX3, B, S, 5_000_000_000, TS + 1000, contract=demo.tron_addr("ad-fake"), symbol="USDT")],
            A: [trc20_row(TX1, A, S, 100_000_000, TS)],
            HOT: [trc20_row(TX2, S, HOT, 60_000_000, TS + 60_000)],
        }
        self.native = {S: [native_row(TX4, C, S, 5_000_000, TS + 2000, 900), native_row("f" * 64, S, C, 1, TS + 3000, 901, ok=False)]}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        if path.startswith("/v1/accounts/") and path.endswith("/transactions/trc20"):
            return httpx.Response(200, json={"data": self.trc20.get(path.split("/")[3], []), "success": True, "meta": {"page_size": 3}})
        if path.startswith("/v1/accounts/") and path.endswith("/transactions"):
            return httpx.Response(200, json={"data": self.native.get(path.split("/")[3], []), "success": True, "meta": {}})
        if path.startswith("/v1/accounts/"):
            return httpx.Response(200, json={"data": [{"balance": 0, "trc20": [{USDT_TRON: "0"}]}], "success": True})
        if path == "/wallet/gettransactioninfobyid":
            tx = json.loads(request.content)["value"]
            if tx == TX2:
                return httpx.Response(200, json=txinfo(TX2, S, HOT, 60_000_000 + (1 if self.tamper else 0), TS + 60_000, 1020))
            if tx == TX1:
                return httpx.Response(200, json=txinfo(TX1, A, S, 100_000_000, TS, 1000))
            if tx == TX4:
                return httpx.Response(200, json={"id": TX4, "blockNumber": 900, "blockTimeStamp": TS + 2000})
        if path == "/wallet/gettransactionbyid":
            return httpx.Response(200, json=native_row(TX4, C, S, 5_000_000, TS + 2000, 900))
        return httpx.Response(404)


def _tron(tmp_path, registry, fake=None):
    fetcher = LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(fake or TronFake())))
    return TronGridSource(fetcher, registry, base_url="https://api.trongrid.test"), fetcher


def test_tron_history_parsing(tmp_path, registry):
    src, _ = _tron(tmp_path, registry)
    h = src.history(S)
    by_tx = {t.tx_hash: t for t in h.transfers}
    assert set(by_tx) == {TX1, TX2, TX3, TX4}  # failed native transfer skipped
    assert by_tx[TX2].asset.verified and by_tx[TX2].block_number is None
    assert not by_tx[TX3].asset.verified and by_tx[TX3].asset.symbol == "USDT?"
    assert by_tx[TX4].kind is TransferKind.NATIVE and by_tx[TX4].sender == C and by_tx[TX4].block_number == 900


def test_tron_verification(tmp_path, registry):
    src, _ = _tron(tmp_path, registry)
    t = {t.tx_hash: t for t in src.history(S).transfers}
    ok = src.verify(t[TX2])
    assert ok.status is VerificationStatus.VERIFIED and ok.block_number == 1020
    assert src.verify(t[TX4]).status is VerificationStatus.VERIFIED
    bad_src, _ = _tron(tmp_path / "b", registry, TronFake(tamper_amount=True))
    bad = bad_src.verify(t[TX2])
    assert bad.status is VerificationStatus.MISMATCH and "transfer_event" in bad.detail


def test_live_run_replays_to_identical_findings_hash(tmp_path, registry, real_directory, settings):
    labels = LabelStore([label(Chain.TRON, HOT, SourceClass.ENTITY_ATTESTED, "https://www.binance.com/en/blog/por", entity="binance", category=Category.EXCHANGE)])
    store = EvidenceStore(settings.evidence_dir)
    live_fetcher = LiveFetcher(store, client=httpx.Client(transport=httpx.MockTransport(TronFake())))
    request = CaseRequest(case_reference="T-1", subjects=(Subject(chain=Chain.TRON, address=S),))
    live = Engine(DataMode.LIVE, labels, registry, real_directory, settings=settings, fetcher=live_fetcher).run(request)
    assert any(d.target_entity_id == "binance" and d.status.value == "ready_for_approval" for d in live.findings.routing)

    replayed = Engine(DataMode.REPLAY, labels, registry, real_directory, settings=settings, fetcher=ReplayFetcher(store)).run(request)
    normalised = replayed.findings.model_copy(update={"data_mode": DataMode.LIVE})
    assert findings_hash(normalised) == live.findings_hash
    assert live.findings.evidence_ids  # the findings cite stored evidence


# --------------------------------------------------------------------------- Etherscan

E_S, E_HOT, E_X = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
USDT_ETH = "0xdac17f958d2ee523a2206206994597c13d831ec7"
ETX1, ETX2 = "0x" + "a" * 64, "0x" + "b" * 64


class EtherscanFake:
    def __init__(self, rate_limit_first: bool = False):
        self.rate_limited = rate_limit_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        assert q["apikey"] == "KEY" and q["chainid"] == "1"
        if self.rate_limited:
            self.rate_limited = False
            return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Max rate limit reached"})
        action = q["action"]
        if action == "txlist":
            rows = [
                {"blockNumber": "100", "timeStamp": "1788250000", "hash": ETX1, "from": E_S, "to": E_X, "value": "2000000000000000000", "isError": "0", "txreceipt_status": "1", "transactionIndex": "3"},
                {"blockNumber": "101", "timeStamp": "1788250012", "hash": "0x" + "c" * 64, "from": E_S, "to": E_X, "value": "5", "isError": "1", "txreceipt_status": "0"},
                {"blockNumber": "102", "timeStamp": "1788250024", "hash": "0x" + "d" * 64, "from": E_S, "to": USDT_ETH, "value": "0", "isError": "0", "txreceipt_status": "1"},
            ]
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows})
        if action == "txlistinternal":
            return httpx.Response(200, json={"status": "0", "message": "No transactions found", "result": []})
        if action == "tokentx":
            rows = [{"blockNumber": "105", "timeStamp": "1788250060", "hash": ETX2, "from": E_S, "contractAddress": USDT_ETH, "to": E_HOT, "value": "750000000", "tokenName": "Tether USD", "tokenSymbol": "USDT", "tokenDecimal": "6", "transactionIndex": "9"}]
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows})
        if action == "eth_getTransactionReceipt":
            if q["txhash"] == ETX2:
                log = {"address": USDT_ETH, "topics": ["0x" + TRANSFER_TOPIC, "0x" + "0" * 24 + E_S[2:], "0x" + "0" * 24 + E_HOT[2:]], "data": hex(750000000)}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"status": "0x1", "blockNumber": hex(105), "logs": [log]}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"status": "0x1", "blockNumber": hex(100), "logs": []}})
        if action == "eth_getTransactionByHash":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"from": E_S, "to": E_X, "value": hex(2 * 10**18), "blockNumber": hex(100)}})
        if action == "eth_getBlockByNumber":
            ts = {"0x64": 1788250000, "0x69": 1788250060}[q["tag"]]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(ts)}})
        if action == "eth_getCode":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x"})
        if action == "eth_getTransactionCount":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x3"})
        return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Invalid action"})


def _eth(tmp_path, registry, fake=None, monkeypatch=None):
    fetcher = LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(fake or EtherscanFake())))
    return EtherscanSource(Chain.ETHEREUM, fetcher, registry, api_key="KEY", base_url="https://api.etherscan.test/v2/api")


def test_etherscan_history_and_verification(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("anveshak.chains.evm.time.sleep", lambda s: None)
    src = _eth(tmp_path, registry, EtherscanFake(rate_limit_first=True))
    h = src.history(E_S)
    assert [t.tx_hash for t in h.transfers] == [ETX1, ETX2]  # failed and zero-value txs skipped
    native, token = h.transfers
    assert native.amount == 2 * 10**18 and token.asset.symbol == "USDT" and token.asset.verified
    assert src.verify(token).status is VerificationStatus.VERIFIED
    assert src.verify(native).status is VerificationStatus.VERIFIED
    assert src.is_contract(E_HOT) is False
    assert src.tx_count(E_S) == 3  # proxy eth_getTransactionCount (R-BUSY-ACCOUNT)


def test_etherscan_error_is_surfaced(tmp_path, registry):
    def invalid_key(request):
        return httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "Invalid API Key"})

    with pytest.raises(SourceError, match="Invalid API Key"):
        _eth(tmp_path, registry, invalid_key).history(E_S)


def test_etherscan_requires_key(registry):
    from anveshak.errors import ConfigError

    with pytest.raises(ConfigError):
        EtherscanSource(Chain.ETHEREUM, None, registry, api_key="")


# --------------------------------------------------------------------------- Esplora

BA, BX, BY, BZ, BW = (demo.btc_addr(n) for n in ("ad-a", "ad-x", "ad-y", "ad-z", "ad-w"))


def esplora_tx(txid, vin, vout, height, ts):
    return {"txid": txid, "vin": [{"is_coinbase": False, "prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
            "vout": [{"scriptpubkey_address": a, "value": v} if a else {"scriptpubkey_type": "op_return", "value": 0} for a, v in vout],
            "status": {"confirmed": True, "block_height": height, "block_time": ts}}


def test_esplora_utxo_model(tmp_path, registry):
    tx_in = esplora_tx("01" * 32, [(BX, 50_000)], [(BA, 40_000)], 800_000, 1788250000)
    tx_out = esplora_tx("02" * 32, [(BA, 40_000), (BW, 10_000)], [(BY, 30_000), (BZ, 19_000), (None, 0)], 800_010, 1788256000)

    def handler(request):
        path = urlsplit(str(request.url)).path
        if path == f"/api/address/{BA}":
            return httpx.Response(200, json={"address": BA, "chain_stats": {"tx_count": 2}})
        if path == f"/api/address/{BA}/txs/chain":
            return httpx.Response(200, json=[tx_out, tx_in])
        if path == f"/api/tx/{'02' * 32}":
            return httpx.Response(200, json=tx_out)
        return httpx.Response(404)

    src = EsploraSource(LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler))), registry, base_url="https://esplora.test/api")
    h = src.history(BA)
    pairs = {(t.sender, t.receiver, t.amount) for t in h.transfers}
    assert pairs == {(BX, BA, 40_000), (BA, BY, 30_000), (BA, BZ, 19_000)}
    out = next(t for t in h.transfers if t.receiver == BY)
    assert out.utxo.input_addresses == tuple(sorted({BA, BW})) and not out.utxo.coinjoin_like
    assert src.verify(out).status is VerificationStatus.VERIFIED
    assert h.complete
