"""Fund-flow graph (Cytoscape.js elements) derived from trace results — presentation only."""

from __future__ import annotations

from .domain import EndpointKind
from .tracer import TraceResult


def _short(address: str) -> str:
    return address if len(address) <= 14 else f"{address[:6]}…{address[-5:]}"


def build_graph(traces: list[TraceResult], links: list | None = None) -> dict:
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}

    def node(chain: str, address: str) -> dict:
        key = f"{chain}:{address}"
        if key not in nodes:
            nodes[key] = {"data": {"id": key, "chain": chain, "address": address, "label": _short(address), "kind": "hop"}}
        return nodes[key]

    for trace in traces:
        att = {a.address: a for a in trace.attributions}
        risk = {r.address: r for r in trace.risks}
        subject = node(trace.chain, trace.subject)
        subject["data"]["kind"] = "subject"
        for t in trace.transfers:
            node(t.chain, t.sender)
            node(t.chain, t.receiver)
            edges[t.id] = {
                "data": {
                    "id": t.id,
                    "source": f"{t.chain}:{t.sender}",
                    "target": f"{t.chain}:{t.receiver}",
                    "label": t.formatted_amount,
                    "tx": t.tx_hash,
                    "time": t.timestamp.isoformat(),
                    "url": t.chain.tx_url(t.tx_hash),
                }
            }
        for e in trace.endpoints:
            n = node(e.chain, e.address)
            if n["data"]["kind"] == "subject":
                continue
            if e.kind in (EndpointKind.HOP_LIMIT, EndpointKind.NOT_EXPANDED) and n["data"]["kind"] != "hop":
                continue
            n["data"]["kind"] = e.kind.value
        for n in nodes.values():
            address = n["data"]["address"]
            a = att.get(address)
            if a and n["data"]["chain"] == trace.chain:
                n["data"]["entity"] = a.entity_name or "conflict"
                n["data"]["grade"] = a.grade.value
                n["data"]["label"] = f"{a.entity_name or 'CONFLICT'} [{a.grade.value}]\n{_short(address)}"
            r = risk.get(address)
            if r and n["data"]["chain"] == trace.chain:
                n["data"]["risk"] = ", ".join(r.flags)
    for link in links or []:
        if link.to_chain is None or not link.to_address:
            continue
        target = node(link.to_chain.value, link.to_address)
        edges[link.id] = {
            "data": {
                "id": link.id,
                "source": f"{link.from_chain.value}:{link.from_address}",
                "target": target["data"]["id"],
                "label": f"{link.protocol}: {link.asset_out}",
                "tx": link.to_tx or "",
                "time": "",
                "url": link.to_chain.tx_url(link.to_tx) if link.to_tx else "",
                "crosschain": True,
            }
        }
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}
