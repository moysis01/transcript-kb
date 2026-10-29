import { $, $$, api, app, esc, store } from "./core.js";

/* Settings: every fixed value of the system, section by section, like a CMS. Each section saves on its own;
   technical settings sit under "Show advanced". */
const APPLIES = { "next video": "Next video", redo: "New videos and Redo", transcribe: "New transcriptions",
  detect: "Next speaker detection" };
const appliesTag = (f) => (APPLIES[f.applies] ? `<span class="tag" title="When a change takes effect">${APPLIES[f.applies]}</span>` : "");

export async function viewSettings(sectionId) {
  const page = await api("/api/settings");
  const ids = [...page.sections.map((s) => s.id), "about"];
  const current = ids.includes(sectionId) ? sectionId : ids[0];
  app.innerHTML = `<h1>Settings</h1><div class="settings">
    <nav class="snav" aria-label="Settings sections">${page.sections.map((s) => `<a href="#/settings/${s.id}" class="${s.id === current ? "on" : ""}">${esc(s.title)}</a>`).join("")}
      <a href="#/settings/about" class="${current === "about" ? "on" : ""}">About</a></nav>
    <div id="spanel"></div></div>`;
  $$(".snav a").forEach((a) => a.addEventListener("click", (e) => {
    if (dirty().length && !window.confirm("Discard unsaved changes?")) e.preventDefault();
  }));
  render(page, current);
}

let shown = null;  // the section on screen: {section, fields}
let providers = {};  // provider id -> {label, ready, why}
// Models a provider listed when tested, offered as suggestions wherever a model is typed in.
const found = () => Object.entries(store.get("tkb.models", {})).flatMap(([p, ms]) => ms.map((m) => `${p}/${m}`));
const dirty = () => (shown ? shown.fields.filter((f) => !same(read(f), f.value)) : []);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);  // values are numbers, strings, booleans or lists

function render(page, id) {
  const panel = $("#spanel");
  if (id === "about") {
    shown = null;
    panel.innerHTML = `<section class="card spanel"><h2>About</h2><p class="muted small">Where things are, and what's installed.</p>
      <dl class="about">${Object.entries(page.about).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></section>`;
    return;
  }
  const section = page.sections.find((s) => s.id === id);
  shown = { section, fields: section.fields };
  providers = page.providers || {};
  const prompts = id === "prompts";
  const advanced = section.fields.filter((f) => f.advanced);
  const openAdv = advanced.some((f) => !same(f.value, f.default));
  panel.innerHTML = `<section class="card spanel"><h2>${esc(section.title)}</h2><p class="muted small">${esc(section.description)}
      ${prompts ? " Changes to video prompts apply to new videos and to Redo." : ""}</p>
    <div class="sfields ${openAdv ? "adv" : ""}">${section.fields.map((f) => (prompts ? promptHtml(f) : fieldHtml(f, section))).join("")}</div>
    ${advanced.length ? `<button class="linkbtn sadv" id="sadv">${openAdv ? "Hide" : "Show"} advanced (${advanced.length})</button>` : ""}
    <datalist id="dl-models">${found().map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
    <div class="sbar"><span class="msg" id="smsg"></span><span class="grow"></span>
      <button class="btn" id="sdiscard" disabled>Discard</button><button class="btn primary" id="ssave" disabled>Save</button></div></section>`;
  for (const f of section.fields) bind(f);
  $("#sadv")?.addEventListener("click", () => {
    const on = $(".sfields").classList.toggle("adv");
    $("#sadv").textContent = `${on ? "Hide" : "Show"} advanced (${advanced.length})`;
  });
  $("#sdiscard").addEventListener("click", () => render(page, id));
  $("#ssave").addEventListener("click", () => save(page, id));
  update();
}

function shownDefault(f) {
  if (f.kind === "bool") return f.default ? "On" : "Off";
  if (f.kind === "choice") return f.choices.find(([v]) => v === f.default)?.[1];
  if (f.kind === "multi") return f.default.length === f.choices.length ? "all" : f.choices.filter(([v]) => f.default.includes(v)).map(([, l]) => l).join(", ") || "none";
  if (f.kind === "list") return `${f.default.length} lines`;
  if (f.kind === "model" && !f.default) return "the default model";
  return f.choices?.find(([v]) => v === f.default)?.[1] || String(f.default);
}

function fieldHtml(f, section) {
  const lock = f.env ? `<span class="tag lock" title="Set when the app was started">Set by ${esc(f.env)}</span>` : "";
  const wide = f.kind === "list" ? "wide" : "";
  return `<div class="sfield ${wide} ${f.advanced ? "advanced" : ""}" data-key="${esc(f.key)}">
    <div class="slabel"><b>${esc(f.label)}</b> ${appliesTag(f)}${lock}
      ${f.help ? `<div class="muted small">${esc(f.help)}</div>` : ""}</div>
    <div class="scontrol">${controlHtml(f, section)}
      ${f.kind === "model" ? `<div class="snote small"></div>` : ""}
      ${f.test ? `<button class="btn small stestbtn" data-test="${esc(f.test)}">Test connection</button><div class="stest small"></div>` : ""}
      <div class="sdefault muted small" hidden>Default: ${esc(shownDefault(f) || "none")} · <button class="linkbtn" data-reset>Reset</button></div></div></div>`;
}

function controlHtml(f, section) {
  const off = f.env ? "disabled" : "";
  if (f.kind === "bool") return `<label class="switch"><input type="checkbox" ${f.value ? "checked" : ""} ${off}><span></span></label>`;
  if (f.kind === "int" || f.kind === "float") return `<input type="number" min="${f.lo}" max="${f.hi}" step="${f.kind === "int" ? 1 : f.step || "any"}" value="${f.value}" ${off}>`;
  if (f.kind === "multi") return `<div class="smulti">${f.choices.map(([v, l]) => `<label class="check"><input type="checkbox" value="${esc(v)}" ${f.value.includes(v) ? "checked" : ""} ${off}> ${esc(l)}</label>`).join("")}</div>`;
  if (f.kind === "list") return `<textarea class="slist" rows="${Math.min(12, Math.max(4, f.value.length + 1))}" spellcheck="false" ${off}>${esc(f.value.join("\n"))}</textarea>`;
  if (f.kind === "choice") return `<select ${off}>${f.choices.map(([v, l]) => `<option value="${esc(v)}" ${v === f.value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  if (f.kind === "secret") return `<input type="password" autocomplete="off" spellcheck="false" placeholder="${f.has_value ? "Saved. Type a new key to replace it" : "Paste a key"}" ${off}>
    ${f.has_value && !f.env ? `<button class="linkbtn sclear" data-clear>Clear the saved key</button>` : ""}`;
  if (f.kind === "url" || f.kind === "line") return `<input type="text" spellcheck="false" value="${esc(f.value)}" placeholder="${f.kind === "url" ? "https://…" : "program --option {model}"}" ${off}>`;
  // model, speech or search model: a list of common ones, or anything typed in ("Other…")
  const base = section.fields.find((x) => x.key === "model.default");
  const follows = f.kind === "model" && f.key !== "model.default";
  const known = f.choices.some(([v]) => v === f.value) || (f.value === "" && follows);
  const opts = [...(follows ? [["", `Default (${base.value})`]] : []), ...f.choices, ["__other", "Other…"]];
  const hint = { model: "provider/model, e.g. openai/gpt-4.1, ollama/llama3.1", speech: "e.g. mlx-community/whisper-small-mlx",
    embed: "e.g. openai/text-embedding-3-small" }[f.kind];
  return `<select data-pick ${off}>${opts.map(([v, l]) => `<option value="${esc(v)}" ${(known ? v === f.value : v === "__other") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
    <input type="text" class="smodel" ${f.kind === "model" ? `list="dl-models"` : ""} spellcheck="false" placeholder="${hint}" value="${known ? "" : esc(f.value)}" ${known ? "hidden" : ""} ${off}>`;
}

function promptHtml(f) {
  return `<details class="sprompt" data-key="${esc(f.key)}"><summary><b>${esc(f.label)}</b> ${appliesTag(f)}
      <span class="tag edited" ${f.value === f.default ? "hidden" : ""}>Edited</span></summary>
    <textarea spellcheck="false">${esc(f.value)}</textarea>
    <div class="sdefault muted small" hidden><button class="linkbtn" data-reset>Reset to the original</button></div></details>`;
}

const row = (f) => $(`[data-key="${CSS.escape(f.key)}"]`);

function read(f) {
  const el = row(f);
  if (f.kind === "text") return $("textarea", el).value;
  if (f.kind === "list") return [...new Set($("textarea", el).value.split("\n").map((x) => x.trim()).filter(Boolean))];
  if (f.kind === "multi") return $$("input", el).filter((c) => c.checked).map((c) => c.value);
  if (f.kind === "bool") return $("input", el).checked;
  if (f.kind === "int" || f.kind === "float") return $("input", el).value === "" ? f.value : Number($("input", el).value);
  if (["model", "speech", "embed"].includes(f.kind)) { const s = $("select", el).value; return s === "__other" ? $(".smodel", el).value.trim() : s; }
  if (f.kind === "secret") return el.dataset.cleared ? null : $("input", el).value.trim();
  if (f.kind === "url" || f.kind === "line") return $("input", el).value.trim();
  return $("select", el).value;
}

function write(f, v) {
  const el = row(f);
  if (f.kind === "text") $("textarea", el).value = v;
  else if (f.kind === "bool") $("input", el).checked = v;
  else if (f.kind === "int" || f.kind === "float") $("input", el).value = v;
  else if (f.kind === "multi") $$("input", el).forEach((c) => { c.checked = v.includes(c.value); });
  else if (f.kind === "list") $("textarea", el).value = v.join("\n");
  else if (f.kind === "secret") { delete el.dataset.cleared; $("input", el).value = ""; }
  else if (f.kind === "url" || f.kind === "line") $("input", el).value = v;
  else if (["model", "speech", "embed"].includes(f.kind)) {
    const listed = [...$("select", el).options].some((o) => o.value === v);
    $("select", el).value = listed ? v : "__other";
    $(".smodel", el).hidden = listed;
    if (!listed) $(".smodel", el).value = v;
  } else $("select", el).value = v;
}

function bind(f) {
  const el = row(f);
  $$("input, select, textarea", el).forEach((c) => c.addEventListener("input", update));
  $$("select", el).forEach((c) => c.addEventListener("change", () => {
    if (c.dataset.pick !== undefined) { $(".smodel", el).hidden = c.value !== "__other"; if (c.value === "__other") $(".smodel", el).focus(); }
    update();
  }));
  $("[data-reset]", el)?.addEventListener("click", (e) => { e.preventDefault(); write(f, f.default); update(); });
  $("[data-clear]", el)?.addEventListener("click", (e) => {
    e.preventDefault();
    el.dataset.cleared = "1";
    $("input", el).value = "";
    $("input", el).placeholder = "Will be cleared when you save";
    update();
  });
  $("[data-test]", el)?.addEventListener("click", () => testProvider(f.test, el));
}

/* Ask a provider which models it has: proves the address and key, and offers its models as suggestions. */
async function testProvider(provider, el) {
  const out = $(".stest", el);
  if (dirty().length) { out.className = "stest small err"; out.textContent = "Save first, then test."; return; }
  out.className = "stest small muted";
  out.textContent = "Testing…";
  try {
    const r = await api("/api/providers/test", { provider });
    out.className = `stest small ${r.ok ? "ok" : "err"}`;
    out.textContent = r.ok ? `${r.message}${r.models.length ? `: ${r.models.slice(0, 6).join(", ")}${r.models.length > 6 ? "…" : ""}` : ""}` : r.message;
    if (r.ok && r.models.length) {
      store.set("tkb.models", { ...store.get("tkb.models", {}), [provider]: r.models });
      out.textContent += " (offered as suggestions under AI models → Other…)";
    }
  } catch (e) {
    out.className = "stest small err";
    out.textContent = e.message;
  }
}

/* Under a model choice: a warning when its provider isn't set up yet. */
function providerNote(f, v) {
  const base = shown.fields.find((x) => x.key === "model.default");
  const ref = v || (base ? read(base) : "") || "";
  const id = ref.includes("/") && providers[ref.split("/")[0]] ? ref.split("/")[0] : "claude-code";
  const p = providers[id];
  return p && p.ready === false ? `⚠ ${esc(p.label)}: ${esc(p.why)}. <a href="#/settings/providers">Set it up</a>` : "";
}

function update() {
  for (const f of shown.fields) {
    const el = row(f), v = read(f);
    $(".sdefault", el).hidden = f.env || f.kind === "secret" || same(v, f.default);
    if (f.kind === "model") $(".snote", el).innerHTML = providerNote(f, v);
    el.classList.toggle("changed", !same(v, f.value));
    const edited = $(".edited", el);
    if (edited) edited.hidden = same(v, f.default);
  }
  const n = dirty().length;
  $("#ssave").disabled = $("#sdiscard").disabled = !n;
  const msg = $("#smsg");
  if (n || !msg.classList.contains("ok")) { msg.className = "msg"; msg.textContent = n ? `${n} unsaved change${n > 1 ? "s" : ""}` : ""; }
}

async function save(page, id) {
  const values = Object.fromEntries(dirty().map((f) => [f.key, read(f)]));
  $("#ssave").disabled = true;
  try {
    const fresh = await api("/api/settings", { values });
    Object.assign(page, fresh);
    render(page, id);
    $("#smsg").className = "msg ok";
    $("#smsg").textContent = "Saved";
  } catch (e) {
    $("#smsg").className = "msg err";
    $("#smsg").textContent = e.message;
    $("#ssave").disabled = false;
  }
}
