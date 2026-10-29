import { $, $$, ago, api, app, esc, fmt, fmtDate, isActive, store, taskStatus, thumb, watchTask } from "./core.js";
import { pollNow } from "./library.js";

/* Discover: add videos by link, or find them with several YouTube searches at once (durable server tasks). */
/* ---------- discover ---------- */
export const DISC_OPTS = {
  duration: [["any", "Any length"], ["short", "Under 4 min"], ["medium", "4–20 min"], ["long", "Over 20 min"]],
  date: [["any", "Any time"], ["today", "Today"], ["week", "This week"], ["month", "This month"], ["year", "This year"]],
  sort: [["relevance", "Relevance"], ["popularity", "Popularity"]],
  picks: [["3", "Pick 3"], ["5", "Pick 5"], ["10", "Pick 10"], ["20", "Pick 20"]],
};
export const fmtViews = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : String(n || 0)) + " views";
export const blankSearch = () => ({ topic: "", duration: "any", date: "any", sort: "relevance" });
export const selectHtml = (k, v, cls = "") => `<select class="${cls}" data-k="${k}">${DISC_OPTS[k].map(([o, l]) => `<option value="${o}" ${String(v) === o ? "selected" : ""}>${l}</option>`).join("")}</select>`;

/* Add videos by link: single videos, playlists or channels. */
function addVideosHtml(d) {
  return `<section class="card add">
      <h1>Add videos</h1>
      <textarea id="urls" rows="3" placeholder="Paste YouTube links: videos, playlists or channels"></textarea>
      <div class="row">
        <label class="check"><input type="checkbox" id="sb" ${d.sponsorblock ? "checked" : ""}> Cut sponsor segments</label>
        <label class="check"><input type="checkbox" id="tr" ${d.translate ? "checked" : ""}> Translate to English</label>
        <label class="check"><input type="checkbox" id="force"> Redo existing videos</label>
        <span class="grow"></span>
        <button class="btn primary" id="addBtn">Add to queue</button>
      </div>
      <div class="msg" id="addMsg"></div>
    </section>`;
}

function bindAddVideos() {
  const submit = async (confirm = false) => {
    const text = $("#urls").value.trim(), msg = $("#addMsg"), btn = $("#addBtn");
    if (!text) return;
    btn.disabled = true;
    msg.className = "msg";
    msg.textContent = "Reading links…";
    try {
      const r = await api("/api/jobs", { text, sponsorblock: $("#sb").checked, translate: $("#tr").checked,
        force: $("#force").checked, confirm });
      if (r.needs_confirm) {
        btn.disabled = false;
        if (window.confirm(`Add all ${r.total} videos?`)) return submit(true);
        msg.textContent = "Nothing added.";
        return;
      }
      msg.className = "msg ok";
      msg.innerHTML = `Queued ${r.added}` + (r.skipped ? ` · ${r.skipped} already added` : "")
        + (r.added ? ` · <a href="#/">Library →</a>` : "");
      if (r.added) $("#urls").value = "";
      pollNow();
    } catch (e) {
      msg.className = "msg err";
      msg.textContent = e.message;
    }
    btn.disabled = false;
  };
  $("#addBtn").addEventListener("click", () => submit());
  $("#urls").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); });
}

export async function viewDiscover({ task: taskId, topic } = {}) {
  const saved = store.get("tkb.discover2", { searches: [blankSearch()], picks: "5", only_new: true, auto: false });
  let state = { ...saved, searches: saved.searches?.length ? saved.searches : [blankSearch()] };
  if (topic) state.searches = [{ ...blankSearch(), topic }];
  const defaults = await api("/api/defaults");  // set in Settings; the checkboxes start from them
  const most = defaults.max_picks;  // the Pick menu follows Settings → Discover → Most picks
  DISC_OPTS.picks = [...new Set([3, 5, 10, 20, 30, 50].filter((n) => n < most).concat(most))].map((n) => [String(n), `Pick ${n}`]);
  if (Number(state.picks) > most) state.picks = String(most);
  app.innerHTML = `<div style="max-width:1000px">
    ${addVideosHtml(defaults)}
    <h1>Discover videos</h1>
    <div class="muted" style="margin-bottom:12px">Search by keywords or by channel (@handle).</div>
    <div class="card">
      <div id="rows" class="srows"></div>
      <button class="btn ghost small" id="addRow">+ Add search</button>
      <div class="row filters-row">${selectHtml("picks", state.picks, "g-picks")}
        <label class="check"><input type="checkbox" id="d-only_new" ${state.only_new ? "checked" : ""}> Only new videos</label>
        <label class="check"><input type="checkbox" id="d-auto" ${state.auto ? "checked" : ""}> Queue automatically</label>
        <span class="grow"></span><button class="btn primary" id="findBtn">Find videos</button></div>
      <div class="msg" id="discMsg"></div>
    </div>
    <div id="discStatus"></div>
    <div id="discResult"></div>
    <div id="recentBox"></div></div>`;

  const readState = () => ({
    searches: $$(".srow").map((r) => ({ topic: r.querySelector(".stopic").value.trim(),
      ...Object.fromEntries($$("select", r).map((s) => [s.dataset.k, s.value])) })),
    picks: $(".g-picks").value, only_new: $("#d-only_new").checked, auto: $("#d-auto").checked,
  });
  const persist = () => store.set("tkb.discover2", readState());
  const renderRows = () => {
    $("#rows").innerHTML = state.searches.map((s, i) => `<div class="srow">
      <span class="snum">${i + 1}</span>
      <input type="text" class="stopic" placeholder="Keywords or @channel" value="${esc(s.topic)}">
      ${selectHtml("duration", s.duration)}${selectHtml("date", s.date)}${selectHtml("sort", s.sort)}
      <button class="btn ghost small srm" data-i="${i}" title="Remove" ${state.searches.length === 1 ? "disabled" : ""}>×</button></div>`).join("");
    $$(".srm").forEach((b) => b.addEventListener("click", () => { state = readState(); state.searches.splice(Number(b.dataset.i), 1); renderRows(); persist(); }));
    $$(".srow input, .srow select").forEach((el) => el.addEventListener("change", persist));
    $$(".stopic").forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") find(); }));
    $$(".stopic").forEach((el) => { markChannel(el); el.addEventListener("input", () => markChannel(el)); });
  };
  bindAddVideos();
  renderRows();
  $("#addRow").addEventListener("click", () => {
    state = readState();
    if (state.searches.length >= 8) return;
    state.searches.push({ ...blankSearch(), ...(({ topic, ...f }) => f)(state.searches[state.searches.length - 1]) });
    renderRows(); persist(); $$(".stopic").at(-1).focus();
  });
  $$(".g-picks, #d-only_new, #d-auto").forEach((el) => el.addEventListener("change", persist));

  const find = async () => {
    const st = readState();
    const searches = st.searches.filter((s) => s.topic);
    if (!searches.length) return;
    persist();
    $("#findBtn").disabled = true;
    try {
      const t = await api("/api/tasks", { kind: "discover", searches, picks: Number(st.picks), only_new: st.only_new,
        auto_queue: st.auto, sponsorblock: $("#sb").checked, translate: $("#tr").checked });
      location.hash = `#/discover?task=${t.id}`;
    } catch (e) {
      $("#discMsg").className = "msg err";
      $("#discMsg").textContent = e.message;
      $("#findBtn").disabled = false;
    }
  };
  $("#findBtn").addEventListener("click", find);

  const renderRecent = async () => {
    const ts = (await api("/api/tasks?kind=discover&limit=8")).filter((t) => t.id !== taskId);
    $("#recentBox").innerHTML = ts.length ? `<div class="sectionhead"><h2>Recent searches</h2></div><div class="recent">${ts.map((t) => `
      <a class="card rtask" href="#/discover?task=${t.id}"><span>${t.params.searches.map((s) => `“${esc(s.topic)}”`).join(" + ")}</span>
        <span class="muted small">${isActive(t) ? `<span class="spin"></span> running` : t.state === "error" ? "failed" : `${t.result.picks.length} picked`} · ${ago(t.created)}</span></a>`).join("")}</div>` : "";
  };

  if (taskId) {
    watchTask(taskId, (t) => {
      if (state.searches.every((s) => !s.topic) || !$(".stopic").value) { state = { ...readState(), searches: t.params.searches }; renderRows(); }
      $("#discStatus").innerHTML = taskStatus(t, "Starting");
      $("#findBtn").disabled = isActive(t);
      if (t.state === "done") renderResult(t);
      if (!isActive(t)) renderRecent();
    });
  } else {
    $$(".stopic")[0].focus({ preventScroll: true });
  }
  renderRecent();
}

/* Same rule as tkb/services/channels.py: a handle or a channel URL (optionally ending in one of its tabs). */
const CHANNEL = /^(@[\w.-]{3,30}|https?:\/\/(www\.|m\.)?youtube\.com\/(@[\w.-]+|channel\/UC[\w-]{22}|c\/[\w.-]+|user\/[\w.-]+))\/?(videos|shorts|streams|playlists|posts|featured)?\/?$/;

function markChannel(input) {
  const row = input.closest(".srow"), on = CHANNEL.test(input.value.trim());
  row.classList.toggle("channel", on);
  row.querySelector(".snum").title = on ? "Channel" : "";
}

export function discCard(c, checked) {
  return `<label class="card dcard">
    <input type="checkbox" class="dpick" value="${c.id}" data-title="${esc(c.title)}" data-min="${Math.round((c.duration || 0) / 60)}" ${checked ? "checked" : ""}>
    <a href="https://youtu.be/${c.id}" target="_blank" rel="noopener" class="dthumb"><img src="${thumb(c.id)}" alt=""><span class="dur">${fmt(c.duration)}</span></a>
    <div style="min-width:0"><div class="t">${esc(c.title)}</div>
      <div class="muted small">${esc(c.channel)} · ${fmtViews(c.views)}${c.upload_date ? " · " + fmtDate(c.upload_date) : ""}${c.language && !c.language.startsWith("en") ? ` · <b>${esc(c.language)}</b>` : ""}${c.tab && c.tab !== "videos" ? ` · ${esc(c.found_by[0])}` : ""}</div>
      ${c.reason ? `<div class="reason"><span class="score">${c.score}</span>${esc(c.reason)}</div>` : ""}</div></label>`;
}

export function renderResult(t) {
  const r = t.result, box = $("#discResult");
  if (!r.picks.length) {
    box.innerHTML = `<div class="notice" style="margin-top:16px">No new videos found. Try other keywords or filters.</div>`;
    return;
  }
  const queued = r.queued != null ? `<div class="notice ok">Queued ${r.queued}. <a href="#/">Library →</a></div>` : "";
  box.innerHTML = `<div class="sectionhead"><h2>Picked for you</h2></div>
    ${queued}
    <div class="dlist">${r.picks.map((c) => discCard(c, true)).join("")}</div>
    ${r.others.length ? `<details class="others"><summary>${r.others.length} more</summary><div class="dlist">${r.others.map((c) => discCard(c, false)).join("")}</div></details>` : ""}
    <div class="card qbar"><span id="selInfo"></span><span class="grow"></span>
      <label class="check"><input type="checkbox" id="q-sb" ${$("#sb").checked ? "checked" : ""}> Cut sponsor segments</label>
      <label class="check"><input type="checkbox" id="q-tr" ${$("#tr").checked ? "checked" : ""}> Translate to English</label>
      <button class="btn primary" id="queueBtn">Process selected</button></div>
    <div class="msg" id="queueMsg"></div>`;
  const update = () => {
    const sel = $$(".dpick:checked");
    const mins = sel.reduce((a, el) => a + Number(el.dataset.min), 0);
    $("#selInfo").textContent = `${sel.length} selected · ${mins} min`;
    $("#queueBtn").disabled = !sel.length;
  };
  $$(".dpick").forEach((el) => el.addEventListener("change", update));
  $("#queueBtn").addEventListener("click", async () => {
    const ids = $$(".dpick:checked").map((el) => el.value);
    if (!ids.length) return;
    try {
      const titles = Object.fromEntries($$(".dpick:checked").map((el) => [el.value, el.dataset.title]));
      const res = await api("/api/jobs", { text: ids.join(" "), titles, sponsorblock: $("#q-sb").checked,
        translate: $("#q-tr").checked });
      $("#queueMsg").className = "msg ok";
      $("#queueMsg").innerHTML = `Queued ${res.added}${res.skipped ? ` · ${res.skipped} already added` : ""} · <a href="#/">Library →</a>`;
      $("#queueBtn").disabled = true;
      pollNow();
    } catch (e) {
      $("#queueMsg").className = "msg err";
      $("#queueMsg").textContent = e.message;
    }
  });
  update();
}

