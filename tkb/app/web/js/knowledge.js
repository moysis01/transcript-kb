import { $, $$, api, app, esc, fmt, onLeave, thumb } from "./core.js";

/* Concepts, a concept's page, the concept graph, and search. */
/* ---------- concepts ---------- */
export async function viewConcepts() {
  const list = await api("/api/concepts");
  const kinds = [...new Set(list.map((c) => c.kind))].sort();
  const shared = list.filter((c) => c.videos > 1).length;
  app.innerHTML = `<h1>Concepts</h1>
    <div class="muted">${list.length} concepts · ${shared} appear in more than one video</div>
    <div class="row"><input type="search" id="cf" placeholder="Filter concepts…" style="max-width:340px">
      <label class="check"><input type="checkbox" id="sharedOnly"> Only concepts shared across videos</label></div>
    <div class="filters">${kinds.map((k) => `<button class="chip" data-kind="${esc(k)}">${esc(k)}</button>`).join("")}</div>
    <div class="clist" id="clist"></div>`;
  const offKinds = new Set();
  const render = () => {
    const f = $("#cf").value.toLowerCase(), so = $("#sharedOnly").checked;
    const rows = list.filter((c) => !offKinds.has(c.kind) && (!so || c.videos > 1) &&
      (c.name.toLowerCase().includes(f) || c.aliases.some((a) => a.toLowerCase().includes(f))));
    $("#clist").innerHTML = rows.map((c) => `<a href="#/c/${encodeURIComponent(c.slug)}"><span>${esc(c.name)}</span>
      <span class="count ${c.videos > 1 ? "multi" : ""}" title="videos">${c.videos}</span></a>`).join("") || `<div class="muted">No matches.</div>`;
  };
  $("#cf").addEventListener("input", render);
  $("#sharedOnly").addEventListener("change", render);
  $$("[data-kind]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.kind;
    offKinds.has(k) ? offKinds.delete(k) : offKinds.add(k);
    b.classList.toggle("off", offKinds.has(k));
    render();
  }));
  render();
}

export async function viewConcept(slug) {
  const c = await api(`/api/concept/${encodeURIComponent(slug)}`);
  app.innerHTML = `<div style="max-width:860px">
    <div class="muted small"><a href="#/concepts">Concepts</a> /</div>
    <h1>${esc(c.name)} <span class="chip">${esc(c.kind)}</span></h1>
    ${c.aliases.length ? `<div class="muted small">Also called: ${c.aliases.map(esc).join(", ")}</div>` : ""}
    <div class="muted" style="margin:6px 0 18px">Appears in ${c.sections.length} video${c.sections.length === 1 ? "" : "s"}</div>
    <div class="ccards">${c.sections.map((s) => `<div class="card ccard">
      <a class="secthead" href="#/v/${s.vid}"><img src="${thumb(s.vid)}" alt=""><b>${esc(s.title)}</b></a>
      <p>${esc(s.summary)}</p>${s.facts.length ? `<ul>${s.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      <div class="times">${s.times.map((t) => `<a class="ts" href="#/v/${s.vid}?t=${t}">▶ ${fmt(t)}</a>`).join("")}</div></div>`).join("")}</div></div>`;
}

/* ---------- graph ---------- */
export function loadVis() {
  if (window.vis) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js";
    s.onload = resolve;
    s.onerror = () => reject(new Error("Couldn't load the graph library (no internet?)."));
    document.head.appendChild(s);
  });
}
export async function viewGraph() {
  const KIND_COLORS = { concept: "#4f46e5", technique: "#0090ff", tool: "#12a594", person: "#f76b15", organization: "#e5484d",
    claim: "#8e4ec6", example: "#ad5700", other: "#8b8d98" };
  // Kinds added in Settings → Notes get their own colour, the same one every time.
  const PALETTE = ["#d6409f", "#0d9488", "#ca8a04", "#2563eb", "#9333ea", "#dc2626", "#65a30d", "#0891b2"];
  const kindColor = (k) => KIND_COLORS[k] || PALETTE[[...String(k)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % PALETTE.length];
  app.innerHTML = `<div class="row" style="margin:0 0 12px"><h1 style="margin:0">Graph</h1><span class="grow"></span>
      <label class="check"><input type="checkbox" id="gShared"> Only concepts shared across videos</label></div>
    <div class="legend" id="glegend" style="margin-bottom:10px"></div>
    <div class="gwrap"><nav class="gtoc" id="gtoc" aria-label="Videos in the graph"></nav><div id="graph"></div></div>`;
  const [data] = await Promise.all([api("/api/graph"), loadVis()]);
  const kinds = [...new Set(data.nodes.filter((n) => n.type !== "video").map((n) => n.kind))]
    .sort((a, b) => (a === "other") - (b === "other") || Object.keys(KIND_COLORS).indexOf(a) - Object.keys(KIND_COLORS).indexOf(b));
  $("#glegend").innerHTML = `<span><i class="box" style="background:var(--text)"></i>Video</span>
    ${kinds.map((k) => `<span><i style="background:${kindColor(k)}"></i>${esc(k)}</span>`).join("")}
    <span>· bigger dot = in more videos · click a node to open it</span>`;
  if (!data.nodes.length) { $("#graph").innerHTML = `<div class="empty"><b>Nothing to draw yet</b>Process a video first.</div>`; return; }
  const css = getComputedStyle(document.documentElement);
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const text = css.getPropertyValue("--text").trim(), accent = css.getPropertyValue("--accent").trim();
  let net = null, located = null, settled = false;

  // Table of contents: one entry per video; clicking one zooms the graph to it and highlights its concepts.
  const concepts = {};
  data.edges.forEach((e) => { concepts[e.from] = (concepts[e.from] || 0) + 1; });
  const videos = data.nodes.filter((n) => n.type === "video").sort((a, b) => a.label.localeCompare(b.label));
  $("#gtoc").innerHTML = `<div class="gtoc-head">Videos <span class="muted">${videos.length}</span></div>`
    + videos.map((v) => `<button class="gtoc-item" data-node="${esc(v.id)}" title="${esc(v.label)}">
        <span>${esc(v.label)}</span><span class="muted small">${concepts[v.id] || 0}</span></button>`).join("");
  const locate = (id) => {
    located = id;
    $$(".gtoc-item").forEach((b) => b.classList.toggle("on", b.dataset.node === id));
    net.selectNodes([id]);
    // Before the layout settles nothing is drawn, so an animation would never play: the view jumps there once it has.
    if (settled) net.focus(id, { scale: 1.1, locked: true, animation: { duration: 700, easingFunction: "easeInOutQuad" } });
  };
  $$(".gtoc-item").forEach((b) => b.addEventListener("click", () => locate(b.dataset.node)));
  onLeave(() => net?.destroy());
  const draw = () => {
    net?.destroy();
    settled = false;
    const sharedOnly = $("#gShared").checked;
    const keep = new Set(data.nodes.filter((n) => n.type === "video" || !sharedOnly || n.videos > 1).map((n) => n.id));
    const nodes = data.nodes.filter((n) => keep.has(n.id)).map((n) => n.type === "video"
      ? { id: n.id, label: n.label.length > 42 ? n.label.slice(0, 40) + "…" : n.label, title: n.label, shape: "box",
          color: { background: text, border: text, highlight: { background: accent, border: accent } },
          font: { color: dark ? "#111" : "#fff", size: 13 }, margin: 8 }
      : { id: n.id, label: n.label, shape: "dot", size: 7 + 6 * (n.videos - 1), color: kindColor(n.kind),
          font: { color: text, size: 12 + 2 * (n.videos - 1) } });
    const edges = data.edges.filter((e) => keep.has(e.from) && keep.has(e.to))
      .map((e) => ({ ...e, color: { color: dark ? "#3a3a44" : "#d5d5dd", highlight: accent } }));
    net = new vis.Network($("#graph"), { nodes, edges }, {
      physics: { barnesHut: { gravitationalConstant: -5000, springLength: 120 }, stabilization: { iterations: 250, fit: false } },
      interaction: { hover: true, tooltipDelay: 150 },
    });
    // Once settled: fit the whole graph, or, if a video was picked meanwhile (or before a redraw), go straight to it
    // and keep it in view while the layout drifts.
    net.once("stabilizationIterationsDone", () => {
      settled = true;
      if (located) {
        net.selectNodes([located]);
        net.focus(located, { scale: 1.1, locked: true, animation: false });
      } else net.fit();
    });
    net.on("click", (p) => {
      const id = p.nodes[0];
      if (!id) return;
      location.hash = id.startsWith("v:") ? `#/v/${id.slice(2)}` : `#/c/${encodeURIComponent(id.slice(2))}`;
    });
  };
  $("#gShared").addEventListener("change", draw);
  draw();
}

/* ---------- search ---------- */
export async function viewSearch(q) {
  $("#q").value = q;
  const hits = await api(`/api/search?q=${encodeURIComponent(q)}`);
  const terms = q.toLowerCase().match(/\w{2,}/g) || [];
  const hl = (s) => { let h = esc(s); for (const t of terms) h = h.replace(new RegExp(`\\b(${t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\w*)`, "gi"), "<mark>$1</mark>"); return h; };
  const snip = (s) => {
    const i = Math.max(0, terms.map((t) => s.toLowerCase().indexOf(t)).filter((i) => i >= 0).sort((a, b) => a - b)[0] ?? 0);
    return (i > 80 ? "…" : "") + s.slice(Math.max(0, i - 80), i + 220) + "…";
  };
  const group = (type) => hits.filter((h) => h.type === type);
  const concepts = group("concept"), videos = group("video"), moments = group("transcript");
  app.innerHTML = `<div style="max-width:920px"><h1>Search: “${esc(q)}”</h1>
    <div class="muted" style="margin-bottom:12px">${hits.length ? `${hits.length} results` : "No results."} · <a href="#/ask">Ask this as a question →</a></div>
    ${concepts.length ? `<h2>Concepts</h2>${concepts.map((h) => `<a class="hit" href="#/c/${encodeURIComponent(h.slug)}"><img src="${thumb(h.vid)}" alt="">
      <div><b>${hl(h.title)}</b> <span class="muted small">in ${esc(h.video_title)}</span><div class="snip">${hl(snip(h.text))}</div></div></a>`).join("")}` : ""}
    ${videos.length ? `<h2 style="margin-top:22px">Videos</h2>${videos.map((h) => `<a class="hit" href="#/v/${h.vid}"><img src="${thumb(h.vid)}" alt="">
      <div><b>${hl(h.title)}</b><div class="snip">${hl(snip(h.text))}</div></div></a>`).join("")}` : ""}
    ${moments.length ? `<h2 style="margin-top:22px">Moments in transcripts</h2>${moments.map((h) => `<a class="hit" href="#/v/${h.vid}?t=${h.t}"><img src="${thumb(h.vid)}" alt="">
      <div><b>${esc(h.title)}</b> <span class="ts">${fmt(h.t)}</span><div class="snip">${hl(snip(h.text))}</div></div></a>`).join("")}` : ""}</div>`;
  const askLink = $$("a[href='#/ask']", app)[0];
  askLink?.addEventListener("click", () => setTimeout(() => { const t = $("#question"); if (t) t.value = q; }, 50));
}

