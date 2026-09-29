"""Address roles, clusters and automated tags (ADR-0013).

Roles (each with the rule that assigned it):
  SUBJECT            the address under investigation
  DEPOSIT_ADDRESS    R-SWEEP (account chains): all of its later transfers went to one VASP
                     R-CONSOLIDATION (Bitcoin): received from outside, then spent together with a VASP's addresses
  HOT_WALLET         R-SWEEP-TARGET: receives a sweep from a deposit-pattern address
                     R-LABEL-ROLE: a label names it a hot wallet
  COLD_WALLET / RESERVE_WALLET   R-LABEL-ROLE: a label names it so
  SERVICE_ADDRESS    attributed to a service (VASP, mixer, bridge ...)
  CONTRACT           unlabelled smart contract
  TRANSIT            T-PASS rapid pass-through
  HOLDING            value stopped here (dormant) · ORIGIN · HIGH_ACTIVITY

Clusters:
  entity:<chain>:<id>   addresses attributed to one entity, plus deposit addresses tied to it
  C-MULTI-INPUT         Bitcoin: addresses spent together in non-CoinJoin transactions share a
                        controller (Meiklejohn et al. 2013). The cluster containing the subject
                        reveals other addresses of the subject's own wallet.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from enum import StrEnum

from .domain import EndpointKind, Frozen, Grade
from .tracer import TraceResult
from .typologies import Typology, TypologyHit


class Role(StrEnum):
    SUBJECT = "subject"
    DEPOSIT_ADDRESS = "deposit_address"
    HOT_WALLET = "hot_wallet"
    COLD_WALLET = "cold_wallet"
    RESERVE_WALLET = "reserve_wallet"
    SERVICE_ADDRESS = "service_address"
    CONTRACT = "contract"
    TRANSIT = "transit"
    HOLDING = "holding"
    ORIGIN = "origin"
    HIGH_ACTIVITY = "high_activity"


class RoleTag(Frozen):
    role: Role
    rule: str
    detail: str


class ClusterMember(Frozen):
    address: str
    basis: str  # rule that placed it in the cluster


class Cluster(Frozen):
    cluster_id: str
    chain: str
    kind: str  # "entity" | "multi_input"
    entity_id: str | None
    entity_name: str | None
    contains_subject: bool
    members: tuple[ClusterMember, ...]


class AddressProfile(Frozen):
    chain: str
    address: str
    roles: tuple[RoleTag, ...]
    tags: tuple[str, ...]
    cluster_ids: tuple[str, ...]


_LABEL_ROLES = [
    (re.compile(r"\bhot\s*wallet", re.I), Role.HOT_WALLET),
    (re.compile(r"\bcold\s*(wallet|storage)", re.I), Role.COLD_WALLET),
    (re.compile(r"\breserve", re.I), Role.RESERVE_WALLET),
    (re.compile(r"\bdeposit", re.I), Role.DEPOSIT_ADDRESS),
]


def _cluster_id(prefix: str, members: list[str]) -> str:
    return f"{prefix}:{hashlib.sha256('|'.join(sorted(members)).encode()).hexdigest()[:12]}"


def _multi_input_clusters(trace: TraceResult) -> list[list[str]]:
    parent: dict[str, str] = {}

    def find(a: str) -> str:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    seen_tx = set()
    for t in trace.transfers:
        if t.utxo is None or t.utxo.coinjoin_like or t.tx_hash in seen_tx or len(t.utxo.input_addresses) < 2:
            continue
        seen_tx.add(t.tx_hash)
        first, *rest = t.utxo.input_addresses
        for other in rest:
            parent[find(other)] = find(first)
    groups: dict[str, list[str]] = defaultdict(list)
    for a in list(parent):
        groups[find(a)].append(a)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def build_profiles(trace: TraceResult, typologies: list[TypologyHit]) -> tuple[list[AddressProfile], list[Cluster]]:
    chain = trace.chain.value
    roles: dict[str, dict[tuple, RoleTag]] = defaultdict(dict)
    tags: dict[str, set[str]] = defaultdict(set)
    att = {a.address: a for a in trace.attributions}

    def add(address: str, role: Role, rule: str, detail: str) -> None:
        roles[address][(role, rule)] = RoleTag(role=role, rule=rule, detail=detail)
        tags[address].add(f"role:{role.value}")

    add(trace.subject, Role.SUBJECT, "input", "address under investigation")
    for t in trace.transfers:
        tags[t.sender]
        tags[t.receiver]

    entity_members: dict[str, dict[str, str]] = defaultdict(dict)
    entity_names: dict[str, str | None] = {}
    for address, a in att.items():
        if a.grade is Grade.X:
            tags[address].add("attribution:conflict")
            continue
        if a.entity_id:
            tags[address].add(f"entity:{a.entity_id}")
            entity_members[a.entity_id][address] = a.rule
            entity_names[a.entity_id] = a.entity_name
        if a.category:
            tags[address].add(f"category:{a.category.value}")
        tags[address].add(f"grade:{a.grade.value}")
        if a.is_service:
            add(address, Role.SERVICE_ADDRESS, a.rule, f"attributed to {a.entity_name or 'a service'} ({a.category})")
        if a.derived and a.derived.rule == "D-COSPEND":
            add(address, Role.DEPOSIT_ADDRESS, "R-CONSOLIDATION", f"received from outside, then spent together with {a.entity_name} addresses")
        for label in a.labels:
            for pattern, role in _LABEL_ROLES:
                if pattern.search(label.text):
                    add(address, role, "R-LABEL-ROLE", f"label text: {label.text!r} ({label.source_id})")

    for e in trace.endpoints:
        if e.kind is EndpointKind.VASP and e.adjacent_role and e.adjacent_address:
            add(e.adjacent_address, Role.DEPOSIT_ADDRESS, "R-SWEEP", e.adjacent_role)
            add(e.address, Role.HOT_WALLET, "R-SWEEP-TARGET", f"receives sweeps from deposit-pattern address {e.adjacent_address}")
            if e.attribution and e.attribution.entity_id:
                entity_members[e.attribution.entity_id][e.adjacent_address] = "R-SWEEP"
        elif e.kind is EndpointKind.UNLABELED_CONTRACT:
            add(e.address, Role.CONTRACT, "E-CONTRACT", "smart contract without a label")
        elif e.kind is EndpointKind.DORMANT:
            add(e.address, Role.HOLDING, "E-DORMANT", "value stopped moving here")
        elif e.kind is EndpointKind.ORIGIN:
            add(e.address, Role.ORIGIN, "E-ORIGIN", "earliest observed source of funds")
        elif e.kind is EndpointKind.HIGH_ACTIVITY:
            add(e.address, Role.HIGH_ACTIVITY, "E-HIGH-ACTIVITY", "too busy to expand; possibly an unlabelled service")

    for h in typologies:
        for address in h.addresses:
            tags[address].add(f"typology:{h.typology.value}")
        if h.typology is Typology.RAPID_PASS_THROUGH:
            add(h.addresses[0], Role.TRANSIT, "T-PASS", h.detail)
    for r in trace.risks:
        for flag in r.flags:
            tags[r.address].add(f"risk:{flag.value}")

    clusters: list[Cluster] = []
    membership: dict[str, list[str]] = defaultdict(list)
    for entity_id, members in sorted(entity_members.items()):
        cid = f"entity:{chain}:{entity_id}"
        clusters.append(
            Cluster(
                cluster_id=cid,
                chain=chain,
                kind="entity",
                entity_id=entity_id,
                entity_name=entity_names.get(entity_id),
                contains_subject=trace.subject in members,
                members=tuple(ClusterMember(address=a, basis=r) for a, r in sorted(members.items())),
            )
        )
        for a in members:
            membership[a].append(cid)
    for group in _multi_input_clusters(trace):
        cid = _cluster_id("multi_input", group)
        owners = {att[a].entity_id for a in group if a in att and att[a].entity_id and att[a].grade is not Grade.X}
        clusters.append(
            Cluster(
                cluster_id=cid,
                chain=chain,
                kind="multi_input",
                entity_id=next(iter(owners)) if len(owners) == 1 else None,
                entity_name=None,
                contains_subject=trace.subject in group,
                members=tuple(ClusterMember(address=a, basis="C-MULTI-INPUT") for a in group),
            )
        )
        for a in group:
            membership[a].append(cid)
            tags[a].add("cluster:multi_input" + (":subject" if trace.subject in group else ""))

    profiles = [
        AddressProfile(
            chain=chain,
            address=address,
            roles=tuple(sorted(roles.get(address, {}).values(), key=lambda r: (r.role.value, r.rule))),
            tags=tuple(sorted(tags[address])),
            cluster_ids=tuple(sorted(membership.get(address, []))),
        )
        for address in sorted(tags)
    ]
    return profiles, clusters
