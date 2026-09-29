"""SYNTHETIC demonstration scenario.

Every entity, address, label and transaction below is fictional and generated from fixed
seeds. Addresses are valid in format (so the whole pipeline runs unchanged) but were
derived from hashes of made-up names. Results produced from this data are watermarked
"SYNTHETIC — NOT EVIDENCE" and synthetic labels are refused by the live label loader.

The scenario exercises every rule the engine has:
  Tron / USDT (forward)   pass-through wallet -> deposit address -> sweep to attested hot wallet (A, R-SWEEP)
                          peel chain of five decreasing transfers                    (T-PEEL)
                          direct payment to a thinly-sourced exchange wallet        (C -> analyst review)
                          payment to an address two sources disagree about          (X -> blocked)
                          payment into a mixer                                      (service endpoint)
                          value parked at an address                                (dormant -> issuer freeze review)
                          a transfer made *before* the funds arrived                (not followed: time order)
                          dust and a fake-USDT token                                (excluded, counted)
  Tron / USDT (backward)  suspect was funded by a withdrawal from an exchange       (A -> disclosure)
  Cross-chain             600 USDT into a THORChain vault, swapped to BTC, deposited at Alpha
                          (X-THORCHAIN link, continuation trace on Bitcoin, D-COSPEND)
  Bitcoin (forward)       two suspect addresses spent together                      (C-MULTI-INPUT subject cluster)
                          deposit address co-spent with an exchange wallet          (D-COSPEND, B)
                          change output entering a CoinJoin                         (coinjoin_like stop)
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from .addresses import encode_segwit, tron_from_hex
from .assets import AssetRegistry
from .chain import Chain
from .chains.bitcoin import is_coinjoin_like
from .chains.memory import MemorySource
from .directory import Channel, DirectoryEntry, SourcedFact, VaspDirectory
from .domain import Asset, Category, Label, RiskFlag, SourceClass, Transfer, TransferKind, UtxoContext
from .labels.store import LabelStore

BASE = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
CASE_REFERENCE = "DEMO-SYNTHETIC-0001"


def tron_addr(name: str) -> str:
    return tron_from_hex("41" + hashlib.sha256(f"anveshak-demo:{name}".encode()).hexdigest()[:40])


def btc_addr(name: str) -> str:
    return encode_segwit(0, hashlib.sha256(f"anveshak-demo:{name}".encode()).digest()[:20])


def txid(name: str) -> str:
    return hashlib.sha256(f"anveshak-demo-tx:{name}".encode()).hexdigest()


T = {n: tron_addr(n) for n in ["victim-1", "victim-2", "suspect", "peel-1", "peel-2", "peel-3", "peel-4", "parking", "thor-vault", "deposit-alpha", "alpha-hot", "beta-hot", "disputed", "mixer", "decoy", "dust", "fake-usdt-holder", "fake-usdt-contract"]}
B = {n: btc_addr(n) for n in ["victim", "victim-b", "suspect", "suspect-2", "peel", "deposit", "alpha-hot", "other-deposit", "alpha-consolidated", "change", "cj-1", "cj-2", "cj-3", "cj-out-1", "cj-out-2", "cj-out-3", "cj-out-4", "cj-change", "thor-btc-vault", "thor-out", "deposit-2", "alpha-consolidated-2"]}


def _synthetic_label(chain: Chain, address: str, source: str, klass: SourceClass, *, entity: str | None = None, name: str | None = None, category: Category | None = None, flags: tuple[RiskFlag, ...] = ()) -> Label:
    return Label(
        chain=chain,
        address=address,
        entity_id=entity,
        entity_name=name,
        category=category,
        risk_flags=flags,
        text=f"SYNTHETIC: {name or ', '.join(flags)}",
        source_id="demo",
        source_class=klass,
        primary_source=f"synthetic://demo/{source}",
        as_of=date(2026, 8, 1),
        dataset_ref="anveshak/demo.py",
        synthetic=True,
    )


def labels() -> LabelStore:
    alpha = dict(entity="demo-alpha", name="Demo Exchange Alpha", category=Category.EXCHANGE)
    return LabelStore(
        [
            _synthetic_label(Chain.TRON, T["alpha-hot"], "alpha-proof-of-reserves", SourceClass.ENTITY_ATTESTED, **alpha),
            _synthetic_label(Chain.TRON, T["beta-hot"], "community-list-1", SourceClass.CURATED, entity="demo-beta", name="Demo Exchange Beta", category=Category.EXCHANGE),
            _synthetic_label(Chain.TRON, T["disputed"], "community-list-1", SourceClass.CURATED, **alpha),
            _synthetic_label(Chain.TRON, T["disputed"], "community-list-2", SourceClass.CURATED, entity="demo-gamma", name="Demo Exchange Gamma", category=Category.EXCHANGE),
            _synthetic_label(Chain.TRON, T["mixer"], "community-list-1", SourceClass.CURATED, entity="demo-mixer", name="Demo Mixer", category=Category.MIXER),
            _synthetic_label(Chain.TRON, T["mixer"], "research-paper-3", SourceClass.CURATED, entity="demo-mixer", name="Demo Mixer", category=Category.MIXER),
            _synthetic_label(Chain.TRON, T["mixer"], "sanctions-list", SourceClass.AUTHORITY, flags=(RiskFlag.SANCTIONED,)),
            _synthetic_label(Chain.TRON, T["suspect"], "victim-reports", SourceClass.CURATED, flags=(RiskFlag.SCAM,)),
            _synthetic_label(Chain.BITCOIN, B["alpha-hot"], "alpha-proof-of-reserves", SourceClass.ENTITY_ATTESTED, **alpha),
        ]
    )


def directory(real: VaspDirectory) -> VaspDirectory:
    synthetic = [
        DirectoryEntry(
            entity_id="demo-alpha",
            display_name="Demo Exchange Alpha (SYNTHETIC)",
            role="vasp",
            official_sources=("demo/alpha-proof-of-reserves",),
            channels=(Channel(name="Alpha LE portal (fictional)", url="https://alpha.example.invalid/law-enforcement", source="synthetic://demo/alpha-le-page", checked=date(2026, 9, 1)),),
            sahyog_onboarded=SourcedFact(value=True, source="synthetic://demo/alpha-press-release", checked=date(2026, 9, 1)),
            synthetic=True,
        ),
        DirectoryEntry(entity_id="demo-beta", display_name="Demo Exchange Beta (SYNTHETIC)", role="vasp", synthetic=True),
    ]
    return VaspDirectory(real.entries() + synthetic)


def _account(name: str, sender: str, receiver: str, asset: Asset, amount: int, minutes: int, block_base: int = 75_000_000) -> Transfer:
    return Transfer(
        chain=asset.chain,
        tx_hash=txid(name),
        kind=TransferKind.TOKEN if asset.contract else TransferKind.NATIVE,
        position=0,
        sender=sender,
        receiver=receiver,
        asset=asset,
        amount=amount,
        block_number=block_base + minutes * 20,
        timestamp=BASE + timedelta(minutes=minutes),
        evidence_id="synthetic",
    )


def _utxo(name: str, inputs: list[str], outputs: list[tuple[str, int]], asset: Asset, block: int) -> list[Transfer]:
    ctx = UtxoContext(input_addresses=tuple(sorted(set(inputs))), output_count=len(outputs), coinjoin_like=is_coinjoin_like(inputs, [v for _, v in outputs]))
    out = []
    for index, (receiver, value) in enumerate(outputs):
        for sender in sorted(set(inputs)):
            if sender != receiver:
                out.append(
                    Transfer(
                        chain=Chain.BITCOIN,
                        tx_hash=txid(name),
                        kind=TransferKind.UTXO_OUTPUT,
                        position=index,
                        sender=sender,
                        receiver=receiver,
                        asset=asset,
                        amount=value,
                        block_number=block,
                        timestamp=BASE + timedelta(minutes=(block - 900_000) * 10),
                        evidence_id="synthetic",
                        utxo=ctx,
                    )
                )
    return out


def sources(registry: AssetRegistry) -> dict[Chain, MemorySource]:
    usdt = registry.token(Chain.TRON, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT", 6)
    fake = registry.token(Chain.TRON, T["fake-usdt-contract"], "USDT", 6)  # unknown contract → verified=False
    u = 1_000_000  # 1 USDT in base units
    tron = [
        _account("alpha-withdrawal", T["alpha-hot"], T["suspect"], usdt, 150 * u, -3 * 24 * 60),
        _account("peel-1-earlier", T["peel-1"], T["decoy"], usdt, 900 * u, -24 * 60),
        _account("victim-1-pays", T["victim-1"], T["suspect"], usdt, 5_000 * u, 0),
        _account("victim-2-pays", T["victim-2"], T["suspect"], usdt, 7_500 * u, 30),
        _account("suspect-to-peel-1", T["suspect"], T["peel-1"], usdt, 12_000 * u, 60),
        _account("suspect-dust", T["suspect"], T["dust"], usdt, u // 2, 65),
        _account("suspect-fake-usdt", T["suspect"], T["fake-usdt-holder"], fake, 10_000 * u, 70),
        _account("peel-1-to-deposit", T["peel-1"], T["deposit-alpha"], usdt, 4_000 * u, 70),
        _account("peel-1-to-peel-2", T["peel-1"], T["peel-2"], usdt, 7_900 * u, 80),
        _account("peel-1-to-disputed", T["peel-1"], T["disputed"], usdt, 50 * u, 85),
        _account("deposit-sweep", T["deposit-alpha"], T["alpha-hot"], usdt, 4_000 * u, 180),
        _account("peel-2-to-peel-3", T["peel-2"], T["peel-3"], usdt, 6_400 * u, 200),
        _account("peel-2-to-mixer", T["peel-2"], T["mixer"], usdt, 1_500 * u, 210),
        _account("peel-3-to-peel-4", T["peel-3"], T["peel-4"], usdt, 4_600 * u, 300),
        _account("peel-3-to-beta", T["peel-3"], T["beta-hot"], usdt, 1_200 * u, 305),
        _account("peel-3-to-thor", T["peel-3"], T["thor-vault"], usdt, 600 * u, 310),
        _account("peel-4-to-parking", T["peel-4"], T["parking"], usdt, 3_800 * u, 400),
    ]
    btc = registry.native(Chain.BITCOIN)
    sat = 100_000_000
    bitcoin = (
        _utxo("btc-victim-pays", [B["victim"]], [(B["suspect"], sat // 2)], btc, 900_000)
        + _utxo("btc-victim-b-pays", [B["victim-b"]], [(B["suspect-2"], 20_000_000)], btc, 900_001)
        + _utxo("btc-suspect-splits", [B["suspect"], B["suspect-2"]], [(B["peel"], 30_000_000), (B["change"], 39_000_000)], btc, 900_003)
        + _utxo("btc-peel-to-deposit", [B["peel"]], [(B["deposit"], 29_950_000)], btc, 900_006)
        + _utxo("btc-alpha-consolidation", [B["deposit"], B["alpha-hot"], B["other-deposit"]], [(B["alpha-consolidated"], 150_000_000)], btc, 900_020)
        + _utxo(
            "btc-coinjoin",
            [B["change"], B["cj-1"], B["cj-2"], B["cj-3"]],
            [(B["cj-out-1"], 10_000_000), (B["cj-out-2"], 10_000_000), (B["cj-out-3"], 10_000_000), (B["cj-out-4"], 10_000_000), (B["cj-change"], 5_000_000)],
            btc,
            900_010,
        )
        + _utxo("btc-thor-outbound", [B["thor-btc-vault"]], [(B["thor-out"], 900_000)], btc, 900_033)
        + _utxo("btc-thor-out-to-deposit", [B["thor-out"]], [(B["deposit-2"], 890_000)], btc, 900_035)
        + _utxo("btc-alpha-consolidation-2", [B["deposit-2"], B["alpha-hot"]], [(B["alpha-consolidated-2"], 50_000_000)], btc, 900_045)
    )
    return {
        Chain.TRON: MemorySource(Chain.TRON, tron, balances={(T["parking"], usdt.key): 3_800 * u}, incomplete={T["thor-vault"]}),
        Chain.BITCOIN: MemorySource(Chain.BITCOIN, bitcoin),
    }


class DemoThorchain:
    """SYNTHETIC stand-in for Midgard: one swap, Tron USDT (peel-3 -> THORChain vault) to BTC."""

    name = "thorchain"

    def resolve(self, chain: Chain, tx_hash: str, from_address: str, endpoint_id: str):
        from .crosschain import CrossChainLink

        if chain is not Chain.TRON or tx_hash != txid("peel-3-to-thor"):
            return []
        return [
            CrossChainLink(
                protocol="thorchain",
                rule="X-THORCHAIN",
                from_chain=Chain.TRON,
                from_tx=tx_hash,
                from_address=from_address,
                endpoint_id=endpoint_id,
                to_chain=Chain.BITCOIN,
                to_chain_code="BTC",
                to_address=B["thor-out"],
                to_tx=txid("btc-thor-outbound"),
                asset_in="TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T",
                asset_out="BTC.BTC",
                amount_out="900000",
                memo=f"=:BTC.BTC:{B['thor-out']}",
                status="success",
                evidence_id="synthetic",
            )
        ]


def trust(directory: VaspDirectory):
    """Source-trust rules for the demo: the fictional sanctions list counts as an authority."""
    from .config import DATA_DIR
    from .sourcetrust import SourceTrust

    base = SourceTrust.load(DATA_DIR / "authorities.yaml", directory.official_sources())
    return SourceTrust(base.official, base.authority_domains + ("demo/sanctions-list",))


MAX_HOPS = 6


def subjects() -> list[dict]:
    return [{"chain": "tron", "address": T["suspect"]}, {"chain": "bitcoin", "address": B["suspect"]}]


def engine(registry: AssetRegistry, real_directory: VaspDirectory):
    """A SYNTHETIC-mode engine wired to the demo labels, sources, directory and trust rules."""
    from .case import DataMode, Engine

    d = directory(real_directory)
    return Engine(DataMode.SYNTHETIC, labels(), registry, d, sources=sources(registry), trust=trust(d), resolvers=[DemoThorchain()])


def request(case_reference: str = CASE_REFERENCE):
    from .case import CaseRequest, Subject

    return CaseRequest(case_reference=case_reference, subjects=tuple(Subject(**s) for s in subjects()), max_hops=MAX_HOPS)
