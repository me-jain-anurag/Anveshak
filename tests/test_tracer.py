"""Tracer guarantees: time order, exclusions are counted, limits are reported, endpoints are correct."""

from anveshak import demo
from anveshak.attribution import Attributor
from anveshak.chain import Chain
from anveshak.chains.memory import MemorySource
from anveshak.domain import Category, Direction, EndpointKind, Grade, SourceClass
from anveshak.labels.store import LabelStore
from anveshak.tracer import TraceParams, Tracer

from .conftest import label, xfer

T = demo.tron_addr
U = 1_000_000


def _usdt(registry):
    return registry.token(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT", 6)


def _trace(registry, transfers, labels=(), direction=Direction.OUT, subject=None, **params):
    src = MemorySource(Chain.TRON, transfers, **{k: params.pop(k) for k in ("contracts", "balances", "incomplete") if k in params})
    tracer = Tracer(src, Attributor(LabelStore(list(labels))), registry)
    return tracer.trace(subject or T("s"), TraceParams(direction=direction, **params))


def kinds(result):
    return sorted((e.kind.value, e.address) for e in result.endpoints)


def test_transfer_before_arrival_is_not_followed(registry):
    usdt = _usdt(registry)
    r = _trace(registry, [xfer(usdt, T("h"), T("decoy"), 5 * U, -10), xfer(usdt, T("s"), T("h"), 10 * U, 0), xfer(usdt, T("h"), T("next"), 9 * U, 5)])
    followed = {t.receiver for t in r.transfers}
    assert T("next") in followed and T("decoy") not in followed
    assert r.coverage.excluded_time_order == 1


def test_backward_trace_only_uses_earlier_funding(registry):
    usdt = _usdt(registry)
    r = _trace(
        registry,
        [xfer(usdt, T("funder"), T("s"), 10 * U, 0), xfer(usdt, T("s"), T("out"), 9 * U, 10), xfer(usdt, T("late"), T("funder"), 3 * U, 20)],
        direction=Direction.IN,
    )
    assert (EndpointKind.ORIGIN.value, T("funder")) in kinds(r)
    assert r.coverage.excluded_time_order == 1  # "late" funded the funder only after it paid the subject


def test_dust_unverified_token_and_asset_switch_are_counted_not_followed(registry):
    usdt = _usdt(registry)
    fake = registry.token(Chain.TRON, T("fake-contract"), "USDT", 6)
    trx = registry.native(Chain.TRON)
    r = _trace(
        registry,
        [
            xfer(usdt, T("s"), T("dust"), U // 10, 1),
            xfer(fake, T("s"), T("fake"), 100 * U, 2),
            xfer(usdt, T("s"), T("h"), 50 * U, 3),
            xfer(trx, T("h"), T("trx-out"), 100 * U, 4),  # different asset than what arrived
        ],
    )
    c = r.coverage
    assert (c.excluded_dust, c.excluded_unverified_asset, c.excluded_other_asset) == (1, 1, 1)
    assert not fake.verified and fake.symbol == "USDT?"


def test_service_endpoint_stops_path_and_carries_attribution(registry):
    usdt = _usdt(registry)
    labels = [label(Chain.TRON, T("hot"), SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")]
    r = _trace(registry, [xfer(usdt, T("s"), T("dep"), 10 * U, 1), xfer(usdt, T("dep"), T("hot"), 10 * U, 2), xfer(usdt, T("hot"), T("beyond"), 10 * U, 3)], labels)
    vasp = [e for e in r.endpoints if e.kind is EndpointKind.VASP]
    assert len(vasp) == 1 and vasp[0].address == T("hot") and vasp[0].attribution.grade is Grade.A
    assert vasp[0].adjacent_address == T("dep") and "R-SWEEP" in vasp[0].adjacent_role
    assert T("beyond") not in {t.receiver for t in r.transfers}  # never follow inside a service


def test_sweep_role_not_claimed_when_address_also_pays_elsewhere(registry):
    usdt = _usdt(registry)
    labels = [label(Chain.TRON, T("hot"), SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")]
    r = _trace(
        registry,
        [xfer(usdt, T("s"), T("dep"), 10 * U, 1), xfer(usdt, T("dep"), T("hot"), 6 * U, 2), xfer(usdt, T("dep"), T("friend"), 4 * U, 3)],
        labels,
    )
    vasp = next(e for e in r.endpoints if e.kind is EndpointKind.VASP)
    assert vasp.adjacent_role is None


def test_mixer_is_a_service_endpoint(registry):
    usdt = _usdt(registry)
    labels = [label(Chain.TRON, T("mix"), SourceClass.CURATED, "https://a.example", entity="mx", category=Category.MIXER)]
    r = _trace(registry, [xfer(usdt, T("s"), T("mix"), 10 * U, 1)], labels)
    assert kinds(r) == [(EndpointKind.SERVICE.value, T("mix"))]


def test_hop_limit_and_budget_are_reported(registry):
    usdt = _usdt(registry)
    chain = [xfer(usdt, T(f"n{i}"), T(f"n{i + 1}"), 10 * U, i) for i in range(6)]
    r = _trace(registry, chain, subject=T("n0"), max_hops=3)
    assert (EndpointKind.HOP_LIMIT.value, T("n3")) in kinds(r)
    r2 = _trace(registry, chain, subject=T("n0"), max_hops=10, max_expansions=2)
    assert r2.coverage.budget_exhausted
    assert any(e.kind is EndpointKind.NOT_EXPANDED for e in r2.endpoints)


def test_branch_pruning_keeps_largest_and_is_recorded(registry):
    usdt = _usdt(registry)
    fan = [xfer(usdt, T("s"), T(f"r{i}"), (i + 2) * U, i) for i in range(5)]
    r = _trace(registry, fan, max_branch=2)
    assert r.coverage.pruned[0].candidates == 5 and r.coverage.pruned[0].kept == 2
    assert {t.receiver for t in r.transfers} == {T("r4"), T("r3")}


def test_incomplete_history_becomes_high_activity_endpoint(registry):
    usdt = _usdt(registry)
    r = _trace(registry, [xfer(usdt, T("s"), T("busy"), 10 * U, 1)], incomplete={T("busy")})
    assert (EndpointKind.HIGH_ACTIVITY.value, T("busy")) in kinds(r)


def test_dormant_endpoint_gets_balance(registry):
    usdt = _usdt(registry)
    r = _trace(registry, [xfer(usdt, T("s"), T("park"), 10 * U, 1)], balances={(T("park"), usdt.key): 10 * U})
    assert kinds(r) == [(EndpointKind.DORMANT.value, T("park"))]
    assert r.balances[0].amount == 10 * U


def test_subject_that_never_moved_funds_is_dormant_with_balance(registry):
    usdt = _usdt(registry)
    r = _trace(registry, [xfer(usdt, T("victim"), T("s"), 10 * U, 1)], balances={(T("s"), usdt.key): 10 * U})
    assert kinds(r) == [(EndpointKind.DORMANT.value, T("s"))]
    assert r.endpoints[0].hops == 0 and r.balances[0].address == T("s")


def test_bottleneck_is_smallest_transfer_on_path(registry):
    usdt = _usdt(registry)
    r = _trace(registry, [xfer(usdt, T("s"), T("a"), 100 * U, 1), xfer(usdt, T("a"), T("b"), 30 * U, 2)])
    end = next(e for e in r.endpoints if e.address == T("b"))
    assert end.bottleneck.amount == 30 * U


def test_trace_is_deterministic(registry):
    src = demo.sources(registry)[Chain.TRON]
    tracer = Tracer(src, Attributor(demo.labels()), registry)
    a = tracer.trace(demo.T["suspect"], TraceParams())
    b = tracer.trace(demo.T["suspect"], TraceParams())
    assert a.model_dump_json() == b.model_dump_json()


def test_only_issuer_assets_raise_freeze_opportunity(registry):
    from anveshak.risk import alerts_for

    usdt = _usdt(registry)
    trx = registry.native(Chain.TRON)
    r = _trace(
        registry,
        [xfer(usdt, T("s"), T("a"), 10 * U, 1), xfer(trx, T("s"), T("b"), 10 * U, 2)],
        balances={(T("a"), usdt.key): 10 * U, (T("b"), trx.key): 10 * U},
    )
    freezable = frozenset(t.asset.key for t in registry.tokens() if t.issuer)
    rules = {(a.rule, a.addresses[0]) for a in alerts_for(r, None, freezable)}
    assert ("A-FREEZE-OPPORTUNITY", T("a")) in rules and ("A-FUNDS-HELD", T("b")) in rules


# --------------------------------------------------------------------------- R-COSPEND-SERVICE (Bitcoin)


def _btc_tx(n, inputs, outputs, minutes, coinjoin=False):
    """Transfers of one Bitcoin transaction as the Esplora adapter models them."""
    from datetime import timedelta

    from anveshak.domain import Asset, Transfer, TransferKind, UtxoContext

    from .conftest import BASE

    ctx = UtxoContext(input_addresses=tuple(sorted(inputs)), output_count=len(outputs), coinjoin_like=coinjoin)
    btc = Asset(chain=Chain.BITCOIN, symbol="BTC", decimals=8, verified=True)
    return [
        Transfer(chain=Chain.BITCOIN, tx_hash=f"{n:064x}", kind=TransferKind.UTXO_OUTPUT, position=i, sender=s, receiver=r, asset=btc,
                 amount=amount, block_number=800_000 + minutes, timestamp=BASE + timedelta(minutes=minutes), evidence_id="t", utxo=ctx)
        for i, (r, amount) in enumerate(outputs) for s in sorted(inputs)
    ]


def test_cospent_with_busy_wallet_stops_backward_trace(registry):
    S, HOT, DEP, B = ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    # a user withdrew from exchange B into DEP (a deposit address at an unlabelled exchange); that
    # exchange later paid the subject in a withdrawal that spent DEP together with its hot wallet HOT
    txs = _btc_tx(1, [B], [(DEP, 50_000_000)], 0) + _btc_tx(2, [HOT, DEP], [(S, 40_000_000)], 10)
    labels = [label(Chain.BITCOIN, B, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")]

    def run(counts):
        src = MemorySource(Chain.BITCOIN, txs, incomplete={HOT}, tx_counts=counts)
        return Tracer(src, Attributor(LabelStore(labels)), registry).trace(S, TraceParams(direction=Direction.IN, max_hops=3))

    stopped = run({HOT: 25_000})
    assert not [e for e in stopped.endpoints if e.kind is EndpointKind.VASP]  # ex1 is NOT reported as the funder
    dep = next(e for e in stopped.endpoints if e.address == DEP)
    assert dep.kind is EndpointKind.HIGH_ACTIVITY and "R-COSPEND-SERVICE" in dep.notes[0] and HOT in dep.notes[0]
    # a quiet co-input does not trigger the rule: ex1 is reached through DEP as before
    quiet_src = MemorySource(Chain.BITCOIN, txs, tx_counts={HOT: 3})
    quiet = Tracer(quiet_src, Attributor(LabelStore(labels)), registry).trace(S, TraceParams(direction=Direction.IN, max_hops=3))
    assert [e.attribution.entity_id for e in quiet.endpoints if e.kind is EndpointKind.VASP] == ["ex1"]


def test_cospend_rule_ignores_coinjoin(registry):
    S, HOT, DEP, B = ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    txs = _btc_tx(1, [B], [(DEP, 50_000_000)], 0) + _btc_tx(2, [HOT, DEP], [(S, 40_000_000)], 10, coinjoin=True)
    src = MemorySource(Chain.BITCOIN, txs, tx_counts={HOT: 25_000})
    r = Tracer(src, Attributor(LabelStore([])), registry).trace(S, TraceParams(direction=Direction.IN, max_hops=3))
    assert not any("R-COSPEND-SERVICE" in n for e in r.endpoints for n in e.notes)