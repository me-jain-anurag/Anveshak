"""Importers that turn public label datasets into provenance-carrying `Label` records.

Each import writes normalised JSONL plus a MANIFEST.json recording the download URL, the
sha256 of the exact bytes downloaded, the fetch time, and how many records were kept or
skipped (and why). Records that cannot be validated are skipped and counted — never
repaired by guesswork.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml

from ..addresses import AddressError, normalize
from ..chain import Chain, ChainFamily
from ..domain import Category, Label, RiskFlag, SourceClass
from ..evidence import sha256_hex
from .store import write_jsonl

GRAPHSENSE_RAW = "https://raw.githubusercontent.com/graphsense/graphsense-tagpacks/master/packs/{}"

# GraphSense `confidence` ids (graphsense-tagpack-tool src/tagpack/db/confidence.csv) → our
# source classes. The numeric "level" GraphSense attaches is deliberately NOT used.
GRAPHSENSE_CONFIDENCE = {
    "ownership": SourceClass.ENTITY_ATTESTED,  # creator controls the key
    "service_api": SourceClass.ENTITY_ATTESTED,  # retrieved from the service's own API
    "service_data": SourceClass.ENTITY_ATTESTED,  # data published/provided by the service itself
    "authority_data": SourceClass.AUTHORITY,  # e.g. OFAC
    "forensic_investigation": SourceClass.AUTHORITY,  # LEA/forensic case material
    "ledger_immanent": SourceClass.CURATED,
    "manual_transaction": SourceClass.CURATED,
    "trusted_provider": SourceClass.CURATED,
    "forensic": SourceClass.CURATED,  # e.g. academic papers
    "override": SourceClass.CURATED,
    "untrusted_transaction": SourceClass.WEAK,
    "web_crawl": SourceClass.WEAK,
    "heuristic": SourceClass.WEAK,
    "unknown": SourceClass.WEAK,
}

# GraphSense DW-VA taxonomy entity ids → our categories. Unlisted ids map to OTHER.
GRAPHSENSE_CATEGORY = {
    "exchange": Category.EXCHANGE,
    "wallet_service": Category.CUSTODIAL_WALLET,
    "payment_processor": Category.PAYMENT_PROCESSOR,
    "atm": Category.CRYPTO_ATM,
    "mixing_service": Category.MIXER,
    "coinjoin": Category.MIXER,
    "defi": Category.DEFI,
    "defi_lending": Category.DEFI,
    "defi_dex": Category.DEFI,
    "defi_derivative": Category.DEFI,
    "gambling": Category.GAMBLING,
    "miner": Category.MINING,
    "mining_service": Category.MINING,
    "market": Category.MARKET,
}

# GraphSense abuse ids → risk flags.
GRAPHSENSE_ABUSE = {
    "scam": RiskFlag.SCAM,
    "extortion": RiskFlag.EXTORTION,
    "sextortion": RiskFlag.EXTORTION,
    "phishing": RiskFlag.PHISHING,
    "service_hack": RiskFlag.HACK,
    "ransomware": RiskFlag.RANSOMWARE,
    "investment_fraud": RiskFlag.INVESTMENT_FRAUD,
    "ponzi_scheme": RiskFlag.PONZI,
    "pyramid_scheme": RiskFlag.PONZI,
    "sanction": RiskFlag.SANCTIONED,
    "extremism": RiskFlag.EXTREMISM,
    "terrorism": RiskFlag.TERRORISM,
    "dark_web": RiskFlag.DARK_WEB,
}

# Only unambiguous network ids. "usdt"/"usdc" as a *network* is ambiguous (Omni, ERC-20, TRC-20 ...)
# and is skipped rather than guessed.
GRAPHSENSE_NETWORK = {
    "btc": Chain.BITCOIN,
    "eth": Chain.ETHEREUM,
    "trx": Chain.TRON,
    "bep20": Chain.BSC,
    "bsc": Chain.BSC,
    "matic": Chain.POLYGON,
    "polygon": Chain.POLYGON,
}

DEFAULT_GRAPHSENSE_PACKS = [
    # exchanges (proof-of-reserves lists published by the exchanges themselves)
    "exchange-wallets-binance.yaml",
    "exchange-wallets-bitfinexcom.yaml",
    "exchange-wallets-bybit.yaml",
    "exchange-wallets-cryptocom.yaml",
    "exchange-wallets-deribit.yaml",
    "exchange-wallets-huobi.yaml",
    "exchange-wallets-kucoin.yaml",
    "exchange-wallets-okx.yaml",
    "exchange-wallets-swissborg.yaml",
    "binance.yaml",
    "etherscan-wordcloud-exchange.yaml",
    # mixers
    "tornado_cash.yaml",
    "blender_io.yaml",
    "sinbad_io.yaml",
    # "samourai.yaml" — 36k Whirlpool addresses (~20 MB); import on demand with --pack samourai.yaml
    "wasabi_collector.yaml",
    "mixing_fc2021.yaml",
    "etherscan-wordcloud-mixing_service.yaml",
    # sanctions, freezes, crime
    "ofac.yaml",
    "usdt_blacklist.yaml",
    "lazarus.yaml",
    "lazarus2.yaml",
    "ransomware.yaml",
    "ransomwhere.yaml",
    "hacks.yaml",
    "ronin_bridge.yaml",
]


def _as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_tagpack(raw: bytes, pack_name: str) -> tuple[list[Label], Counter]:
    doc = yaml.safe_load(raw)
    digest = sha256_hex(raw)
    skipped: Counter = Counter()
    labels: list[Label] = []
    header = {k: v for k, v in doc.items() if k != "tags"}
    for tag in doc.get("tags") or []:
        merged = {**header, **tag}
        network = str(merged.get("network") or merged.get("currency") or "").lower()
        chain = GRAPHSENSE_NETWORK.get(network)
        if chain is None:
            skipped[f"unsupported network {network or '?'}"] += 1
            continue
        try:
            address = normalize(chain, str(merged.get("address", "")))
        except AddressError:
            skipped["invalid address"] += 1
            continue
        source = str(merged.get("source") or "").strip()
        if not source:
            skipped["no primary source"] += 1
            continue
        confidence = str(merged.get("confidence") or "unknown")
        source_class = GRAPHSENSE_CONFIDENCE.get(confidence, SourceClass.WEAK)
        raw_category = merged.get("category")
        category = GRAPHSENSE_CATEGORY.get(str(raw_category), Category.OTHER) if raw_category else None
        abuse = merged.get("abuse")
        flags = (GRAPHSENSE_ABUSE[abuse],) if abuse in GRAPHSENSE_ABUSE else ()
        if category is None and not flags:
            skipped["no category or abuse"] += 1
            continue
        actor = merged.get("actor")
        text = str(merged.get("label") or merged.get("title") or pack_name)
        labels.append(
            Label(
                chain=chain,
                address=address,
                entity_id=str(actor).lower() if actor else None,
                entity_name=str(actor) if actor else None,
                category=category,
                risk_flags=flags,
                text=text,
                source_id="graphsense-tagpacks",
                source_class=source_class,
                primary_source=source,
                as_of=_as_date(merged.get("lastmod")),
                dataset_ref=f"graphsense-tagpacks/packs/{pack_name}@sha256:{digest}",
            )
        )
    return labels, skipped


def import_graphsense(out_dir: Path, packs: list[str] | None = None, client: httpx.Client | None = None) -> dict:
    client = client or httpx.Client(timeout=60)
    manifest = {"source_id": "graphsense-tagpacks", "imported_at": datetime.now(timezone.utc).isoformat(), "files": []}
    for pack in packs or DEFAULT_GRAPHSENSE_PACKS:
        url = GRAPHSENSE_RAW.format(pack)
        response = client.get(url)
        response.raise_for_status()
        raw = response.content
        labels, skipped = parse_tagpack(raw, pack)
        write_jsonl(Path(out_dir) / f"{Path(pack).stem}.jsonl", labels)
        manifest["files"].append({"pack": pack, "url": url, "sha256": sha256_hex(raw), "kept": len(labels), "skipped": dict(skipped)})
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


OFAC_RAW = "https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_{}.json"
OFAC_PRIMARY = "https://sanctionssearch.ofac.treas.gov/"
OFAC_TICKERS = ["XBT", "ETH", "TRX", "USDT", "USDC"]


def _chains_for_listed_address(address: str) -> list[Chain]:
    """OFAC lists an address per *asset*, not per chain. Decide the chain(s) by address format.
    An EVM address is the same key on every EVM chain, so a sanctions flag applies to all of them."""
    if address.startswith("0x"):
        return [c for c in Chain if c.family is ChainFamily.EVM]
    if address.startswith("T"):
        return [Chain.TRON]
    return [Chain.BITCOIN]


def parse_ofac_list(raw: bytes, ticker: str) -> tuple[list[Label], Counter]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"unexpected OFAC list format for {ticker}: expected a JSON array")
    digest = sha256_hex(raw)
    labels, skipped = [], Counter()
    for entry in data:
        if not isinstance(entry, str):
            skipped["non-string entry"] += 1
            continue
        for chain in _chains_for_listed_address(entry.strip()):
            try:
                address = normalize(chain, entry)
            except AddressError:
                skipped[f"invalid address for {chain}"] += 1
                continue
            labels.append(
                Label(
                    chain=chain,
                    address=address,
                    category=None,
                    risk_flags=(RiskFlag.SANCTIONED,),
                    text=f"OFAC SDN list: digital currency address ({ticker})",
                    source_id="ofac-sdn-0xb10c",
                    source_class=SourceClass.AUTHORITY,
                    primary_source=OFAC_PRIMARY,
                    as_of=None,
                    dataset_ref=f"0xB10C/ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_{ticker}.json@sha256:{digest}",
                )
            )
    return labels, skipped


def import_ofac(out_dir: Path, tickers: list[str] | None = None, client: httpx.Client | None = None) -> dict:
    client = client or httpx.Client(timeout=60)
    manifest = {"source_id": "ofac-sdn-0xb10c", "imported_at": datetime.now(timezone.utc).isoformat(), "files": []}
    for ticker in tickers or OFAC_TICKERS:
        url = OFAC_RAW.format(ticker)
        response = client.get(url)
        if response.status_code == 404:
            manifest["files"].append({"ticker": ticker, "url": url, "status": "not published"})
            continue
        response.raise_for_status()
        labels, skipped = parse_ofac_list(response.content, ticker)
        write_jsonl(Path(out_dir) / f"ofac_{ticker.lower()}.jsonl", labels)
        manifest["files"].append({"ticker": ticker, "url": url, "sha256": sha256_hex(response.content), "kept": len(labels), "skipped": dict(skipped)})
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def attestation_label(
    chain: Chain,
    address: str,
    entity_id: str,
    entity_name: str,
    category: Category,
    document_ref: str,
    as_of: date,
    source_class: SourceClass = SourceClass.ENTITY_ATTESTED,
) -> Label:
    """A label recorded from a formal document — typically a VASP's reply to a Sahyog
    request confirming that `address` is one of its (deposit) addresses."""
    if source_class not in (SourceClass.ENTITY_ATTESTED, SourceClass.AUTHORITY):
        raise ValueError("investigator attestations must be entity_attested or authority")
    if not document_ref.strip():
        raise ValueError("an attestation must reference the document it was taken from")
    return Label(
        chain=chain,
        address=normalize(chain, address),
        entity_id=entity_id.lower(),
        entity_name=entity_name,
        category=category,
        text=f"confirmed by {entity_name}",
        source_id="investigator",
        source_class=source_class,
        primary_source=document_ref,
        as_of=as_of,
        dataset_ref=f"investigator:{document_ref}",
    )
