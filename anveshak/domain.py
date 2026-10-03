"""Core domain types.

Three kinds of statement are kept strictly apart (ADR-0003):

  Fact         a `Transfer`: something a ledger records. Re-verifiable by anyone.
  Claim        a `Label`: someone (a named source) says an address belongs to X / is risky.
  Inference    an `Attribution` or `Endpoint`: a documented, deterministic rule applied to
               facts and claims. Always carries the rule id and the inputs it used.

All models are immutable. Amounts are integers in base units (satoshi, wei, sun) — no floats.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from .chain import Chain


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def drop_derived_id(data):
    """`id` is derived from content and serialised for API consumers; on input it is dropped
    and recomputed, so a stored id can never disagree with the data it names."""
    if isinstance(data, dict) and "id" in data:
        data = {k: v for k, v in data.items() if k != "id"}
    return data


# --------------------------------------------------------------------------- facts


class Direction(StrEnum):
    OUT = "out"  # follow value leaving the subject (cash-out side)
    IN = "in"  # follow value arriving at the subject (funding side)


class TransferKind(StrEnum):
    NATIVE = "native"  # coin transfer on an account chain (ETH, BNB, POL, TRX)
    INTERNAL = "internal"  # coin moved inside a contract call (EVM internal transaction)
    TOKEN = "token"  # ERC-20 / TRC-20 Transfer event
    UTXO_OUTPUT = "utxo_output"  # a Bitcoin transaction output


class Asset(Frozen):
    chain: Chain
    contract: str | None = None  # None = the chain's native coin
    symbol: str
    decimals: int = Field(ge=0, le=36)
    # True only when the (chain, contract) pair is in the curated asset registry. Token
    # symbols are attacker-controlled ("fake USDT"), so identity is the contract address.
    verified: bool

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.contract or 'native'}"

    def format(self, amount: int) -> str:
        value = Decimal(amount).scaleb(-self.decimals)
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text} {self.symbol}"


class UtxoContext(Frozen):
    input_addresses: tuple[str, ...]
    output_count: int
    coinjoin_like: bool


class Transfer(Frozen):
    chain: Chain
    tx_hash: str
    kind: TransferKind
    # Bitcoin: the output index. Account chains: the occurrence ordinal of this exact
    # (asset, sender, receiver, amount) tuple within the transaction — stable from either
    # party's perspective because both parties' histories contain every such transfer.
    position: int = Field(ge=0)
    sender: str
    receiver: str
    asset: Asset
    amount: int = Field(ge=0)
    block_number: int | None = None  # TronGrid's TRC-20 listing omits it; verification fills it in
    timestamp: datetime
    evidence_id: str  # sha256 of the raw API response this record was parsed from
    utxo: UtxoContext | None = None

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v.astimezone(timezone.utc)

    @property
    def id(self) -> str:
        return ":".join(
            [
                self.chain,
                self.tx_hash,
                self.kind,
                self.asset.contract or "native",
                self.sender,
                self.receiver,
                str(self.amount),
                str(self.position),
            ]
        )

    @property
    def order_key(self) -> tuple:
        """Total order used everywhere a deterministic ordering of transfers is needed."""
        return (self.timestamp, self.block_number or -1, self.tx_hash, self.position, self.sender, self.receiver)

    def not_before(self, other: Transfer) -> bool:
        """True if self could have spent value that `other` delivered (same block allowed)."""
        if self.block_number is not None and other.block_number is not None:
            return self.block_number >= other.block_number
        return self.timestamp >= other.timestamp

    def not_after(self, other: Transfer) -> bool:
        if self.block_number is not None and other.block_number is not None:
            return self.block_number <= other.block_number
        return self.timestamp <= other.timestamp

    @property
    def formatted_amount(self) -> str:
        return self.asset.format(self.amount)


class AddressHistory(Frozen):
    chain: Chain
    address: str
    transfers: tuple[Transfer, ...]
    complete: bool  # False when a record/page cap was hit — coverage is then partial
    note: str | None = None
    # Set by window-limited sources (RPC log scans): only transfers inside this window were
    # examined. Complete *within* the window; anything outside it is a declared coverage gap.
    window_start: datetime | None = None
    window_end: datetime | None = None


# --------------------------------------------------------------------------- claims


class SourceClass(StrEnum):
    """Who is making a claim — the only input to grading besides agreement/conflict."""

    ENTITY_ATTESTED = "entity_attested"  # the entity itself (proof-of-reserves list, VASP reply to an LEA request)
    AUTHORITY = "authority"  # a public authority (OFAC, court order, LEA seizure record)
    CURATED = "curated"  # third-party curated dataset with a dereferenceable primary source
    WEAK = "weak"  # web crawls, heuristics, unknown provenance — never enough on its own


class Category(StrEnum):
    EXCHANGE = "exchange"
    CUSTODIAL_WALLET = "custodial_wallet"
    PAYMENT_PROCESSOR = "payment_processor"
    CRYPTO_ATM = "crypto_atm"
    MIXER = "mixer"
    BRIDGE = "bridge"
    DEFI = "defi"
    GAMBLING = "gambling"
    MINING = "mining"
    MARKET = "market"
    STABLECOIN_ISSUER = "stablecoin_issuer"
    OTHER = "other"


VASP_CATEGORIES = frozenset(
    {Category.EXCHANGE, Category.CUSTODIAL_WALLET, Category.PAYMENT_PROCESSOR, Category.CRYPTO_ATM}
)
# Reaching any of these ends a trace path: funds entered a service's pooled custody, and
# following the service's internal movements would attribute other people's money.
SERVICE_CATEGORIES = VASP_CATEGORIES | {
    Category.MIXER,
    Category.BRIDGE,
    Category.DEFI,
    Category.GAMBLING,
    Category.MINING,
    Category.MARKET,
    Category.STABLECOIN_ISSUER,
}


class RiskFlag(StrEnum):
    SANCTIONED = "sanctioned"
    SCAM = "scam"
    EXTORTION = "extortion"
    PHISHING = "phishing"
    HACK = "hack"
    RANSOMWARE = "ransomware"
    INVESTMENT_FRAUD = "investment_fraud"
    PONZI = "ponzi"
    TERRORISM = "terrorism"
    EXTREMISM = "extremism"
    DARK_WEB = "dark_web"
    ISSUER_FROZEN = "issuer_frozen"


class Label(Frozen):
    chain: Chain
    address: str
    entity_id: str | None = None  # stable identifier, e.g. "binance"; None if the source names no entity
    entity_name: str | None = None
    category: Category | None = None  # None → the label only carries risk flags
    risk_flags: tuple[RiskFlag, ...] = ()
    text: str  # label text exactly as the source gives it
    source_id: str  # dataset identifier, see data/sources.yaml
    source_class: SourceClass
    primary_source: str  # dereferenceable URL or document reference the claim rests on
    as_of: date | None = None
    dataset_ref: str  # where this record came from (file + sha256), for chain of custody
    synthetic: bool = False  # demo data — must never appear in a live case
    # A denial: the entity states the address is NOT theirs (e.g. a VASP's reply to a Sahyog
    # request). Removes that entity as a candidate owner — rule G-N1 (ADR-0020).
    denies: bool = False

    @model_validator(mode="after")
    def _has_content(self) -> Label:
        if self.denies:
            if not self.entity_id:
                raise ValueError("a denial must name the entity that denies ownership")
            if self.risk_flags:
                raise ValueError("a denial cannot carry risk flags")
        elif self.category is None and not self.risk_flags:
            raise ValueError("a label must carry a category or at least one risk flag")
        if not self.primary_source.strip():
            raise ValueError("a label must cite a primary source")
        return self

    @property
    def independence_key(self) -> str:
        """Two labels corroborate each other only if their primary sources differ."""
        src = self.primary_source.strip().lower()
        for prefix in ("https://", "http://"):
            if src.startswith(prefix):
                src = src[len(prefix) :]
        src = src.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        if src.startswith("www."):
            src = src[4:]
        return src


# --------------------------------------------------------------------------- inferences


class Grade(StrEnum):
    """Evidence grade of an ownership attribution. Not a probability — see ADR-0002."""

    A = "A"  # attested by the entity itself or a public authority
    B = "B"  # two or more independent curated sources agree, none disagree
    C = "C"  # a single curated source, or weak sources only
    X = "X"  # sources disagree on the owner — blocked from automatic routing

    @property
    def rank(self) -> int:
        return {"A": 3, "B": 2, "C": 1, "X": 0}[self.value]


class DerivedLink(Frozen):
    rule: str  # e.g. "D-COSPEND"
    anchor_chain: Chain
    anchor_address: str
    anchor_grade: Grade
    tx_hashes: tuple[str, ...]  # the on-chain facts the derivation rests on
    evidence_ids: tuple[str, ...] = ()


class Attribution(Frozen):
    chain: Chain
    address: str
    entity_id: str | None
    entity_name: str | None
    category: Category | None
    grade: Grade
    rule: str
    explanation: str
    labels: tuple[Label, ...] = ()
    # Source class each label actually earned after the source-trust check (aligned with `labels`).
    effective_classes: tuple[SourceClass, ...] = ()
    trust_notes: tuple[str, ...] = ()  # why a label's claimed class was lowered, if it was
    derived: DerivedLink | None = None
    conflicts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _grounded(self) -> Attribution:
        if not self.labels and self.derived is None:
            raise ValueError("an attribution must rest on labels or on a derivation")
        if self.grade is Grade.X and not self.conflicts:
            raise ValueError("grade X requires the conflicting entities to be listed")
        return self

    @property
    def is_vasp(self) -> bool:
        return self.category in VASP_CATEGORIES

    @property
    def is_service(self) -> bool:
        return self.category in SERVICE_CATEGORIES


class RiskHit(Frozen):
    chain: Chain
    address: str
    flags: tuple[RiskFlag, ...]
    labels: tuple[Label, ...]


class EndpointKind(StrEnum):
    VASP = "vasp"  # funds entered a VASP's custody — the target of a disclosure/freeze request
    SERVICE = "service"  # a non-VASP service (mixer, bridge, DeFi, gambling ...) — obfuscation or exit
    COINJOIN_LIKE = "coinjoin_like"  # Bitcoin tx with CoinJoin structure — deterministic linking stops here
    UNLABELED_CONTRACT = "unlabeled_contract"  # EVM contract with no label — needs manual review
    HIGH_ACTIVITY = "high_activity"  # unlabelled address too busy to expand — possibly an unknown service
    DORMANT = "dormant"  # no qualifying outgoing movement observed after arrival — funds may still be here
    ORIGIN = "origin"  # (funding trace) no qualifying earlier incoming transfer — origin of the observed funds
    HOP_LIMIT = "hop_limit"  # search depth exhausted
    NOT_EXPANDED = "not_expanded"  # search budget exhausted
    SOURCE_ERROR = "source_error"  # data source failed — coverage gap, reported as such


class AssetAmount(Frozen):
    asset: Asset
    amount: int

    @property
    def formatted(self) -> str:
        return self.asset.format(self.amount)


class Endpoint(Frozen):
    kind: EndpointKind
    chain: Chain
    address: str
    hops: int
    path: tuple[str, ...]  # transfer ids from the subject to this endpoint
    attribution: Attribution | None = None
    adjacent_address: str | None = None  # the path address next to a service (candidate deposit / withdrawal address)
    adjacent_role: str | None = None  # rule-based role of adjacent_address, if any rule fired
    bottleneck: AssetAmount | None = None  # upper bound on value that could have moved along this path
    notes: tuple[str, ...] = ()

    _drop_id = model_validator(mode="before")(classmethod(lambda cls, data: drop_derived_id(data)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        digest = hashlib.sha256("|".join(self.path).encode()).hexdigest()[:12]
        return f"{self.kind}:{self.chain}:{self.address}:{digest}"


def utc_from_timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
