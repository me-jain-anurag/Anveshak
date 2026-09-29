// Anveshak dashboard. All server data is inserted through esc() — label texts come from
// third-party datasets and must never be interpreted as HTML.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a) => (a && a.length > 18 ? `${a.slice(0, 8)}…${a.slice(-6)}` : a || "");
const dirText = (d) => (d === "out" ? "funds out" : "funding in");
let selected = null;
let pollTimer = null;
let cy = null;
let CHAINS = [];

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof body === "object" ? JSON.stringify(body.detail ?? body) : body);
  return body;
}

// ------------------------------------------------------------------ tabs & meta

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${b.dataset.tab}`));
    ({ alerts: loadAlerts, watch: loadWatch, analytics: loadAnalytics }[b.dataset.tab] || (() => {}))();
  })
);

async function loadMeta() {
  try {
    const m = await api("/v1/meta");
    CHAINS = m.chains;
    for (const sel of ["#chain-select", "#watch-chain", "#lookup-chain"]) {
      $(sel).innerHTML = m.chains.map((c) => `<option value="${esc(c.chain)}">${esc(c.name)}${c.configured ? "" : " (not configured)"}</option>`).join("");
    }
    $("#meta").textContent = `${m.labels.count.toLocaleString()} labels · policy v${m.policy.version} · ${m.chains.filter((c) => c.configured).length}/${m.chains.length} chains ready · v${m.version}`;
  } catch (e) {
    $("#meta").textContent = `meta unavailable: ${e.message}`;
  }
}

async function refreshAlertCount() {
  try {
    const a = await api("/v1/alerts?unacknowledged=true&limit=1000");
    const serious = a.filter((x) => x.severity === "critical" || x.severity === "high").length;
    $("#alert-count").textContent = serious || "";
  } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------------ cases

async function loadCases() {
  const list = await api("/v1/cases");
  $("#cases").innerHTML = list.map((c) => `<li data-id="${esc(c.case_id)}" class="${c.case_id === selected ? "sel" : ""}">
      <div><strong>${esc(c.case_reference)}</strong></div>
      <div class="small muted">${esc(c.data_mode)} · ${esc(c.status)} · ${esc(c.created_at.slice(0, 19).replace("T", " "))}</div></li>`).join("");
  document.querySelectorAll("#cases li").forEach((li) => (li.onclick = () => showCase(li.dataset.id)));
}

$("#case-form [name=address]").addEventListener("input", async (ev) => {
  const v = ev.target.value.trim();
  if (v.length < 25) { $("#detect-hint").textContent = ""; return; }
  try {
    const r = await api(`/v1/detect/${encodeURIComponent(v)}`);
    if (!r.chains.length) { $("#detect-hint").textContent = "not a valid address on any supported chain (format/checksum)"; return; }
    $("#detect-hint").textContent = `valid on: ${r.chains.join(", ")}`;
    $("#chain-select").value = r.chains[0];
  } catch (e) { /* ignore */ }
});

$("#case-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#form-error").textContent = "";
  const f = new FormData(ev.target);
  const directions = [];
  if (f.get("out")) directions.push("out");
  if (f.get("in")) directions.push("in");
  const payload = {
    mode: "live", case_reference: f.get("case_reference").trim(),
    subjects: [{ chain: f.get("chain"), address: f.get("address").trim() }],
    directions, max_hops: Number(f.get("max_hops")), max_expansions: Number(f.get("max_expansions")),
    follow_cross_chain: !!f.get("xchain"),
  };
  if (f.get("since")) payload.since = new Date(f.get("since")).toISOString();
  try {
    const { case_id } = await api("/v1/cases", { method: "POST", body: JSON.stringify(payload) });
    await loadCases();
    showCase(case_id);
  } catch (e) { $("#form-error").textContent = e.message; }
});

$("#demo-btn").addEventListener("click", async () => {
  const { case_id } = await api("/v1/cases", { method: "POST", body: JSON.stringify({ mode: "synthetic" }) });
  await loadCases();
  showCase(case_id);
});

async function showCase(id) {
  selected = id;
  clearTimeout(pollTimer);
  const c = await api(`/v1/cases/${id}`);
  loadCases();
  const view = $("#case-view");
  if (c.status === "queued" || c.status === "running") {
    view.innerHTML = `<div class="card"><h2>${esc(c.case_reference)}</h2><p class="muted">Case is ${esc(c.status)}… live traces respect API rate limits and can take a few minutes.</p></div>`;
    pollTimer = setTimeout(() => showCase(id), 2000);
    return;
  }
  if (c.status === "failed" || c.status === "legacy") {
    view.innerHTML = `<div class="card"><h2>${esc(c.case_reference)}</h2><p class="error">Failed: ${esc(c.error)}</p></div>`;
    return;
  }
  renderCase(c);
  refreshAlertCount();
}

function fmtAmount(b) {
  const a = b.asset;
  const s = BigInt(b.amount).toString().padStart(a.decimals + 1, "0");
  const whole = s.slice(0, s.length - a.decimals) || "0";
  const frac = a.decimals ? s.slice(s.length - a.decimals).replace(/0+$/, "") : "";
  return `${whole}${frac ? "." + frac : ""} ${a.symbol}`;
}

function renderCase(c) {
  const f = c.result.findings;
  const synthetic = f.data_mode === "synthetic";
  const approved = new Set((c.approvals || []).map((a) => a.decision_id));
  const cont = Object.fromEntries(f.continuations.map((x) => [x.trace_index, f.crosschain_links.find((l) => l.id === x.link_id)]));
  let h = synthetic ? `<div class="banner">SYNTHETIC DEMO DATA — fictional entities and transactions, not evidence</div>` : "";
  h += `<div class="card"><h2>${esc(f.request.case_reference)}</h2><div class="kv">
      <span class="muted">Case id</span><span class="mono">${esc(c.case_id)}</span>
      <span class="muted">Findings hash</span><span class="mono">${esc(c.findings_hash)}</span>
      <span class="muted">Scoring policy</span><span class="mono">v${esc(f.policy_version)} · ${esc(f.policy_snapshot.slice(0, 16))}…</span>
      <span class="muted">Evidence objects</span><span>${f.evidence_ids.length}</span>
    </div>
    <p><a href="/v1/cases/${esc(c.case_id)}/report" target="_blank" rel="noopener">Full report ↗</a> ·
       <a href="/v1/cases/${esc(c.case_id)}/export/neo4j">Neo4j (Cypher)</a> ·
       <a href="/v1/cases/${esc(c.case_id)}/export/graphml">GraphML</a> ·
       <a href="/v1/cases/${esc(c.case_id)}/export/json">JSON</a></p></div>`;

  // nearest VASPs
  const nearest = f.analyses.flatMap((a, i) => a.nearest_vasps.map((n) => ({ ...n, trace: f.traces[i], i })));
  h += `<h3>Nearest VASPs</h3>`;
  if (!nearest.length) h += `<p class="muted">No VASP reached on any traced path.</p>`;
  else {
    h += `<div class="table-wrap"><table><tr><th>Subject</th><th>#</th><th>VASP</th><th>Hops</th><th>Grade</th><th>Confidence</th><th>Deposit / withdrawal address</th></tr>`;
    for (const n of nearest) {
      h += `<tr><td class="mono small">${esc(n.trace.chain)} ${esc(short(n.trace.subject))}<div class="muted">${dirText(n.trace.params.direction)}${cont[n.i] ? " · via " + esc(cont[n.i].protocol) : ""}</div></td>
        <td>${n.rank}</td><td>${esc(n.entity_name || "unresolved owner (conflict)")}</td><td>${n.hops}</td>
        <td><span class="grade grade-${esc(n.grade)}">${esc(n.grade)}</span></td>
        <td><span class="score band-${esc(n.band)}">${n.confidence}</span> <span class="small muted">${esc(n.band)}</span></td>
        <td class="mono small">${esc(n.deposit_address || n.address)}</td></tr>`;
    }
    h += `</table></div>`;
  }

  // risk + alerts
  h += `<h3>Wallet risk</h3><div class="table-wrap"><table><tr><th>Wallet</th><th>Risk</th><th>Top contributions</th></tr>`;
  for (const r of f.subject_risks) {
    const items = r.items.filter((i) => i.counted).map((i) => `${esc(i.component)} ${i.points}: ${esc(i.reason)}`).join("<br>") || "no indicators observed in traced data";
    h += `<tr><td class="mono small">${esc(r.chain)} ${esc(short(r.address))}</td><td><span class="lvl-${esc(r.level)}">${esc(r.level.replace("_", " "))}</span> (${r.score}/100)</td><td class="small">${items}</td></tr>`;
  }
  h += `</table></div>`;
  const alerts = f.analyses.flatMap((a) => a.alerts);
  if (alerts.length) {
    h += `<h3>Alerts</h3><div class="table-wrap"><table>`;
    const seen = new Set();
    for (const a of alerts) {
      const k = a.rule + a.message; if (seen.has(k)) continue; seen.add(k);
      h += `<tr><td class="sev-${esc(a.severity)}">${esc(a.severity)}</td><td class="mono small">${esc(a.rule)}</td><td>${esc(a.message)}</td></tr>`;
    }
    h += `</table></div>`;
  }

  // routing
  h += `<h3>Request drafts</h3>`;
  if (!f.routing.length) h += `<p class="muted">No VASP or issuer reached on any traced path.</p>`;
  else {
    h += `<div class="table-wrap"><table><tr><th>Target</th><th>Request</th><th>Grade</th><th>Confidence</th><th>Status</th><th>Reason</th><th></th></tr>`;
    for (const d of f.routing) {
      const canApprove = d.status === "ready_for_approval" && !synthetic && !approved.has(d.id);
      h += `<tr><td>${esc(d.target_name)}<div class="small muted">${esc(d.chain)} · ${dirText(d.direction)}</div></td>
        <td>${esc(d.request_type.replace("_", " "))}</td>
        <td>${d.grade ? `<span class="grade grade-${esc(d.grade)}">${esc(d.grade)}</span>` : "—"}</td>
        <td>${d.confidence !== null ? `<span class="score band-${esc(d.confidence_band)}">${d.confidence}</span>` : "—"}</td>
        <td class="st-${esc(d.status)}">${esc(d.status.replaceAll("_", " "))}${approved.has(d.id) ? "<div class='small'>approved</div>" : ""}</td>
        <td class="small">${esc(d.reasons.join("; "))}<details><summary>draft</summary><pre class="small">${esc(d.draft_text)}</pre></details></td>
        <td>${canApprove ? `<button class="small" data-approve="${esc(d.id)}">Approve</button>` : ""}</td></tr>`;
    }
    h += `</table></div>`;
  }

  if (f.crosschain_links.length) {
    h += `<h3>Cross-chain movements</h3><div class="table-wrap"><table><tr><th>Rule</th><th>From</th><th>To</th><th>Destination check</th></tr>`;
    for (const l of f.crosschain_links) {
      h += `<tr><td class="mono small">${esc(l.rule)}</td><td class="mono small">${esc(l.from_chain)} ${esc(short(l.from_tx))}</td>
        <td class="mono small">${esc(l.to_chain || l.to_chain_code)} ${esc(short(l.to_address))}<div class="muted">${esc(l.asset_in)} → ${esc(l.asset_out)}</div></td>
        <td class="small">${l.destination_confirmed === true ? "confirmed" : l.destination_confirmed === false ? "NOT FOUND" : "not confirmed"} <span class="muted">${esc(l.destination_detail || "")}</span></td></tr>`;
    }
    h += `</table></div>`;
  }

  h += `<h3>Fund flow</h3><div class="legend">
    <span style="--dot:#1f5fa8">subject</span><span style="--dot:#1f7a3f">VASP</span><span style="--dot:#7a3fb0">service / mixer</span>
    <span style="--dot:#d9822b">dormant</span><span style="--dot:#8a8f98">hop</span><span style="--dot:#b3261e">conflict / high activity</span>
    <span class="muted">dashed = cross-chain · click an edge to open the transaction</span></div><div id="graph"></div>`;

  f.traces.forEach((t, i) => {
    const an = f.analyses[i];
    const scores = Object.fromEntries(an.confidences.map((s) => [s.endpoint_id, s]));
    h += `<h3>${esc(t.chain)} · <span class="mono">${esc(short(t.subject))}</span> · ${dirText(t.params.direction)}${cont[i] ? ` · <span class="muted">cross-chain continuation via ${esc(cont[i].protocol)}</span>` : ""}</h3>`;
    if (!t.endpoints.length) { h += `<p class="muted">No endpoints (${esc(t.coverage.subject_note || "nothing to follow")}).</p>`; return; }
    h += `<div class="table-wrap"><table><tr><th>Endpoint</th><th>Address</th><th>Attribution</th><th>Confidence</th><th>Hops</th><th>Bottleneck</th></tr>`;
    for (const e of t.endpoints) {
      const a = e.attribution, s = scores[e.id];
      h += `<tr><td>${esc(e.kind.replaceAll("_", " "))}${e.notes.map((n) => `<div class="small muted">${esc(n)}</div>`).join("")}</td>
        <td class="mono">${esc(e.address)}${e.adjacent_role ? `<div class="small muted">via ${esc(e.adjacent_address)} — ${esc(e.adjacent_role)}</div>` : ""}</td>
        <td>${a ? `<span class="grade grade-${esc(a.grade)}">${esc(a.grade)}</span> ${esc(a.entity_name || "conflict")} <span class="small muted">${esc(a.rule)}</span>` : "<span class='muted'>—</span>"}</td>
        <td>${s ? `<span class="score band-${esc(s.band)}" title="${esc(s.items.map((i) => `${i.component} ${i.points}: ${i.reason}`).join("\n"))}">${s.score}</span>` : "—"}</td>
        <td>${e.hops}</td><td>${e.bottleneck ? esc(fmtAmount(e.bottleneck)) : "—"}</td></tr>`;
    }
    h += `</table></div>`;
    if (an.typologies.length) h += `<p class="small"><strong>Typologies:</strong> ${an.typologies.map((x) => `${esc(x.rule)} — ${esc(x.detail)}`).join("<br>")}</p>`;
    const cl = an.clusters.filter((x) => x.kind === "multi_input" || x.members.length > 1);
    if (cl.length) h += `<p class="small"><strong>Clusters:</strong> ${cl.map((x) => `${esc(x.kind)} ${esc(x.entity_name || x.entity_id || "")} (${x.members.length} addresses${x.contains_subject ? ", contains the subject" : ""})`).join("; ")}</p>`;
    const cv = t.coverage;
    h += `<p class="small muted">Coverage: ${cv.addresses_expanded} expanded · not followed: ${cv.excluded_time_order} time-order, ${cv.excluded_dust} dust, ${cv.excluded_unverified_asset} unverified token, ${cv.excluded_other_asset} other asset${cv.budget_exhausted ? " · <strong>budget exhausted</strong>" : ""}${cv.incomplete_histories.length ? ` · ${cv.incomplete_histories.length} incomplete histories` : ""}${cv.source_errors.length ? ` · ${cv.source_errors.length} source errors` : ""}</p>`;
  });
  $("#case-view").innerHTML = h;
  document.querySelectorAll("[data-approve]").forEach((b) => (b.onclick = () => approve(c.case_id, b.dataset.approve)));
  drawGraph(c.case_id);
}

async function approve(caseId, decisionId) {
  const name = prompt("Approving officer — full name");
  if (!name) return;
  const officerId = prompt("Officer ID / designation");
  if (!officerId) return;
  try {
    const r = await api(`/v1/cases/${caseId}/routing/${decisionId}/approve`, { method: "POST", body: JSON.stringify({ officer_name: name, officer_id: officerId }) });
    alert(`Approved. Gateway (${r.receipt.mode}) wrote ${r.receipt.path}\nsha256 ${r.receipt.payload_sha256}`);
    showCase(caseId);
  } catch (e) { alert(`Not approved: ${e.message}`); }
}

const KIND_COLOR = {
  subject: "#1f5fa8", vasp: "#1f7a3f", service: "#7a3fb0", dormant: "#d9822b", origin: "#5b6470",
  coinjoin_like: "#7a3fb0", unlabeled_contract: "#8a5a2b", high_activity: "#b3261e", hop: "#8a8f98",
  hop_limit: "#b8bec6", not_expanded: "#b8bec6", source_error: "#b3261e",
};

async function drawGraph(caseId) {
  if (typeof cytoscape === "undefined") { $("#graph").textContent = "Graph library unavailable (offline). Tables and report are complete."; return; }
  const g = await api(`/v1/cases/${caseId}/graph`);
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $("#graph"),
    elements: [...g.nodes, ...g.edges],
    style: [
      { selector: "node", style: { "background-color": (n) => KIND_COLOR[n.data("kind")] || "#8a8f98", label: "data(label)", "font-size": 9, "text-wrap": "wrap", "text-valign": "bottom", "text-margin-y": 3, color: dark ? "#e6e9ee" : "#1b1f24", width: 18, height: 18 } },
      { selector: "node[grade = 'X']", style: { "border-width": 3, "border-color": "#b3261e" } },
      { selector: "node[kind = 'subject']", style: { width: 26, height: 26 } },
      { selector: "edge", style: { width: 1.5, "curve-style": "bezier", "target-arrow-shape": "triangle", "line-color": dark ? "#4a525c" : "#b8bec6", "target-arrow-color": dark ? "#4a525c" : "#b8bec6", label: "data(label)", "font-size": 8, color: dark ? "#9aa4b0" : "#5b6470", "text-rotation": "autorotate" } },
      { selector: "edge[?crosschain]", style: { "line-style": "dashed", "line-color": "#d9822b", "target-arrow-color": "#d9822b", width: 2.5 } },
    ],
    layout: { name: "breadthfirst", directed: true, roots: g.nodes.filter((n) => n.data.kind === "subject").map((n) => "#" + CSS.escape(n.data.id)), spacingFactor: 1.1, padding: 20 },
    wheelSensitivity: 0.2,
  });
  cy.on("tap", "edge", (ev) => ev.target.data("url") && window.open(ev.target.data("url"), "_blank", "noopener"));
}

// ------------------------------------------------------------------ intake

$("#intake-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const wallets = f.get("wallets").split(/\s+/).map((w) => w.trim()).filter(Boolean);
  const out = $("#intake-out");
  try {
    const r = await api("/v1/sahyog/reports", { method: "POST", body: JSON.stringify({ sahyog_reference: f.get("sahyog_reference"), agency: f.get("agency") || null, wallets }) });
    out.innerHTML = renderIntake(r) + `<p><button class="small" id="open-case">Open case</button></p>`;
    $("#open-case").onclick = () => { document.querySelector('[data-tab="cases"]').click(); showCase(r.case_id); };
  } catch (e) {
    let detail = e.message;
    try { const d = JSON.parse(e.message); detail = renderIntake(d); } catch (_) { detail = `<p class="error">${esc(e.message)}</p>`; }
    out.innerHTML = detail;
  }
});

function renderIntake(r) {
  let h = "";
  if (r.accepted?.length) h += `<h3>Accepted</h3><ul>${r.accepted.map((a) => `<li class="mono small">${esc(a.input)} → ${esc(a.chains.join(", "))}</li>`).join("")}</ul>`;
  if (r.rejected?.length) h += `<h3>Rejected</h3><ul>${r.rejected.map((a) => `<li class="small"><span class="mono">${esc(a.input)}</span> — ${esc(a.reason)}</li>`).join("")}</ul>`;
  if (r.message) h = `<p class="error">${esc(r.message)}</p>` + h;
  return h;
}

// ------------------------------------------------------------------ alerts / watchlist / analytics / lookup

async function loadAlerts() {
  const list = await api("/v1/alerts?limit=200");
  $("#alerts").innerHTML = list.length ? `<div class="table-wrap"><table><tr><th>When</th><th>Severity</th><th>Rule</th><th>Alert</th><th>Case</th><th></th></tr>${list.map((a) => `
    <tr><td class="small">${esc(a.created_at.slice(0, 19).replace("T", " "))}</td><td class="sev-${esc(a.severity)}">${esc(a.severity)}</td><td class="mono small">${esc(a.rule)}</td>
    <td>${esc(a.message)}</td><td class="mono small">${a.case_id ? `<a href="#" data-case="${esc(a.case_id)}">${esc(a.case_id.slice(0, 8))}</a>` : ""}</td>
    <td>${a.acknowledged ? "<span class='muted small'>ack</span>" : `<button class="small" data-ack="${a.alert_id}">Ack</button>`}</td></tr>`).join("")}</table></div>` : `<p class="muted">No alerts.</p>`;
  document.querySelectorAll("[data-ack]").forEach((b) => (b.onclick = async () => { await api(`/v1/alerts/${b.dataset.ack}/ack`, { method: "POST" }); loadAlerts(); refreshAlertCount(); }));
  document.querySelectorAll("[data-case]").forEach((a) => (a.onclick = (ev) => { ev.preventDefault(); document.querySelector('[data-tab="cases"]').click(); showCase(a.dataset.case); }));
}

async function loadWatch() {
  const list = await api("/v1/watchlist");
  $("#watchlist").innerHTML = list.length ? `<div class="table-wrap"><table><tr><th>Chain</th><th>Address</th><th>Reason</th><th>Last checked</th><th></th></tr>${list.map((w) => `
    <tr><td>${esc(w.chain)}</td><td class="mono">${esc(w.address)}</td><td class="small">${esc(w.reason)}</td><td class="small">${esc((w.last_checked_at || "not yet").slice(0, 19).replace("T", " "))}</td>
    <td><button class="small" data-unwatch="${w.watch_id}">Stop</button></td></tr>`).join("")}</table></div>` : `<p class="muted">Nothing watched yet.</p>`;
  document.querySelectorAll("[data-unwatch]").forEach((b) => (b.onclick = async () => { await api(`/v1/watchlist/${b.dataset.unwatch}`, { method: "DELETE" }); loadWatch(); }));
}

$("#watch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try { await api("/v1/watchlist", { method: "POST", body: JSON.stringify({ chain: f.get("chain"), address: f.get("address").trim(), reason: f.get("reason") }) }); ev.target.reset(); loadWatch(); }
  catch (e) { alert(e.message); }
});

async function loadAnalytics() {
  const a = await api("/v1/analytics");
  const kpi = (v, k) => `<div class="kpi"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`;
  let h = `<div class="kpis">${kpi(a.cases.done || 0, "cases completed")}${kpi(a.cases.queued || 0, "queued")}${kpi(a.cases.failed || 0, "failed")}
    ${kpi(a.vasps_reached.length, "VASPs reached")}${kpi(a.cross_chain_links, "cross-chain links")}${kpi(a.watchlist_active, "addresses watched")}
    ${kpi((a.alerts_by_severity.critical || 0) + (a.alerts_by_severity.high || 0), "critical/high alerts")}</div>`;
  const max = Math.max(1, ...a.vasps_reached.map((v) => v.cases));
  h += `<h3>VASPs reached</h3><div class="table-wrap"><table><tr><th>VASP</th><th>Cases</th><th></th><th>Ready drafts</th><th>Best confidence</th><th>Nearest hops</th></tr>${a.vasps_reached.map((v) => `
    <tr><td>${esc(v.vasp)}</td><td>${v.cases}</td><td style="width:30%"><div class="bar" style="width:${(100 * v.cases) / max}%"></div></td><td>${v.ready_drafts}</td><td>${v.best_confidence}</td><td>${v.min_hops ?? "—"}</td></tr>`).join("")}</table></div>`;
  const table = (title, obj) => `<h3>${esc(title)}</h3><div class="table-wrap"><table>${Object.entries(obj).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") || "<tr><td class='muted'>none</td></tr>"}</table></div>`;
  h += table("Typologies detected", a.typologies) + table("Subject risk levels", a.subject_risk_levels) + table("Traces by chain", a.traces_by_chain) + table("Drafts by type and status", a.routing);
  $("#analytics").innerHTML = h;
}

$("#lookup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const out = $("#lookup-out");
  try {
    const r = await api(`/v1/addresses/${encodeURIComponent(f.get("chain"))}/${encodeURIComponent(f.get("address").trim())}`);
    const a = r.attribution;
    out.innerHTML = (a ? `<p><span class="grade grade-${esc(a.grade)}">${esc(a.grade)}</span> ${esc(a.entity_name || "conflict")} — ${esc(a.explanation)}</p>${a.trust_notes.map((n) => `<p class="error">${esc(n)}</p>`).join("")}`
      : `<p class="muted">No ownership label for this address.</p>`)
      + (r.risk ? `<p>Risk flags: ${esc(r.risk.flags.join(", "))}</p>` : "")
      + (r.seen_in_cases.length ? `<p>Seen in ${r.seen_in_cases.length} case(s): ${r.seen_in_cases.map((s) => `${esc(s.case_id.slice(0, 8))} (${esc(s.role)})`).join(", ")}</p>` : "");
  } catch (e) { out.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
});

loadMeta();
loadCases();
refreshAlertCount();
setInterval(refreshAlertCount, 30000);
