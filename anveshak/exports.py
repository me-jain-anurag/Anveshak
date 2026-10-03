"""Exports of a case's fund-flow graph to external graph-analytics engines (ADR-0016).

  to_cypher   Neo4j / Memgraph: idempotent MERGE statements (run with cypher-shell or the browser)
  to_graphml  GraphML for Gephi, Cytoscape desktop, yEd, and tools that import GraphML

Nodes are (chain, address) with attribution, risk and role data; edges are transfers (with
tx hash, amount, asset, time) plus cross-chain links between chains.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .case import CaseResult


def _nodes_edges(result: CaseResult) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    f = result.findings
    nodes: dict[tuple[str, str], dict] = {}
    edges: list[dict] = []

    def node(chain: str, address: str) -> dict:
        return nodes.setdefault((chain, address), {"chain": chain, "address": address, "kind": "hop"})

    for trace, analysis in zip(f.traces, f.analyses):
        node(trace.chain.value, trace.subject)["kind"] = "subject"
        for t in trace.transfers:
            node(t.chain.value, t.sender)
            node(t.chain.value, t.receiver)
            edges.append(
                {"type": "TRANSFER", "from": (t.chain.value, t.sender), "to": (t.chain.value, t.receiver), "id": t.id, "tx": t.tx_hash,
                 "amount": t.formatted_amount, "asset": t.asset.key, "time": t.timestamp.isoformat(), "block": t.block_number or -1}
            )
        for e in trace.endpoints:
            n = node(e.chain.value, e.address)
            if n["kind"] != "subject":
                n["kind"] = e.kind.value
        for a in trace.attributions:
            n = node(a.chain.value, a.address)
            n.update({"entity": a.entity_id or "CONFLICT", "grade": a.grade.value, "rule": a.rule})
        for r in trace.risks:
            node(r.chain.value, r.address)["risk"] = ",".join(r.flags)
        for p in analysis.profiles:
            n = node(p.chain, p.address)
            if p.tags:
                n["tags"] = ",".join(p.tags)
    for link in f.crosschain_links:
        if link.to_chain is None or not link.to_address:
            continue
        node(link.to_chain.value, link.to_address)
        edges.append(
            {"type": "CROSS_CHAIN", "from": (link.from_chain.value, link.from_address), "to": (link.to_chain.value, link.to_address), "id": link.id,
             "tx": link.from_tx, "amount": link.amount_out, "asset": link.asset_out, "time": "", "block": -1, "protocol": link.protocol, "to_tx": link.to_tx or ""}
        )
    return nodes, edges


def _cypher_str(value: object) -> str:
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ") + "'"


def to_cypher(result: CaseResult) -> str:
    nodes, edges = _nodes_edges(result)
    case = _cypher_str(result.case_id)
    lines = [
        f"// Anveshak case {result.case_id} — findings hash {result.findings_hash}",
        "CREATE CONSTRAINT address_key IF NOT EXISTS FOR (a:Address) REQUIRE (a.chain, a.address) IS UNIQUE;",
    ]
    for (chain, address), props in sorted(nodes.items()):
        sets = ", ".join(f"a.{k} = {_cypher_str(v)}" for k, v in sorted(props.items()) if k not in ("chain", "address"))
        lines.append(f"MERGE (a:Address {{chain: {_cypher_str(chain)}, address: {_cypher_str(address)}}}) SET {sets}, a.last_case = {case};")
    for e in edges:
        (fc, fa), (tc, ta) = e["from"], e["to"]
        props = {k: v for k, v in e.items() if k not in ("from", "to", "type", "id")}
        sets = ", ".join(f"r.{k} = {_cypher_str(v) if not isinstance(v, int) else v}" for k, v in sorted(props.items()))
        lines.append(
            f"MATCH (a:Address {{chain: {_cypher_str(fc)}, address: {_cypher_str(fa)}}}), (b:Address {{chain: {_cypher_str(tc)}, address: {_cypher_str(ta)}}}) "
            f"MERGE (a)-[r:{e['type']} {{id: {_cypher_str(e['id'])}}}]->(b) SET {sets}, r.case_id = {case};"
        )
    return "\n".join(lines) + "\n"


def to_graphml(result: CaseResult) -> str:
    nodes, edges = _nodes_edges(result)
    ns = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}graphml")
    node_keys = sorted({k for props in nodes.values() for k in props})
    edge_keys = sorted({k for e in edges for k in e if k not in ("from", "to")})
    for k in node_keys:
        ET.SubElement(root, f"{{{ns}}}key", id=f"n_{k}", **{"for": "node", "attr.name": k, "attr.type": "string"})
    for k in edge_keys:
        ET.SubElement(root, f"{{{ns}}}key", id=f"e_{k}", **{"for": "edge", "attr.name": k, "attr.type": "string"})
    graph = ET.SubElement(root, f"{{{ns}}}graph", id=result.case_id, edgedefault="directed")
    for (chain, address), props in sorted(nodes.items()):
        el = ET.SubElement(graph, f"{{{ns}}}node", id=f"{chain}:{address}")
        for k, v in sorted(props.items()):
            ET.SubElement(el, f"{{{ns}}}data", key=f"n_{k}").text = str(v)
    for i, e in enumerate(edges):
        (fc, fa), (tc, ta) = e["from"], e["to"]
        el = ET.SubElement(graph, f"{{{ns}}}edge", id=f"e{i}", source=f"{fc}:{fa}", target=f"{tc}:{ta}")
        for k in edge_keys:
            if k in e:
                ET.SubElement(el, f"{{{ns}}}data", key=f"e_{k}").text = str(e[k])
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"
