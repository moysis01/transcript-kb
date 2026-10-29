
/* Shared helpers: DOM, formatting, API calls, storage, removal categories, background tasks. */

export const app = document.getElementById("app");
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const thumb = (id) => `https://i.ytimg.com/vi/${id}/mqdefault.jpg`;
export const fmt = (sec) => {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};
export const parseTs = (ts) => ts.split(":").reduce((a, b) => a * 60 + Number(b), 0);
export const fmtDate = (d) => (d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : d || "");
export const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

export async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

export const leaveFns = [];
/** Run `fn` when the current page is left (stop timers, destroy players and graphs). */
export const onLeave = (fn) => leaveFns.push(fn);
export const leave = () => leaveFns.splice(0).forEach((fn) => fn());

/* ---------- removal categories ---------- */
export const CAT = {
  sponsor: "Sponsor / ad read", self_promotion: "Self-promotion", selfpromo: "Self-promotion",
  engagement: "Like / subscribe / comment", interaction: "Like / subscribe / comment",
  intro_outro: "Intro / sign-off", intro: "Intro / sign-off", outro: "Intro / sign-off",
  filler: "Filler / banter", off_topic: "Off-topic", preview: "Preview / recap", music_offtopic: "Off-topic",
};
// SponsorBlock's categories share the colours of the AI's: e.g. its "selfpromo" is shown as self-promotion.
export const catKey = (c) => ({ selfpromo: "self_promotion", interaction: "engagement", intro: "intro_outro",
  outro: "intro_outro", preview: "off_topic", music_offtopic: "off_topic" }[c] || c);
export const catChip = (c) => `<em class="chip cat-${esc(catKey(c))}"><span class="dot"></span>${esc(CAT[c] || c)}</em>`;
export const BY = { sponsorblock: "SponsorBlock", llm: "the AI", fallback: "kept raw" };

/* ---------- background tasks (survive refreshes, closed tabs and restarts) ---------- */
export const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; };
export const isActive = (t) => t.state === "queued" || t.state === "running";
export function watchTask(id, onUpdate) {
  let stop = false, timer = null;
  const tick = async () => {
    if (stop) return;
    try {
      const t = await api(`/api/tasks/${id}`);
      onUpdate(t);
      if (isActive(t)) timer = setTimeout(tick, 800);
    } catch {
      timer = setTimeout(tick, 2000); // server restarting: keep trying, the task resumes there
    }
  };
  onLeave(() => { stop = true; clearTimeout(timer); });
  tick();
}
export function taskStatus(t, fallback) {
  if (t.state === "error") return `<div class="notice err">This didn't finish: ${esc(t.error)}</div>`;
  if (!isActive(t)) return "";
  const step = t.progress.length ? t.progress[t.progress.length - 1] : fallback;
  const secs = t.started ? Math.round(Date.now() / 1000 - t.started) : 0;
  return `<div class="taskline"><span class="pill"><span class="spin"></span>${esc(step)}…</span>
    <span class="muted small">${secs ? secs + "s" : ""}</span></div>`;
}

