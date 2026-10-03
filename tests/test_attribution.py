from anveshak.attribution import Attributor, grade_labels
from anveshak.chain import Chain
from anveshak.domain import AddressHistory, Category, Grade, RiskFlag, SourceClass, Transfer, TransferKind, UtxoContext
from anveshak.labels.store import LabelStore

from .conftest import BASE, label

E = Chain.ETHEREUM
A1 = "0x" + "a1" * 20


def g(*labels):
    return grade_labels(E, A1, tuple(labels))


def test_no_ownership_labels_means_no_attribution():
    risk_only = label(E, A1, SourceClass.AUTHORITY, "https://ofac", entity=None, category=None, risk_flags=(RiskFlag.SANCTIONED,))
    assert g(risk_only) is None
    assert g() is None


def test_entity_attested_is_grade_a():
    a = g(label(E, A1, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por"))
    assert (a.grade, a.rule, a.entity_id) == (Grade.A, "G-A1", "ex1")


def test_authority_is_grade_a():
    assert g(label(E, A1, SourceClass.AUTHORITY, "https://court.example/order")).rule == "G-A2"


def test_two_independent_curated_sources_is_grade_b():
    a = g(label(E, A1, SourceClass.CURATED, "https://list-one.example/x"), label(E, A1, SourceClass.CURATED, "https://list-two.example/y"))
    assert (a.grade, a.rule) == (Grade.B, "G-B1")


def test_same_primary_source_twice_is_not_corroboration():
    # Different datasets copying the same upstream page must not count twice.
    a = g(
        label(E, A1, SourceClass.CURATED, "https://www.list-one.example/x/", source_id="ds1"),
        label(E, A1, SourceClass.CURATED, "http://list-one.example/x?ref=2", source_id="ds2"),
    )
    assert (a.grade, a.rule) == (Grade.C, "G-C1")


def test_weak_sources_never_exceed_c():
    a = g(*[label(E, A1, SourceClass.WEAK, f"https://crawl{i}.example") for i in range(5)])
    assert (a.grade, a.rule) == (Grade.C, "G-C2")


def test_conflict_is_grade_x_even_with_attested_source():
    a = g(label(E, A1, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por"), label(E, A1, SourceClass.CURATED, "https://list.example", entity="ex2"))
    assert a.grade is Grade.X
    assert a.conflicts == ("ex1", "ex2")
    assert a.entity_id is None


def test_alias_prevents_false_conflict():
    labels = (label(E, A1, SourceClass.CURATED, "https://a.example", entity="crypto.com"), label(E, A1, SourceClass.CURATED, "https://b.example", entity="cryptocom"))
    assert grade_labels(E, A1, labels).grade is Grade.X
    fixed = grade_labels(E, A1, labels, aliases={"crypto.com": "cryptocom"})
    assert (fixed.grade, fixed.entity_id) == (Grade.B, "cryptocom")


def test_vasp_category_preferred_over_other_for_same_entity():
    a = g(label(E, A1, SourceClass.CURATED, "https://a.example", category=Category.OTHER), label(E, A1, SourceClass.CURATED, "https://b.example", category=Category.EXCHANGE))
    assert a.category is Category.EXCHANGE


# --------------------------------------------------------------------------- derivations

def _btc_history(address, txs):
    transfers = []
    for i, (inputs, coinjoin) in enumerate(txs):
        ctx = UtxoContext(input_addresses=tuple(sorted(inputs)), output_count=1, coinjoin_like=coinjoin)
        from anveshak.assets import AssetRegistry  # noqa: F401 - asset below is native BTC
        from anveshak.domain import Asset

        transfers.append(
            Transfer(
                chain=Chain.BITCOIN, tx_hash=f"{i:064x}", kind=TransferKind.UTXO_OUTPUT, position=0, sender=address,
                receiver="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", asset=Asset(chain=Chain.BITCOIN, symbol="BTC", decimals=8, verified=True),
                amount=1000, block_number=1, timestamp=BASE, evidence_id="t", utxo=ctx,
            )
        )
    return AddressHistory(chain=Chain.BITCOIN, address=address, transfers=tuple(transfers), complete=True)


X = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
HOT = "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo"
OTHER = "3LYJfcfHPXYJreMsASk2jkn69LWEYKzexb"


def test_cospend_with_attested_address_derives_grade_b():
    store = LabelStore([label(Chain.BITCOIN, HOT, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")])
    a = Attributor(store).derive_cospend(_btc_history(X, [({X, HOT}, False)]))
    assert (a.grade, a.rule, a.entity_id, a.derived.anchor_address) == (Grade.B, "D-COSPEND", "ex1", HOT)


def test_cospend_ignores_coinjoin_transactions():
    store = LabelStore([label(Chain.BITCOIN, HOT, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")])
    assert Attributor(store).derive_cospend(_btc_history(X, [({X, HOT}, True)])) is None


def test_cospend_never_derives_from_grade_c():
    store = LabelStore([label(Chain.BITCOIN, HOT, SourceClass.CURATED, "https://one.example")])
    assert Attributor(store).derive_cospend(_btc_history(X, [({X, HOT}, False)])) is None


def test_cospend_with_two_entities_is_conflict():
    store = LabelStore(
        [
            label(Chain.BITCOIN, HOT, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por"),
            label(Chain.BITCOIN, OTHER, SourceClass.ENTITY_ATTESTED, "https://ex2.example/por", entity="ex2"),
        ]
    )
    a = Attributor(store).derive_cospend(_btc_history(X, [({X, HOT}, False), ({X, OTHER}, False)]))
    assert a.grade is Grade.X and a.conflicts == ("ex1", "ex2")


def test_evm_key_derivation_requires_confirmed_eoa():
    store = LabelStore([label(Chain.ETHEREUM, A1, SourceClass.ENTITY_ATTESTED, "https://ex1.example/por")])
    att = Attributor(store)
    derived = att.resolve(Chain.BSC, A1, is_contract=lambda _: False)
    assert (derived.grade, derived.rule, derived.chain) == (Grade.B, "D-EVM-KEY", Chain.BSC)
    assert Attributor(store).resolve(Chain.BSC, A1, is_contract=lambda _: True) is None  # contract
    assert Attributor(store).resolve(Chain.BSC, A1, is_contract=lambda _: None) is None  # unknown
    assert Attributor(store).resolve(Chain.BSC, A1) is None  # no checker available


def test_every_attribution_is_grounded():
    import pytest

    from anveshak.domain import Attribution

    with pytest.raises(ValueError):
        Attribution(chain=E, address=A1, entity_id="x", entity_name="x", category=None, grade=Grade.A, rule="G-A1", explanation="")


# --------------------------------------------------------------------------- G-N1: formal denials


def _deny(entity="ex1", as_of=None, klass=SourceClass.ENTITY_ATTESTED, source="Sahyog reply R-1"):
    from datetime import date

    return label(E, A1, klass, source, entity=entity, category=None, denies=True, as_of=as_of or date(2026, 3, 1), text=f"denied by {entity}")


def _confirm(entity="ex1", as_of=None):
    from datetime import date

    return label(E, A1, SourceClass.ENTITY_ATTESTED, "Sahyog reply R-2", entity=entity, as_of=as_of or date(2026, 3, 1))


def test_denial_overrides_curated_labels():
    curated = (label(E, A1, SourceClass.CURATED, "https://list-one.example/x"), label(E, A1, SourceClass.CURATED, "https://list-two.example/y"))
    assert g(*curated).grade is Grade.B
    assert g(*curated, _deny()) is None  # the entity said it is not theirs: no attribution left


def test_denial_removes_only_the_denying_entity():
    a = g(label(E, A1, SourceClass.CURATED, "https://list-one.example/x", entity="ex1"),
          label(E, A1, SourceClass.CURATED, "https://list-two.example/y", entity="ex2"), _deny("ex1"))
    assert (a.grade, a.rule, a.entity_id) == (Grade.C, "G-C1", "ex2")  # was a G-X1 conflict before the denial
    assert any("G-N1" in n for n in a.trust_notes)


def test_later_confirmation_supersedes_denial():
    from datetime import date

    a = g(_deny(as_of=date(2026, 1, 10)), _confirm(as_of=date(2026, 2, 1)))
    assert (a.grade, a.rule) == (Grade.A, "G-A1") and any("superseded" in n for n in a.trust_notes)
    assert g(_confirm(as_of=date(2026, 1, 10)), _deny(as_of=date(2026, 2, 1))) is None  # later denial wins


def test_same_date_confirmation_and_denial_is_grade_x():
    a = g(_confirm(), _deny())
    assert (a.grade, a.rule) == (Grade.X, "G-N1") and a.conflicts == ("ex1",)


def test_denial_from_a_weak_source_is_not_counted():
    curated = label(E, A1, SourceClass.CURATED, "https://list-one.example/x")
    a = g(curated, _deny(klass=SourceClass.WEAK, source="https://forum.example/post"))
    assert (a.grade, a.entity_id) == (Grade.C, "ex1") and any("not counted" in n for n in a.trust_notes)


def test_denial_survives_store_round_trip(tmp_path):
    from anveshak.labels.importers import attestation_label
    from anveshak.labels.store import write_jsonl
    from datetime import date

    d = attestation_label(E, A1, "ex1", "EX1", Category.EXCHANGE, "Sahyog reply R-9", date(2026, 3, 1), denies=True, reply_id="R-9")
    assert d.denies and d.category is None and d.dataset_ref == "sahyog-reply:R-9"
    write_jsonl(tmp_path / "a.jsonl", [d])
    back = LabelStore.load([tmp_path / "a.jsonl"])
    assert back.get(E, A1)[0] == d and back.snapshot_hash() == LabelStore([d]).snapshot_hash()