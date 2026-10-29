import { $, $$, BY, CAT, api, app, catChip, catKey, esc, fmt, fmtDate, store } from "./core.js";
import { spkColor } from "./live.js";
import { Player } from "./player.js";
import { pollNow } from "./library.js";

/* Video page: summary, transcript with removed noise, speakers, concepts. */
/* ---------- video page ---------- */
export let spkNames = {};
/* "Hindi → English (translated)", or just the language; nothing for English. */
function langHtml(t) {
  if (!t || !t.language || (t.language === "en" && !t.translated)) return "";
  let name = t.language;
  try { name = new Intl.DisplayNames(["en"], { type: "language" }).of(t.language) || t.language; } catch { /* unknown code */ }
  return `<dt>Language</dt><dd>${esc(name)}${t.translated ? " → English (translated by Whisper)" : ""}</dd>`;
}

export async function viewVideo(vid, t) {
  const d = await api(`/api/video/${vid}`);
  const m = d.meta, k = d.kb, sents = d.sentences;
  spkNames = Object.fromEntries((d.speakers || []).map((s) => [s.id, s.name]));
  const removed = sents.filter((s) => !s.text);
  const counts = {};
  removed.forEach((s) => { const c = catKey(s.category); counts[c] = (counts[c] || 0) + 1; });
  const tabs = [["overview", "Overview"], ["transcript", "Transcript"], ["removed", `Removed (${removed.length})`],
    ["concepts", `Concepts (${k ? k.concepts.length : 0})`]];
  if (d.warnings.length) tabs.push(["warnings", `Warnings (${d.warnings.length})`]);
  const sbText = { ok: "found labelled segments", none: "no labels for this video", error: "unreachable", disabled: "off" }[d.sponsorblock] || "not checked";

  app.innerHTML = `<div class="vpage">
    <aside>
      <div class="player"><div id="player"></div></div>
      <div class="fallback" id="fallback" hidden>This video can't be embedded. <a id="fallbackLink" target="_blank" rel="noopener" href="https://youtu.be/${vid}?t=${Math.floor(t)}">Watch on YouTube ↗</a></div>
      <dl class="meta">
        <dt>Channel</dt><dd>${esc(m.channel || "")}</dd>
        <dt>Uploaded</dt><dd>${fmtDate(m.upload_date)}</dd>
        <dt>Length</dt><dd>${fmt(m.duration)}</dd>
        <dt>Source</dt><dd><a href="${esc(m.webpage_url)}" target="_blank" rel="noopener">YouTube ↗</a></dd>
        <dt>SponsorBlock</dt><dd>${sbText}</dd>
        ${langHtml(d.transcript)}
      </dl>
      ${removed.length ? `<div><div class="muted small" style="margin-bottom:6px">Removed as noise: ${removed.length} of ${sents.length} sentences</div>
        <div class="tags">${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `<a href="#" data-tab-jump="removed">${catChip(c).replace("</em>", ` ${n}</em>`)}</a>`).join("")}</div></div>` : ""}
      <div id="speakersBox"></div>
      ${k ? `<div class="tags">${k.tags.map((x) => `<a class="chip" href="#/search?q=${encodeURIComponent(x)}">#${esc(x)}</a>`).join("")}</div>` : ""}
    </aside>
    <section>
      <h1>${esc(m.title)}</h1>
      ${k ? `<p class="lead">${esc(k.tldr)}</p>` : `<div class="notice">This video hasn't finished processing. Add it again from the Library to complete it.</div>`}
      <div class="tabs">${tabs.map(([id, l]) => `<button data-tab="${id}">${l}</button>`).join("")}</div>
      <div id="tabbody"></div>
    </section></div>`;

  const renderers = {
    overview: () => k ? `<h2>Key takeaways</h2><ul class="clean">${k.takeaways.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <h2>Outline</h2><ol class="outline">${k.outline.map((o) => `<li><button class="ts" data-t="${o.t}">${fmt(o.t)}</button><span>${esc(o.title)}</span></li>`).join("")}</ol>` : "",
    transcript: () => transcriptHtml(sents, m.chapters || [], spkNames),
    removed: () => removedHtml(removed, sents.length, counts),
    concepts: () => k ? `<div class="ccards">${k.concepts.map((c) => `<div class="card ccard">
        <h3><a href="#/c/${encodeURIComponent(c.slug)}">${esc(c.name)}</a><span class="chip">${esc(c.kind)}</span></h3>
        <p>${esc(c.summary)}</p>${c.facts.length ? `<ul>${c.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
        <div class="times">${c.times.map((x) => `<button class="ts" data-t="${x}">${fmt(x)}</button>`).join("")}</div></div>`).join("")}</div>` : "",
    warnings: () => `<div class="notice">Automatic checks raised these. They usually need no action; skim for possible ads left in or odd rewrites.</div>
      <ul class="warnlist">${d.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`,
  };
  const show = (id) => {
    $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === id));
    $("#tabbody").innerHTML = renderers[id]();
    store.set("tkb.tab", id);
    Player.onTime = id === "transcript" ? followTranscript : null;
    if (id === "transcript") wireTranscript();
    if (id === "removed") wireRemoved();
  };
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
  $$("[data-tab-jump]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); show(a.dataset.tabJump); }));
  const saved = store.get("tkb.tab", "overview");
  show(tabs.some(([id]) => id === saved) ? saved : "overview");
  renderSpeakers(vid, d);
  Player.mount(vid, t);
}

export async function deleteVideo(vid, btn, onDone) {
  const p = await api(`/api/video/${vid}/delete-preview`);
  const parts = [`its audio, transcripts and notes (${p.mb} MB)`];
  if (p.concepts_deleted) parts.push(`${p.concepts_deleted} concept note${p.concepts_deleted > 1 ? "s" : ""} that only this video supports`);
  if (p.concepts_trimmed) parts.push(`its section in ${p.concepts_trimmed} concept note${p.concepts_trimmed > 1 ? "s" : ""} shared with other videos`);
  if (!confirm(`Delete “${p.title}” permanently?\n\nThis removes ${parts.join(", ")}, plus its queue history, Discover's memory of it, and saved Ask answers that cite it.\n\nThis can't be undone.`)) return;
  btn.disabled = true;
  btn.closest(".vcard")?.classList.add("deleting");
  try {
    await api(`/api/video/${vid}/delete`, {});
    flash(`Deleted “${p.title}”.`);
    await onDone();
  } catch (e) {
    btn.disabled = false;
    btn.closest(".vcard")?.classList.remove("deleting");
    alert(e.message);
  }
}

export function flash(msg) {
  const el = document.createElement("div");
  el.className = "flash";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

export function renderSpeakers(vid, d) {
  const box = $("#speakersBox");
  if (!box) return;
  const sp = d.speakers || [];
  const redetect = `<div class="row small" style="margin-top:8px"><span class="muted">Wrong number of speakers?</span>
    <select id="nSpk">${[1, 2, 3, 4, 5, 6].map((n) => `<option ${n === Math.max(sp.length, 1) ? "selected" : ""}>${n}</option>`).join("")}</select>
    <button class="btn small" id="redetect">Re-detect</button></div><div class="msg small" id="spkMsg"></div>`;
  if (!d.speakers_checked) {
    box.innerHTML = `<div class="muted small">Speakers weren't identified for this video.</div>${redetect}`;
  } else if (!sp.length) {
    box.innerHTML = `<div class="muted small">One speaker throughout.</div>${redetect}`;
  } else {
    box.innerHTML = `<div class="spkhead">Speakers</div><div class="spklist">${sp.map((s) => `
      <div class="spk" data-id="${s.id}">
        <span class="dot" style="background:${spkColor(s.id)}"></span>
        <span class="spkname" title="${s.evidence ? esc("Identified from: “" + s.evidence + "”") : s.renamed ? "Renamed by you" : "Name not stated in the video"}">${esc(s.name)}</span>
        <span class="muted small">${esc(s.role || "")}</span>
        <span class="spkbar"><span style="width:${Math.round(s.share * 100)}%;background:${spkColor(s.id)}"></span></span>
        <span class="muted small">${Math.round(s.share * 100)}%</span>
        <button class="btn ghost small" data-rename="${s.id}">Rename</button></div>`).join("")}</div>${redetect}`;
  }
  $$("[data-rename]", box).forEach((b) => b.addEventListener("click", () => {
    const row = b.closest(".spk"), id = b.dataset.rename, cur = sp.find((s) => s.id === id);
    row.querySelector(".spkname").outerHTML = `<input type="text" class="spkinput" value="${esc(cur.name)}" placeholder="${esc(cur.auto_name || "Name")}">`;
    const input = row.querySelector(".spkinput");
    input.focus(); input.select();
    b.textContent = "Save";
    const save = async () => {
      try {
        const nd = await api(`/api/video/${vid}/speakers`, { names: { [id]: input.value } });
        Object.assign(d, nd);
        renderSpeakers(vid, d);
        spkNames = Object.fromEntries((d.speakers || []).map((s) => [s.id, s.name]));
        if ($(".tabs button.on")?.dataset.tab === "transcript") $(".tabs button.on").click();
        const msg = $("#spkMsg");
        msg.className = "msg small";
        msg.innerHTML = `Transcript updated. Knowledge-base notes still use the old name. <a href="#" id="redoKb">Update notes</a> (uses AI credits)`;
        $("#redoKb").addEventListener("click", async (e) => {
          e.preventDefault();
          await api("/api/jobs", { text: vid, force: true, stage: "kb", redo_kb: true });
          msg.innerHTML = `Queued. <a href="#/">Progress in the Library →</a>`;
          pollNow();
        });
      } catch (e) { alert(e.message); }
    };
    b.onclick = save;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") renderSpeakers(vid, d); });
  }, { once: true }));
  $("#redetect")?.addEventListener("click", async () => {
    const n = Number($("#nSpk").value), msg = $("#spkMsg");
    await api("/api/jobs", { text: vid, force: true, stage: "speakers", num_speakers: n });
    msg.className = "msg small ok";
    msg.innerHTML = `Re-detecting with ${n} speaker${n > 1 ? "s" : ""}. <a href="#/">Progress in the Library →</a> Reload this page when it's done.`;
    pollNow();
  });
}

export function transcriptHtml(sents, chapters, names = {}) {
  if (!sents.length) return `<div class="notice">No cleaned transcript yet.</div>`;
  const showRemoved = store.get("tkb.showRemoved", false), follow = store.get("tkb.follow", true);
  const chs = chapters.filter((c) => !/^<Untitled Chapter \d+>$/.test(c.title)).sort((a, b) => a.start_time - b.start_time);
  let ci = 0, html = "", para = null, buf = [], anyKept = false, pending = null, shownGone = false, lastWho = null;
  const flush = () => {
    if (!buf.length) return;
    const first = buf.find((s) => s.text) || buf[0];
    // A chapter heading goes with its first kept paragraph; a chapter that was removed entirely
    // only shows its heading in "show removed" mode.
    if (pending && !anyKept) { html += `<h3 class="gone">${esc(pending)}</h3>`; shownGone = true; }
    if (pending && anyKept) { html += `<h3 class="${shownGone ? "dup" : ""}">${esc(pending)}</h3>`; pending = null; shownGone = false; }
    const showWho = names[first.spk] && first.spk !== lastWho && anyKept;
    if (anyKept) lastWho = first.spk;
    const who = showWho ? `<span class="who" style="color:${spkColor(first.spk)}">${esc(names[first.spk])}</span>` : "";
    html += `<p class="${anyKept ? "" : "gone"}">${who}<button class="ts" data-t="${Math.floor(first.start)}">${fmt(first.start)}</button> ${buf.map((s) => s.text
      ? `<span class="s" data-t="${s.start}" data-start="${s.start}">${esc(s.text)}</span>`
      : `<del data-t="${s.start}" title="${esc(CAT[s.category] || s.category)} · removed by ${esc(BY[s.by] || s.by)}">${catChip(s.category)}${esc(s.src)}</del>`).join(" ")}</p>`;
    buf = []; anyKept = false;
  };
  for (const s of sents) {
    const key = `${s.para}|${s.spk || ""}`;
    if (key !== para) {
      flush();
      para = key;
      while (ci < chs.length && chs[ci].start_time <= s.start + 1) { pending = chs[ci++].title; shownGone = false; }
    }
    buf.push(s);
    if (s.text) anyKept = true;
  }
  flush();
  return `<div class="toolbar">
      <label class="check"><input type="checkbox" id="showRemoved" ${showRemoved ? "checked" : ""}> Show removed noise (struck through)</label>
      <label class="check"><input type="checkbox" id="follow" ${follow ? "checked" : ""}> Follow along while playing</label>
      <span class="muted small">Click any sentence to jump there.</span>
    </div><div class="transcript ${showRemoved ? "show-removed" : ""}" id="transcript">${html}</div>`;
}
export let lastNow = null;
export function wireTranscript() {
  $("#showRemoved")?.addEventListener("change", (e) => { store.set("tkb.showRemoved", e.target.checked); $("#transcript").classList.toggle("show-removed", e.target.checked); });
  $("#follow")?.addEventListener("change", (e) => store.set("tkb.follow", e.target.checked));
  lastNow = null;
}
export function followTranscript(t) {
  const spans = $$("#transcript .s");
  if (!spans.length) return;
  let cur = null;
  for (const s of spans) { if (Number(s.dataset.start) <= t + 0.3) cur = s; else break; }
  if (cur === lastNow) return;
  lastNow?.classList.remove("now");
  cur?.classList.add("now");
  lastNow = cur;
  if (cur && $("#follow")?.checked) cur.scrollIntoView({ block: "center", behavior: "smooth" });
}

export function removedHtml(removed, total, counts) {
  if (!removed.length) return `<div class="notice">Nothing was removed from this video.</div>`;
  const max = Math.max(...Object.values(counts));
  return `<div class="notice">Each removed sentence, why it was removed and who decided: <b>SponsorBlock</b> (community-labelled ad segments)
      or the <b>LLM</b>. ${removed.length} of ${total} sentences (${Math.round((100 * removed.length) / total)}%). Click a row to hear it.</div>
    <div class="catbars">${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `<div class="catbar cat-${c}" style="background:none">
      <span>${catChip(c)}</span><div class="track"><span style="width:${(100 * n) / max}%"></span></div><span class="muted">${n}</span></div>`).join("")}</div>
    <div class="filters">${Object.keys(counts).map((c) => `<button class="chip cat-${c}" data-filter="${c}"><span class="dot"></span>${esc(CAT[c] || c)}</button>`).join("")}</div>
    <ul class="rlist">${removed.map((s) => `<li data-cat="${catKey(s.category)}"><button class="ts" data-t="${s.start}">${fmt(s.start)}</button>
      <span>${catChip(s.category)} <span class="by">${esc(BY[s.by] || s.by)}</span></span><span>${esc(s.src)}</span></li>`).join("")}</ul>`;
}
export function wireRemoved() {
  const off = new Set();
  $$("[data-filter]").forEach((b) => b.addEventListener("click", () => {
    const c = b.dataset.filter;
    off.has(c) ? off.delete(c) : off.add(c);
    b.classList.toggle("off", off.has(c));
    $$(".rlist li").forEach((li) => { li.hidden = off.has(li.dataset.cat); });
  }));
}

