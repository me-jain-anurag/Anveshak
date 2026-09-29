"""Deterministic grading of ownership attributions (ADR-0002, ADR-0005).

Grades are assigned by the rules below — nothing else. Each rule has an id that appears
in every report next to the grade, so a reader can look the rule up in docs.

  G-X1  Two or more labels name different owners .............................. grade X
  G-A1  A label from the entity itself (e.g. its proof-of-reserves list) ...... grade A
  G-A2  A label from a public authority ........................................ grade A
  G-B1  Two or more curated labels with different primary sources agree ........ grade B
  G-C1  Exactly one curated primary source ..................................... grade C
  G-C2  Only weak sources (web crawl, heuristic, unknown) ...................... grade C

Derivations — facts on the ledger linking an unlabelled address to a labelled one.
Each lowers the grade one step (A→B, B→C). They never start from C or X, and never chain
(a derived attribution is never the anchor of another derivation).

  D-COSPEND  Bitcoin: the address was spent in the same transaction as an address
             attributed (A or B) to one entity, and the transaction is not CoinJoin-like.
             Spending requires each input's key, so the inputs share a controller.
  D-EVM-KEY  EVM: the address carries an A/B attribution on another EVM chain, and is an
             externally-owned account (no contract code) on this chain. An EOA's address
             is derived from its key, so it is the same key holder on every EVM chain.
"""

from __future__ import annotations

from collections.abc import Callable

from .chain import Chain, ChainFamily
from .domain import (
    VASP_CATEGORIES,
    AddressHistory,
    Attribution,
    Category,
    DerivedLink,
    Grade,
    Label,
    RiskHit,
    SourceClass,
)
from .labels.store import LabelStore
from .sourcetrust import SourceTrust

_CATEGORY_PREFERENCE = list(Category)  # enum order: VASP categories first, OTHER last

_DOWNGRADE = {Grade.A: Grade.B, Grade.B: Grade.C}


def _pick_category(labels: list[Label]) -> Category | None:
    cats = {l.category for l in labels if l.category is not None}
    if not cats:
        return None
    vasp = [c for c in _CATEGORY_PREFERENCE if c in cats and c in VASP_CATEGORIES]
    if vasp:
        return vasp[0]
    return next(c for c in _CATEGORY_PREFERENCE if c in cats)


def _source_phrase(label: Label, klass: SourceClass) -> str:
    return f"{label.source_id} ({klass}) citing {label.primary_source}"


def grade_labels(
    chain: Chain,
    address: str,
    labels: tuple[Label, ...],
    aliases: dict[str, str] | None = None,
    trust: SourceTrust | None = None,
) -> Attribution | None:
    """Apply rules G-X1 .. G-C2.

    `aliases` maps dataset-specific entity ids to canonical ones (from the VASP directory) so
    that e.g. "crypto.com" and "cryptocom" are not a conflict. `trust` re-checks each label's
    claimed source class against who actually published it (see sourcetrust.py)."""
    ownership = [l for l in labels if l.category is not None]
    if not ownership:
        return None
    aliases = aliases or {}
    entities = sorted({aliases.get(l.entity_id, l.entity_id) for l in ownership if l.entity_id})
    category = _pick_category(ownership)

    if len(entities) > 1:
        return Attribution(
            chain=chain,
            address=address,
            entity_id=None,
            entity_name=None,
            category=category,
            grade=Grade.X,
            rule="G-X1",
            explanation=f"Grade X (G-X1): sources disagree on the owner ({', '.join(entities)}). "
            "Not routed automatically; an analyst must resolve the conflict.",
            labels=tuple(ownership),
            effective_classes=tuple(l.source_class for l in ownership),
            conflicts=tuple(entities),
        )

    entity_id = entities[0] if entities else None
    entity_name = next((l.entity_name for l in ownership if l.entity_name), None) or entity_id
    who = entity_name or "an unnamed service"
    classes: list[SourceClass] = []
    notes: list[str] = []
    for l in ownership:
        klass, note = trust.effective(l, entity_id) if trust else (l.source_class, None)
        classes.append(klass)
        if note:
            notes.append(note)
    pairs = list(zip(ownership, classes))
    attested = [l for l, c in pairs if c is SourceClass.ENTITY_ATTESTED]
    authority = [l for l, c in pairs if c is SourceClass.AUTHORITY]
    curated = [l for l, c in pairs if c is SourceClass.CURATED]
    curated_keys = sorted({l.independence_key for l in curated})

    if attested:
        grade, rule = Grade.A, "G-A1"
        why = f"{who} is named as owner by the entity itself: {_source_phrase(attested[0], SourceClass.ENTITY_ATTESTED)}."
    elif authority:
        grade, rule = Grade.A, "G-A2"
        why = f"{who} is named as owner by a public authority: {_source_phrase(authority[0], SourceClass.AUTHORITY)}."
    elif len(curated_keys) >= 2:
        grade, rule = Grade.B, "G-B1"
        why = f"{who} is named as owner by {len(curated_keys)} curated sources with different primary sources: {', '.join(curated_keys)}."
    elif curated:
        grade, rule = Grade.C, "G-C1"
        why = f"{who} is named as owner by a single curated source: {_source_phrase(curated[0], SourceClass.CURATED)}."
    else:
        grade, rule = Grade.C, "G-C2"
        why = f"{who} is named as owner only by weak sources ({', '.join(sorted({l.source_id for l in ownership}))})."

    return Attribution(
        chain=chain,
        address=address,
        entity_id=entity_id,
        entity_name=entity_name,
        category=category,
        grade=grade,
        rule=rule,
        explanation=f"Grade {grade} ({rule}): {why}",
        labels=tuple(ownership),
        effective_classes=tuple(classes),
        trust_notes=tuple(notes),
    )


class Attributor:
    def __init__(
        self,
        labels: LabelStore,
        aliases: dict[str, str] | None = None,
        trust: SourceTrust | None = None,
        providers: list | None = None,
    ):
        self.labels = labels
        self.aliases = aliases or {}
        self.trust = trust
        self.providers = providers or []  # intelligence-API label providers (intel.py)
        self._cache: dict[tuple[Chain, str], Attribution | None] = {}
        self._label_cache: dict[tuple[Chain, str], tuple[Label, ...]] = {}

    def labels_for(self, chain: Chain, address: str) -> tuple[Label, ...]:
        """Stored labels plus any returned by configured intelligence providers (cached)."""
        key = (chain, address)
        if key not in self._label_cache:
            extra: list[Label] = []
            for provider in self.providers:
                extra.extend(provider.lookup(chain, address))
            self._label_cache[key] = self.labels.get(chain, address) + tuple(extra)
        return self._label_cache[key]

    def ownership(self, chain: Chain, address: str) -> Attribution | None:
        key = (chain, address)
        if key not in self._cache:
            self._cache[key] = grade_labels(chain, address, self.labels_for(chain, address), self.aliases, self.trust)
        return self._cache[key]

    def risk(self, chain: Chain, address: str) -> RiskHit | None:
        flagged = [l for l in self.labels_for(chain, address) if l.risk_flags]
        if not flagged:
            return None
        flags = tuple(sorted({f for l in flagged for f in l.risk_flags}))
        return RiskHit(chain=chain, address=address, flags=flags, labels=tuple(flagged))

    def resolve(self, chain: Chain, address: str, is_contract: Callable[[str], bool | None] | None = None) -> Attribution | None:
        """Label-based attribution on this chain, else D-EVM-KEY if applicable."""
        direct = self.ownership(chain, address)
        if direct is not None:
            return direct
        if chain.family is ChainFamily.EVM and is_contract is not None:
            return self.derive_evm_key(chain, address, is_contract)
        return None

    def derive_evm_key(self, chain: Chain, address: str, is_contract: Callable[[str], bool | None]) -> Attribution | None:
        elsewhere = self.labels.on_other_evm_chains(chain, address)
        if not elsewhere:
            return None
        anchors = {}
        for label in elsewhere:
            anchors.setdefault(label.chain, []).append(label)
        graded = [grade_labels(c, address, tuple(ls), self.aliases, self.trust) for c, ls in sorted(anchors.items())]
        graded = [g for g in graded if g is not None]
        if not graded:
            return None
        entities = sorted({g.entity_id for g in graded if g.entity_id} | {e for g in graded for e in g.conflicts})
        if len(entities) != 1 or any(g.grade is Grade.X for g in graded):
            return None  # conflicting or anonymous across chains — no derivation
        anchor = max(graded, key=lambda g: (g.grade.rank, g.chain))
        if anchor.grade not in _DOWNGRADE:
            return None
        if is_contract(address) is not False:
            return None  # contract, or unknown: the same-key argument only holds for EOAs
        grade = _DOWNGRADE[anchor.grade]
        return Attribution(
            chain=chain,
            address=address,
            entity_id=anchor.entity_id,
            entity_name=anchor.entity_name,
            category=anchor.category,
            grade=grade,
            rule="D-EVM-KEY",
            explanation=(
                f"Grade {grade} (D-EVM-KEY): {address} is attributed to {anchor.entity_name} on {anchor.chain} "
                f"(grade {anchor.grade}, {anchor.rule}) and is an externally-owned account on {chain}, i.e. the same key holder. "
                "Grade lowered one step for the derivation."
            ),
            labels=anchor.labels,
            effective_classes=anchor.effective_classes,
            derived=DerivedLink(rule="D-EVM-KEY", anchor_chain=anchor.chain, anchor_address=address, anchor_grade=anchor.grade, tx_hashes=()),
        )

    def derive_cospend(self, history: AddressHistory) -> Attribution | None:
        """D-COSPEND for Bitcoin. Uses only transactions in which `history.address` is an input."""
        if history.chain is not Chain.BITCOIN:
            return None
        address = history.address
        hits: dict[str, list[tuple[Attribution, str]]] = {}
        seen_tx: set[str] = set()
        for t in history.transfers:
            if t.sender != address or t.utxo is None or t.tx_hash in seen_tx:
                continue
            seen_tx.add(t.tx_hash)
            if t.utxo.coinjoin_like:
                continue
            for co_input in t.utxo.input_addresses:
                if co_input == address:
                    continue
                anchor = self.ownership(Chain.BITCOIN, co_input)
                if anchor is None or anchor.grade is Grade.X or anchor.entity_id is None:
                    continue
                hits.setdefault(anchor.entity_id, []).append((anchor, t.tx_hash))
        if not hits:
            return None
        if len(hits) > 1:
            entities = tuple(sorted(hits))
            all_anchors = [a for pairs in hits.values() for a, _ in pairs]
            return Attribution(
                chain=Chain.BITCOIN,
                address=address,
                entity_id=None,
                entity_name=None,
                category=_pick_category([l for a in all_anchors for l in a.labels]),
                grade=Grade.X,
                rule="D-COSPEND",
                explanation=f"Grade X (D-COSPEND): co-spent with addresses of different entities ({', '.join(entities)}).",
                derived=DerivedLink(
                    rule="D-COSPEND",
                    anchor_chain=Chain.BITCOIN,
                    anchor_address=all_anchors[0].address,
                    anchor_grade=all_anchors[0].grade,
                    tx_hashes=tuple(sorted({tx for pairs in hits.values() for _, tx in pairs}))[:10],
                ),
                conflicts=entities,
            )
        (entity_id, pairs), = hits.items()
        best = max(pairs, key=lambda p: (p[0].grade.rank, p[0].address))[0]
        if best.grade not in _DOWNGRADE:
            return None
        grade = _DOWNGRADE[best.grade]
        txs = tuple(sorted({tx for _, tx in pairs}))
        return Attribution(
            chain=Chain.BITCOIN,
            address=address,
            entity_id=entity_id,
            entity_name=best.entity_name,
            category=best.category,
            grade=grade,
            rule="D-COSPEND",
            explanation=(
                f"Grade {grade} (D-COSPEND): {address} was spent as an input together with {best.address}, "
                f"attributed to {best.entity_name} (grade {best.grade}, {best.rule}), in {len(txs)} non-CoinJoin "
                f"transaction(s) ({', '.join(txs[:3])}{'…' if len(txs) > 3 else ''}). Grade lowered one step for the derivation."
            ),
            labels=best.labels,
            effective_classes=best.effective_classes,
            derived=DerivedLink(rule="D-COSPEND", anchor_chain=Chain.BITCOIN, anchor_address=best.address, anchor_grade=best.grade, tx_hashes=txs[:10]),
        )
