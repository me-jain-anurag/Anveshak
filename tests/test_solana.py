"""Solana adapter against JSON-RPC payloads shaped like live mainnet-beta responses (2026-09-30)."""

from __future__ import annotations

import json

import httpx

from anveshak.addresses import b58encode, ed25519_on_curve, solana_associated_token_account, solana_is_program_derived
from anveshak.chain import Chain
from anveshak.chains.base import VerificationStatus
from anveshak.chains.solana import SolanaRpcSource
from anveshak.domain import TransferKind
from anveshak.evidence import EvidenceStore, LiveFetcher

USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Real wallet and its real associated token accounts, observed on mainnet (see test below).
WALLET = "CgeCCF1khuPwtXrd9NYXLa1pNEhQVecePfHHHU38REQJ"
WALLET_USDT_ATA = "CxfiXMPWDFkLAzgwoz11ALf2DZJAwCsyJ6Zi6oMJKnLM"


def _key(n: int) -> str:
    """Deterministic on-curve test keys (a wallet must be on the curve)."""
    import hashlib

    i = 0
    while True:
        k = hashlib.sha256(f"sol-test-{n}-{i}".encode()).digest()
        if ed25519_on_curve(k):
            return b58encode(k)
        i += 1


PEER, PEER_ATA = _key(1), _key(2)
SIG_IN, SIG_OUT = "5" * 87, "4" * 87


def test_associated_token_account_derivation_matches_mainnet():
    assert solana_associated_token_account(WALLET, USDT) == WALLET_USDT_ATA
    assert solana_associated_token_account(WALLET, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == "8gTQ3i7gj3vsfzTXXVccpouFJhMa9EfwjWXG8R41SkXT"
    assert solana_is_program_derived(WALLET_USDT_ATA) and not solana_is_program_derived(WALLET)


def _tx(sig, src_acct, dst_acct, src_owner, dst_owner, amount, pre_src, pre_dst, slot, bt, sol_to=None, tamper=0):
    keys = [WALLET, src_acct, dst_acct, TOKEN_PROGRAM, "11111111111111111111111111111111"] + ([sol_to] if sol_to else [])
    ixs = [{"parsed": {"info": {"amount": str(amount), "authority": src_owner, "destination": dst_acct, "source": src_acct}, "type": "transfer"},
            "program": "spl-token", "programId": TOKEN_PROGRAM, "stackHeight": 1}]
    if sol_to:
        ixs.append({"parsed": {"info": {"destination": sol_to, "lamports": 5_000_000, "source": WALLET}, "type": "transfer"},
                    "program": "system", "programId": "11111111111111111111111111111111", "stackHeight": 1})
    def bal(idx, owner, amt):
        return {"accountIndex": idx, "mint": USDT, "owner": owner, "programId": TOKEN_PROGRAM, "uiTokenAmount": {"amount": str(amt), "decimals": 6}}
    return {
        "blockTime": bt, "slot": slot, "version": 0,
        "meta": {"err": None, "innerInstructions": [], "preTokenBalances": [bal(1, src_owner, pre_src), bal(2, dst_owner, pre_dst)],
                 "postTokenBalances": [bal(1, src_owner, pre_src - amount), bal(2, dst_owner, pre_dst + amount + tamper)]},
        "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": k, "signer": i == 0, "source": "transaction", "writable": True} for i, k in enumerate(keys)], "instructions": ixs}},
    }


class RpcFake:
    def __init__(self, tamper: int = 0):
        self.txs = {
            SIG_IN: _tx(SIG_IN, PEER_ATA, WALLET_USDT_ATA, PEER, WALLET, 25_000_000, 90_000_000, 0, 451_000_000, 1790700000),
            SIG_OUT: _tx(SIG_OUT, WALLET_USDT_ATA, PEER_ATA, WALLET, PEER, 20_000_000, 25_000_000, 65_000_000, 451_000_100, 1790700060, sol_to=PEER, tamper=tamper),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "getTokenAccountsByOwner":
            value = [{"pubkey": WALLET_USDT_ATA, "account": {"data": {"parsed": {"info": {"mint": USDT, "owner": WALLET, "tokenAmount": {"amount": "5000000", "decimals": 6}}}}}}] if params[0] == WALLET and params[1]["mint"] == USDT else []
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": value}})
        if method == "getSignaturesForAddress":
            sigs = {WALLET: [SIG_OUT], WALLET_USDT_ATA: [SIG_OUT, SIG_IN]}.get(params[0], [])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [{"signature": s, "slot": self.txs[s]["slot"], "err": None, "blockTime": self.txs[s]["blockTime"]} for s in sigs]})
        if method == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": self.txs.get(params[0])})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}})


def _src(tmp_path, registry, fake):
    return SolanaRpcSource(LiveFetcher(EvidenceStore(tmp_path), client=httpx.Client(transport=httpx.MockTransport(fake))), registry, rpc_url="https://rpc.test")


def test_history_includes_incoming_token_transfers_via_token_account(tmp_path, registry):
    h = _src(tmp_path, registry, RpcFake()).history(WALLET)
    got = {(t.kind, t.sender, t.receiver, t.amount) for t in h.transfers}
    assert (TransferKind.TOKEN, PEER, WALLET, 25_000_000) in got  # only visible through the token account
    assert (TransferKind.TOKEN, WALLET, PEER, 20_000_000) in got
    assert (TransferKind.NATIVE, WALLET, PEER, 5_000_000) in got
    assert all(t.asset.verified for t in h.transfers)


def test_verification_checks_balance_deltas(tmp_path, registry):
    src = _src(tmp_path, registry, RpcFake())
    out = next(t for t in src.history(WALLET).transfers if t.kind is TransferKind.TOKEN and t.sender == WALLET)
    assert src.verify(out).status is VerificationStatus.VERIFIED
    tampered = _src(tmp_path / "t", registry, RpcFake(tamper=1))
    result = tampered.verify(out)
    assert result.status is VerificationStatus.MISMATCH and "balance change" in result.detail


def test_program_derived_receiver_is_a_contract(tmp_path, registry):
    src = _src(tmp_path, registry, RpcFake())
    assert src.is_contract(WALLET_USDT_ATA) is True and src.is_contract(WALLET) is False
    assert src.balance(WALLET, registry.token(Chain.SOLANA, USDT, "USDT", 6)).amount == 5_000_000
