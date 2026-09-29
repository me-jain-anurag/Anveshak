// Anveshak dashboard. All server data is inserted with textContent / esc() — label texts come
// from third-party datasets and must never be interpreted as HTML.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const short = (a) => (a && a.length > 16 ? `${a.slice(0, 8)}…${a.slice(-6)}` : a || "");
let selected = null;
let pollTimer = null;
let cy = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof body === "object" ? JSON.stringify(body.detail ?? body) : body);
  return body;
}

async function loadMeta() {
  try {
    const m = await api("/v1/meta");
    const chains = m.chains.map((c) => `${c.chain}${c.configured ? "" : " (not configured)"}`).join(" · ");
    $("#meta").textContent = `${m.labels.count.toLocaleString()} labels · ${chains} · v${m.version}`;
  } catch (e) {
    $("#meta").textContent = `meta unavailable: ${e.message}`;
  }
}

async function loadCases() {
  const list = await api("/v1/cases");
  const ul = $("#cases");
  ul.innerHTML = "";
  for (const c of list) {
    const li = document.createElement("li");
    li.className = c.case_id === selected ? "sel" : "";
    li.innerHTML = `<div><strong>${esc(c.case_reference)}</strong></div>
      <div class="small muted">${esc(c.data_mode)} · ${esc(c.status)} · ${esc(c.created_at.slice(0, 19).replace("T", " "))}</div>`;
    li.onclick = () => showCase(c.case_id);
    ul.appendChild(li);
  }
}

function formPayload(form) {
  const f = new FormData(form);
  const directions = [];
  if (f.get("out")) directions.push("out");
  if (f.get("in")) directions.push("in");
  const payload = {
    mode: "live",
    case_reference: f.get("case_reference").trim(),
    subjects: [{ chain: f.get("chain"), address: f.get("address").trim() }],
    directions,
    max_hops: Number(f.get("max_hops")),
    max_expansions: Number(f.get("max_expansions")),
  };
  if (f.get("since")) payload.since = new Date(f.get("since")).toISOString();
  return payload;
}

$("#case-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#form-error").textContent = "";
  try {
    const { case_id } = await api("/v1/cases", { method: "POST", body: JSON.stringify(formPayload(ev.target)) });
    await loadCases();
    showCase(case_id);
  } catch (e) {
    $("#form-error").textContent = e.message;
  }
});

$("#demo-btn").addEventListener("click", async () => {
  const { case_id } = await api("/v1/cases", { method: "POST", body: JSON.stringify({ mode: "synthetic" }) });
  await loadCases();
  showCase(case_id);
});

$("#lookup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const out = $("#lookup-out");
  try {
    const r = await api(`/v1/addresses/${encodeURIComponent(f.get("chain"))}/${encodeURIComponent(f.get("address").trim())}`);
    const a = r.attribution;
    out.innerHTML = a
      ? `<p><span class="grade grade-${esc(a.grade)}">${esc(a.grade)}</span> ${esc(a.entity_name || "conflict")} — ${esc(a.explanation)}</p>`
      : `<p class="muted">No ownership label for this address.</p>`;
    if (r.risk) out.innerHTML += `<p>Risk flags: ${esc(r.risk.flags.join(", "))}</p>`;
  } catch (e) {
    out.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
});

async function showCase(id) {
  selected = id;
  clearTimeout(pollTimer);
  const c = await api(`/v1/cases/${id}`);
  loadCases();
  const main = $("#main");
  if (c.status === "queued" || c.status === "running") {
    main.innerHTML = `<div class="card"><h2>${esc(c.case_reference)}</h2><p class="muted">Case is ${esc(c.status)}… live traces respect API rate limits and can take a few minutes.</p></div>`;
    pollTimer = setTimeout(() => showCase(id), 2000);
    return;
  }
  if (c.status === "failed") {
    main.innerHTML = `<div class="card"><h2>${esc(c.case_reference)}</h2><p class="error">Failed: ${esc(c.error)}</p></div>`;
    return;
  }
  renderCase(c);
}

function renderCase(c) {
  const f = c.result.findings;
  const synthetic = f.data_mode === "synthetic";
  const approved = new Set((c.approvals || []).map((a) => a.decision_id));
  let html = synthetic ? `<div class="banner">SYNTHETIC DEMO DATA — fictional entities and transactions, not evidence</div>` : "";
  html += `<div class="card"><h2>${esc(f.request.case_reference)}</h2><div class="kv">
      <span class="muted">Case id</span><span class="mono">${esc(c.case_id)}</span>
      <span class="muted">Findings hash</span><span class="mono">${esc(c.findings_hash)}</span>
      <span class="muted">Label snapshot</span><span class="mono">${esc(f.label_snapshot)}</span>
      <span class="muted">Evidence objects</span><span>${f.evidence_ids.length}</span>
    </div><p><a href="/v1/cases/${esc(c.case_id)}/report" target="_blank" rel="noopener">Open full report ↗</a></p></div>`;

  html += `<h3>Routing drafts</h3>`;
  if (!f.routing.length) html += `<p class="muted">No VASP or issuer reached on any traced path.</p>`;
  else {
    html += `<div class="table-wrap"><table><tr><th>Target</th><th>Request</th><th>Grade</th><th>Status</th><th>Reason</th><th></th></tr>`;
    for (const d of f.routing) {
      const canApprove = d.status === "ready_for_approval" && !synthetic && !approved.has(d.id);
      html += `<tr><td>${esc(d.target_name)}<div class="small muted">${esc(d.chain)} · ${d.direction === "out" ? "funds out" : "funding in"}</div></td>
        <td>${esc(d.request_type.replace("_", " "))}</td>
        <td>${d.grade ? `<span class="grade grade-${esc(d.grade)}">${esc(d.grade)}</span> <span class="small">${esc(d.attribution_rules.join(", "))}</span>` : "—"}</td>
        <td class="st-${esc(d.status)}">${esc(d.status.replaceAll("_", " "))}${approved.has(d.id) ? "<div class='small'>approved</div>" : ""}</td>
        <td class="small">${esc(d.reasons.join("; "))}</td>
        <td>${canApprove ? `<button data-approve="${esc(d.id)}">Approve</button>` : ""}</td></tr>`;
    }
    html += `</table></div>`;
  }

  html += `<h3>Fund flow</h3><div class="legend">
    <span style="--dot:#1f5fa8">subject</span><span style="--dot:#1f7a3f">VASP</span><span style="--dot:#7a3fb0">service / mixer</span>
    <span style="--dot:#d9822b">dormant</span><span style="--dot:#8a8f98">hop</span><span style="--dot:#b3261e">conflict / high activity</span></div>
    <div id="graph"></div>`;

  for (const t of f.traces) {
    html += `<h3>${esc(t.chain)} · <span class="mono">${esc(short(t.subject))}</span> · ${t.params.direction === "out" ? "funds out" : "funding in"}</h3>`;
    if (!t.endpoints.length) {
      html += `<p class="muted">No endpoints (${esc(t.coverage.subject_note || "nothing to follow")}).</p>`;
      continue;
    }
    html += `<div class="table-wrap"><table><tr><th>Endpoint</th><th>Address</th><th>Attribution</th><th>Hops</th><th>Bottleneck</th></tr>`;
    for (const e of t.endpoints) {
      const a = e.attribution;
      const bn = e.bottleneck ? `${formatAmount(e.bottleneck)}` : "—";
      html += `<tr><td>${esc(e.kind.replaceAll("_", " "))}${e.notes.map((n) => `<div class="small muted">${esc(n)}</div>`).join("")}</td>
        <td class="mono">${esc(e.address)}${e.adjacent_role ? `<div class="small muted">via ${esc(e.adjacent_address)} — ${esc(e.adjacent_role)}</div>` : ""}</td>
        <td>${a ? `<span class="grade grade-${esc(a.grade)}">${esc(a.grade)}</span> ${esc(a.entity_name || "conflict")} <span class="small muted">${esc(a.rule)}</span>` : "<span class='muted'>—</span>"}</td>
        <td>${e.hops}</td><td>${esc(bn)}</td></tr>`;
    }
    const cv = t.coverage;
    html += `</table></div><p class="small muted">Coverage: ${cv.addresses_expanded} addresses expanded · not followed: ${cv.excluded_time_order} time-order, ${cv.excluded_dust} dust, ${cv.excluded_unverified_asset} unverified token, ${cv.excluded_other_asset} other asset${cv.budget_exhausted ? " · <strong>budget exhausted</strong>" : ""}${cv.incomplete_histories.length ? ` · ${cv.incomplete_histories.length} incomplete histories` : ""}${cv.source_errors.length ? ` · ${cv.source_errors.length} source errors` : ""}</p>`;
  }
  $("#main").innerHTML = html;
  document.querySelectorAll("[data-approve]").forEach((b) => (b.onclick = () => approve(c.case_id, b.dataset.approve)));
  drawGraph(c.case_id);
}

function formatAmount(b) {
  const a = b.asset;
  const s = BigInt(b.amount).toString().padStart(a.decimals + 1, "0");
  const whole = s.slice(0, s.length - a.decimals) || "0";
  const frac = a.decimals ? s.slice(s.length - a.decimals).replace(/0+$/, "") : "";
  return `${whole}${frac ? "." + frac : ""} ${a.symbol}`;
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
  } catch (e) {
    alert(`Not approved: ${e.message}`);
  }
}

const KIND_COLOR = {
  subject: "#1f5fa8", vasp: "#1f7a3f", service: "#7a3fb0", dormant: "#d9822b", origin: "#5b6470",
  coinjoin_like: "#7a3fb0", unlabeled_contract: "#8a5a2b", high_activity: "#b3261e", hop: "#8a8f98",
  hop_limit: "#b8bec6", not_expanded: "#b8bec6", source_error: "#b3261e",
};

async function drawGraph(caseId) {
  if (typeof cytoscape === "undefined") {
    $("#graph").textContent = "Graph library unavailable (offline). Tables and report are complete.";
    return;
  }
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
    ],
    layout: { name: "breadthfirst", directed: true, roots: g.nodes.filter((n) => n.data.kind === "subject").map((n) => "#" + CSS.escape(n.data.id)), spacingFactor: 1.1, padding: 20 },
    wheelSensitivity: 0.2,
  });
  cy.on("tap", "edge", (ev) => window.open(ev.target.data("url"), "_blank", "noopener"));
}

loadMeta();
loadCases();
