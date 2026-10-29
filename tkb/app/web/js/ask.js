import { $, ago, api, app, esc, fmt, isActive, parseTs, store, taskStatus, watchTask } from "./core.js";

/* Ask: questions answered from the knowledge base, run as durable server tasks. */
/* ---------- ask ---------- */
export function inlineMd(s, titles) {
  return s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([A-Za-z0-9_-]{11})@(\d+(?::\d+)+)\]/g, (_, vid, ts) => {
      const title = titles[vid] || vid;
      return `<a class="cite" href="#/v/${vid}?t=${parseTs(ts)}" title="${esc(title)}">▶ ${esc(title.length > 28 ? title.slice(0, 26) + "…" : title)} ${ts}</a>`;
    });
}
export function md(text, titles) {
  let html = "", list = false;
  for (const line of esc(text).split("\n")) {
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (li) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inlineMd(li[1], titles)}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    const hd = line.match(/^#{1,4}\s+(.*)/);
    if (hd) html += `<h4>${inlineMd(hd[1], titles)}</h4>`;
    else if (line.trim()) html += `<p>${inlineMd(line, titles)}</p>`;
  }
  return html + (list ? "</ul>" : "");
}
export function answerHtml(t) {
  const q = `<div class="q"><a href="#/ask?task=${t.id}">${esc(t.params.question)}</a> <span class="muted small">· ${ago(t.created)}</span></div>`;
  if (isActive(t) || t.state === "error") return `<div class="card answer">${q}${taskStatus(t, "Reading your knowledge base")}</div>`;
  const a = t.result, titles = {};
  a.sources.forEach((s) => { if (s.type !== "concept") titles[s.vid] = s.title; });
  a.sources.forEach((s) => { titles[s.vid] ??= s.title; });
  return `<div class="card answer">${q}${md(a.answer, titles)}
    ${a.dropped_citations?.length ? `<div class="muted small">Removed ${a.dropped_citations.length} citation(s) that didn't match a retrieved source.</div>` : ""}
    ${a.sources.length ? `<details class="sources"><summary>${a.sources.length} sources used</summary><ul>${a.sources.map((s) => `<li>
      <a class="cite" href="#/v/${s.vid}?t=${s.t}">▶ ${fmt(s.t)}</a> <b>${esc(s.title)}</b>: <span class="muted">${esc(s.text.slice(0, 180))}…</span></li>`).join("")}</ul></details>` : ""}</div>`;
}
export async function viewAsk(taskId) {
  store.set("tkb.asks", null); // answers used to live in this browser; they're on the server now
  app.innerHTML = `<div style="max-width:860px"><h1>Ask your knowledge base</h1>
    <div class="muted" style="margin-bottom:12px">Answers come only from your processed videos, with clickable timestamps for every claim.</div>
    <div class="card ask"><textarea id="question" placeholder="e.g. Why does a shaken bottle foam when opened?"></textarea>
      <div class="row"><span class="muted small">Usually 10 to 30 seconds. ⌘↵ to ask.</span><span class="grow"></span><button class="btn primary" id="askBtn">Ask</button></div>
      <div class="msg" id="askMsg"></div></div>
    <div id="answer"></div>
    <div class="history" id="history"></div></div>`;
  const go = async () => {
    const q = $("#question").value.trim();
    if (!q) return;
    $("#askBtn").disabled = true;
    try {
      const t = await api("/api/tasks", { kind: "ask", question: q });
      location.hash = `#/ask?task=${t.id}`;
    } catch (e) {
      $("#askMsg").className = "msg err";
      $("#askMsg").textContent = e.message;
      $("#askBtn").disabled = false;
    }
  };
  $("#askBtn").addEventListener("click", go);
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) go(); });
  const renderHistory = async () => {
    const past = (await api("/api/tasks?kind=ask&limit=20")).filter((t) => t.id !== taskId);
    $("#history").innerHTML = past.length ? `<div class="sectionhead"><h2>Earlier questions</h2></div>${past.map(answerHtml).join("")}` : "";
  };
  if (taskId) watchTask(taskId, (t) => { $("#answer").innerHTML = answerHtml(t); if (!isActive(t)) renderHistory(); });
  else $("#question").focus();
  renderHistory();
}

