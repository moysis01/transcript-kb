import { $, $$, api, app, esc, fmt, fmtDate, onLeave, thumb } from "./core.js";
import { STAGES, jobListeners, jobProgress, jobs, stageLabel } from "./live.js";
import { deleteVideo } from "./video.js";

/* Library page: the processing queue, and the video grid with delete buttons. */
/* ---------- library ---------- */
export async function viewLibrary() {
  app.innerHTML = `
    <div id="queue"></div>
    <div class="sectionhead"><h2>Library</h2><span class="muted small" id="libCount"></span></div>
    <div id="grid" class="grid"></div>`;
  const renderGrid = async () => {
    const vids = await api("/api/library");
    $("#libCount").textContent = vids.length ? `${vids.length} video${vids.length === 1 ? "" : "s"}` : "";
    $("#grid").innerHTML = vids.length ? vids.map(videoCard).join("") :
      `<div class="empty card" style="grid-column:1/-1"><b>No videos yet</b><a href="#/discover">Add some in Discover →</a></div>`;
    $$("[data-del]").forEach((b) => b.addEventListener("click", () => deleteVideo(b.dataset.del, b, renderGrid)));
  };
  const listener = (finished) => { renderQueue(); if (finished) renderGrid(); };
  jobListeners.add(listener);
  onLeave(() => jobListeners.delete(listener));
  renderQueue();
  await renderGrid();
}

export function videoCard(v) {
  return `<div class="vcard"><a class="vlink" href="#/v/${v.id}">
    <div class="thumb"><img loading="lazy" src="${thumb(v.id)}" alt="">${v.duration ? `<span class="dur">${fmt(v.duration)}</span>` : ""}</div>
    <div class="body">
      <div class="t">${esc(v.title)}</div>
      <div class="muted small">${esc(v.channel || "")}${v.upload_date ? " · " + fmtDate(v.upload_date) : ""}</div>
      ${v.ready ? `<div class="tldr">${esc(v.tldr)}</div>` : `<div class="tldr">${v.orphan ? "Files missing." : "Not finished."} Add it again.</div>`}
      ${v.ready ? `<div class="foot"><span>${v.n_concepts} concepts</span></div>` : ""}
    </div></a>
    <button class="vdel" data-del="${v.id}" title="Delete video" aria-label="Delete ${esc(v.title)}">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg></button></div>`;
}

export function renderQueue() {
  const el = $("#queue");
  if (!el) return;
  if (!jobs.length) { el.innerHTML = ""; return; }
  const finished = jobs.some((j) => !["queued", "running"].includes(j.state));
  el.innerHTML = `<div class="sectionhead"><h2>Queue</h2><span class="grow"></span>${finished ? `<button class="btn ghost small" id="clearJobs">Clear finished</button>` : ""}</div>
    <div class="jobs">${jobs.slice().reverse().map(jobCard).join("")}</div>`;
  $("#clearJobs")?.addEventListener("click", async () => { await api("/api/jobs/clear", {}); pollNow(); });
  $$("[data-cancel]", el).forEach((b) => b.addEventListener("click", async () => { await api(`/api/jobs/${b.dataset.cancel}/cancel`, {}); pollNow(); }));
}

export function jobCard(j) {
  const idx = STAGES.findIndex(([k]) => k === j.stage);
  const steps = STAGES.map(([k, label], i) => {
    const cls = j.state === "done" || (idx >= 0 && i < idx) ? "done" : i === idx && j.state === "running" ? "active" : "";
    return `<li class="${cls}">${label}</li>`;
  }).join("");
  const pct = jobProgress(j);
  let status;
  const took = j.finished && j.started ? ` in ${fmtSecs(j.finished - j.started)}` : "";
  if (j.state === "done") status = `Done${took} · <a href="#/v/${j.vid}">Open →</a>`;
  else if (j.state === "error") status = esc(j.error || "Failed");
  else if (j.state === "cancelled") status = "Cancelled";
  else if (j.state === "queued") status = "Waiting";
  else status = esc(stageLabel(j));
  const bar = j.state === "running" ? `<div class="bar ${pct == null ? "indet" : ""}"><span style="width:${pct ?? 0}%"></span></div>` : "";
  const cancel = ["queued", "running"].includes(j.state) ? `<button class="btn ghost small" data-cancel="${j.id}">Cancel</button>` : "";
  return `<div class="card job ${j.state}"><img src="${thumb(j.vid)}" alt="">
    <div style="min-width:0"><div class="title">${esc(j.title)}</div><ol class="steps">${steps}</ol>${bar}<div class="status">${status}</div></div>${cancel}</div>`;
}
export const fmtSecs = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`);

export let pollTimer = null;
export function pollNow() { clearTimeout(pollTimer); pollTimer = setTimeout(async () => { try { jobs = await api("/api/jobs"); renderQueue(); } catch {} }, 150); }

