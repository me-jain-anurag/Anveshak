"""Blockchain-intelligence API providers (ADR-0016).

A provider turns a third-party intelligence API into ordinary `Label`s, fetched through the
evidence store (so every answer is hashed, stored and replayable) and graded by exactly the
same rules as any other label. A provider never overrides the grading: an intelligence
vendor is a curated source unless its answer cites an allow-listed authority.

Parsing is strict. If a response does not have the documented shape, the provider raises
SourceError instead of guessing — an unexpected answer is a coverage gap, not a label.

Implemented:
  ChainalysisSanctions  free sanctions-screening API (public.chainalysis.com, X-API-Key).
                        Response shape as documented publicly ({"identifications": [...]});
                        not verified live in this project for lack of a key — hence strict parsing.
  EtherscanNametags     Etherscan V2 `nametag/getaddresstag` (Pro Plus plan); response shape
                        per https://docs.etherscan.io/api-reference/endpoint/getaddresstag.md
"""

from __future__ import annotations

from typing import Protocol

from .chain import EVM_CHAIN_IDS, Chain, ChainFamily
from .domain import Category, Label, RiskFlag, SourceClass
from .errors import SourceError
from .evidence import Fetcher


class IntelProvider(Protocol):
    name: str

    def lookup(self, chain: Chain, address: str) -> list[Label]: ...


class ChainalysisSanctions:
    name = "chainalysis-sanctions-api"

    def __init__(self, fetcher: Fetcher, api_key: str, base_url: str = "https://public.chainalysis.com/api/v1/address"):
        self.fetcher = fetcher
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def lookup(self, chain: Chain, address: str) -> list[Label]:
        fetched = self.fetcher.get(f"{self.base_url}/{address}", headers={"X-API-Key": self.api_key})
        data = fetched.data
        if not isinstance(data, dict) or not isinstance(data.get("identifications"), list):
            raise SourceError("Chainalysis sanctions API: response lacks an 'identifications' list")
        labels = []
        for item in data["identifications"]:
            if not isinstance(item, dict) or not isinstance(item.get("category"), str):
                raise SourceError("Chainalysis sanctions API: identification without a category")
            if item["category"].lower() != "sanctions":
                continue
            url = str(item.get("url") or "").strip()
            labels.append(
                Label(
                    chain=chain,
                    address=address,
                    category=None,
                    risk_flags=(RiskFlag.SANCTIONED,),
                    text=str(item.get("name") or "sanctioned (Chainalysis)"),
                    source_id=self.name,
                    # Claimed as authority only when it cites the authority; the source-trust
                    # check still verifies the URL is on an allow-listed authority domain.
                    source_class=SourceClass.AUTHORITY if url else SourceClass.CURATED,
                    primary_source=url or f"{self.base_url}/{address}",
                    dataset_ref=f"evidence:{fetched.evidence_id}",
                )
            )
        return labels


# Etherscan label slugs → our categories / risk flags. Slugs not listed carry no category.
_SLUG_CATEGORY = {
    "exchange": Category.EXCHANGE,
    "bridge": Category.BRIDGE,
    "mixer": Category.MIXER,
    "tornado-cash": Category.MIXER,
    "dex": Category.DEFI,
    "gambling": Category.GAMBLING,
    "mining": Category.MINING,
    "payment": Category.PAYMENT_PROCESSOR,
}
_SLUG_FLAG = {
    "phish-hack": RiskFlag.PHISHING,
    "heist": RiskFlag.HACK,
    "exploit": RiskFlag.HACK,
    "ofac-sanctioned": RiskFlag.SANCTIONED,
    "sanctioned": RiskFlag.SANCTIONED,
    "scam": RiskFlag.SCAM,
}


class EtherscanNametags:
    name = "etherscan-nametag-api"

    def __init__(self, fetcher: Fetcher, api_key: str, base_url: str = "https://api.etherscan.io/v2/api"):
        self.fetcher = fetcher
        self.api_key = api_key
        self.base_url = base_url

    def lookup(self, chain: Chain, address: str) -> list[Label]:
        if chain.family is not ChainFamily.EVM:
            return []
        fetched = self.fetcher.get(
            self.base_url,
            params={"chainid": str(EVM_CHAIN_IDS[chain]), "module": "nametag", "action": "getaddresstag", "address": address, "apikey": self.api_key},
        )
        data = fetched.data
        if not isinstance(data, dict) or data.get("status") not in ("0", "1"):
            raise SourceError("Etherscan nametag API: unexpected payload")
        if data["status"] == "0":
            if isinstance(data.get("result"), list):
                return []
            raise SourceError(f"Etherscan nametag API: {data.get('result')}")
        labels = []
        for item in data.get("result") or []:
            if not isinstance(item, dict) or str(item.get("address", "")).lower() != address:
                continue
            slugs = [str(s).lower() for s in item.get("labels_slug") or []]
            category = next((_SLUG_CATEGORY[s] for s in slugs if s in _SLUG_CATEGORY), None)
            flags = tuple(sorted({_SLUG_FLAG[s] for s in slugs if s in _SLUG_FLAG}))
            if category is None and not flags:
                continue
            entity = next((s for s in slugs if s not in _SLUG_CATEGORY and s not in _SLUG_FLAG), None)
            labels.append(
                Label(
                    chain=chain,
                    address=address,
                    entity_id=entity,
                    entity_name=str(item.get("nametag") or entity or "") or None,
                    category=category,
                    risk_flags=flags,
                    text=str(item.get("nametag") or ", ".join(item.get("labels") or [])),
                    source_id=self.name,
                    source_class=SourceClass.CURATED,
                    primary_source=chain.address_url(address),
                    dataset_ref=f"evidence:{fetched.evidence_id}",
                )
            )
        return labels
