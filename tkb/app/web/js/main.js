import { $, $$, app, esc, leave } from "./core.js";
import { pollJobs } from "./live.js";
import { Player } from "./player.js";
import { viewLibrary } from "./library.js";
import { viewVideo } from "./video.js";
import { viewConcept, viewConcepts, viewGraph, viewSearch } from "./knowledge.js";
import { viewAsk } from "./ask.js";
import { viewDiscover } from "./discover.js";
import { viewSettings } from "./settings.js";

/* Entry point: routing and every startup side effect live here, nowhere else. */
function route() {
  const [path, qs] = location.hash.slice(1).split("?");
  const q = new URLSearchParams(qs || "");
  const parts = (path || "/").split("/").filter(Boolean);
  const view = parts[0] || "library";
  // Same video, new timestamp: just seek.
  if (view === "v" && Player.vid === parts[1] && $("#player")) {
    if (q.get("t")) Player.seek(Number(q.get("t")));
    return;
  }
  leave();
  Player.reset();
  $$(".top nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === ({ v: "library", c: "concepts", search: "" }[view] ?? view)));
  window.scrollTo(0, 0);
  const views = { library: viewLibrary, v: () => viewVideo(parts[1], Number(q.get("t")) || 0),
    concepts: viewConcepts, c: () => viewConcept(decodeURIComponent(parts[1] || "")), graph: viewGraph,
    ask: () => viewAsk(q.get("task") || ""), search: () => viewSearch(q.get("q") || ""),
    discover: () => viewDiscover({ task: q.get("task"), topic: q.get("topic") }),
    settings: () => viewSettings(parts[1]) };
  (views[view] || viewLibrary)().catch((e) => { app.innerHTML = `<div class="notice">${esc(e.message)}</div>`; });
}
window.addEventListener("hashchange", route);

$("#searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#q").value.trim();
  if (v) location.hash = `#/search?q=${encodeURIComponent(v)}`;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
});

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-t]");
  if (!el || !Player.vid) return;
  e.preventDefault();
  Player.seek(Number(el.dataset.t));
});


pollJobs();
route();
