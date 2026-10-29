import { $, api, esc, isActive } from "./core.js";

/* Live status of the processing queue and background tasks (the header indicator). */
/* ---------- job polling (global) ---------- */
export let jobs = [], liveTasks = [];
export const jobListeners = new Set();
export async function pollJobs() {
  try {
    const prev = new Map(jobs.map((j) => [j.id, j.state]));
    [jobs, liveTasks] = await Promise.all([api("/api/jobs"), api("/api/tasks?limit=10").then((ts) => ts.filter(isActive))]);
    const active = jobs.filter((j) => j.state === "running" || j.state === "queued");
    const running = jobs.filter((j) => j.state === "running");
    const pill = $("#queuePill");
    pill.hidden = !active.length && !liveTasks.length;
    const parts = [];
    if (running.length) parts.push(running.length > 1 ? `${running.length} videos processing` : esc(stageLabel(running[0])));
    else if (active.length) parts.push("Queued");
    if (active.length > running.length && running.length) parts.push(`${active.length - running.length} waiting`);
    if (liveTasks.length) parts.push(liveTasks.length === 1 ? (liveTasks[0].kind === "ask" ? "answering" : "searching") : `${liveTasks.length} tasks`);
    pill.innerHTML = parts.length ? `<span class="spin"></span>${parts.join(" · ")}` : "";
    pill.href = active.length || !liveTasks.length ? "#/" : `#/${liveTasks[0].kind}?task=${liveTasks[0].id}`;
    const finished = jobs.some((j) => prev.has(j.id) && prev.get(j.id) !== j.state && j.state === "done");
    jobListeners.forEach((fn) => fn(finished));
  } catch { /* server restarting */ }
  setTimeout(pollJobs, jobs.some((j) => j.state === "running" || j.state === "queued") || liveTasks.length ? 1200 : 3000);
}

export const STAGES = [["ingest", "Download"], ["speakers", "Speakers"], ["transcribe", "Transcribe"], ["clean", "Remove noise"], ["kb", "Knowledge base"]];
export const SPK_COLORS = ["#4f46e5", "#e5484d", "#12a594", "#f76b15", "#8e4ec6", "#0090ff", "#ad5700", "#d6409f"];
export const spkColor = (id) => SPK_COLORS[(Number(String(id).slice(1)) - 1) % SPK_COLORS.length];
export function stageLabel(j) {
  const s = STAGES.find(([k]) => k === j.stage);
  if (!s) return j.state === "queued" ? "Queued" : "Starting";
  if (j.stage === "transcribe" && j.pct != null) return `${s[1]} ${j.pct}%`;
  if (j.total) return `${s[1]} ${j.done}/${j.total}`;
  return `${s[1]}…`;
}
export function jobProgress(j) {
  const i = STAGES.findIndex(([k]) => k === j.stage);
  if (i < 0) return null;
  let frac = null;
  if (j.stage === "transcribe" && j.pct != null) frac = j.pct / 100;
  else if (j.total) frac = j.done / j.total;
  return frac == null ? null : ((i + frac) / STAGES.length) * 100;
}

