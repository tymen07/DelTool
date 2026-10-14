/* DelTool frontend: bbox selection, job submission/polling, results display,
 * polygon editing, field database & export. */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
};
const fmt = (v, d = 0) => Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtDate = (iso) => new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const state = {
  config: null,
  system: null,
  model: "finetuned",
  deviceChoice: "auto",
  resolution: 1,
  bboxLayer: null,
  activeJob: null,
  jobs: [],
  geojson: null,
  fieldsLayer: null,
  layers: new Map(),        // field_id -> Leaflet layer of the visible fields
  hovered: null,
  imageryLayer: null,
  pollTimer: null,
  // editing
  editing: false,
  selected: new Set(),
  vertexLayer: null,        // the one field whose vertices are being edited
  drawKind: null,           // "bbox" | "field" | "split" while drawing
  createKind: null,
  // database
  database: null,
  dbLayer: null,
};

// ------------------------------------------------------------------ toasts
function toast(message, kind = "info", ms = 4500) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, ms);
}

// ------------------------------------------------------------------ map
const GHANA = L.latLngBounds([4.6, -3.3], [11.2, 1.25]);
const panelPadding = () => (window.innerWidth > 640 ? [404, 24] : [24, 24]);
const fitView = (bounds) => map.fitBounds(bounds, { paddingTopLeft: panelPadding(), paddingBottomRight: [24, 24] });

const map = L.map("map", { zoomControl: false });
fitView(GHANA);
map.createPane("database").style.zIndex = 390;   // saved fields below this run's fields
map.createPane("labels").style.zIndex = 650;     // place names above the fields
map.getPane("labels").style.pointerEvents = "none";

L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Basemap © Esri, Maxar, Earthstar Geographics" }
).addTo(map);
const esriLabels = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, pane: "labels" }
);
L.control.zoom({ position: "topright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);

// Map buttons under the zoom control: zoom to Ghana, place labels.
const MapButtons = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const bar = L.DomUtil.create("div", "leaflet-bar map-buttons");
    bar.innerHTML = `
      <a href="#" id="btn-home" role="button" title="Zoom to Ghana" aria-label="Zoom to Ghana">
        <svg viewBox="0 0 20 20"><path d="M3 9.5L10 3l7 6.5M5 8v9h10V8"/></svg></a>
      <a href="#" id="btn-labels" role="button" title="Place names and borders" aria-label="Place names and borders" aria-pressed="false">
        <svg viewBox="0 0 20 20"><path d="M3 4h8l6 6-7 7-7-7z"/><circle cx="7" cy="8" r="1.3"/></svg></a>`;
    L.DomEvent.disableClickPropagation(bar);
    return bar;
  },
});
new MapButtons().addTo(map);
$("btn-home").onclick = (e) => { e.preventDefault(); fitView(GHANA); };
function setLabels(on) {
  on ? esriLabels.addTo(map) : map.removeLayer(esriLabels);
  $("btn-labels").classList.toggle("active", on);
  $("btn-labels").setAttribute("aria-pressed", on);
  try { localStorage.setItem("deltool-labels", on ? "1" : "0"); } catch (_) {}
}
$("btn-labels").onclick = (e) => { e.preventDefault(); setLabels(!map.hasLayer(esriLabels)); };
try { setLabels(localStorage.getItem("deltool-labels") === "1"); } catch (_) { setLabels(false); }

const CYAN = "#00A6D6", YELLOW = "#FFB81C", GREEN = "#6CC24A";
const BBOX_STYLE = { color: "#ffffff", weight: 2, fill: false, dashArray: "8 6", interactive: false, snapIgnore: true };
const FIELD_STYLE = { color: CYAN, weight: 1.8, fillColor: CYAN, fillOpacity: 0.14 };
const FIELD_HOVER = { color: YELLOW, weight: 2.6, fillColor: YELLOW, fillOpacity: 0.22 };
const FIELD_SELECTED = { color: YELLOW, weight: 3, fillColor: YELLOW, fillOpacity: 0.32 };
const DRAW_STYLE = { color: YELLOW, weight: 2.5, fillColor: YELLOW, fillOpacity: 0.15 };
const DB_STYLE = { color: GREEN, weight: 1.4, dashArray: "4 3", fillColor: GREEN, fillOpacity: 0.07, pane: "database", pmIgnore: true };
const SNAP = { snappable: true, snapDistance: 14 };

// ------------------------------------------------------------ tooltips
// Hover, focus or click an (i) icon to read more. Clicking an icon never
// triggers the button/label/summary it sits in.
const tip = $("tooltip");
function showTip(t) {
  tip.textContent = t.dataset.tip;
  const r = t.getBoundingClientRect();
  tip.classList.add("show");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 8) top = r.top - h - 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
const hideTip = () => tip.classList.remove("show");
document.addEventListener("mouseover", (e) => { const t = e.target.closest?.(".info"); if (t) showTip(t); });
document.addEventListener("mouseout", (e) => e.target.closest?.(".info") && hideTip());
document.addEventListener("focusin", (e) => { const t = e.target.closest?.(".info"); if (t) showTip(t); });
document.addEventListener("focusout", (e) => e.target.closest?.(".info") && hideTip());
document.addEventListener("click", (e) => {
  const t = e.target.closest?.(".info");
  if (!t) return hideTip();
  e.preventDefault();
  e.stopPropagation();
  showTip(t);
}, true);
const infoIcon = (text) => `<i class="info" tabindex="0" data-tip="${esc(text)}"></i>`;
document.querySelectorAll(".info").forEach((i) => i.setAttribute("tabindex", "0"));

// Filled track for range inputs (WebKit has no ::range-progress).
function paintRange(input) {
  const pct = ((input.value - input.min) / (input.max - input.min)) * 100;
  input.style.setProperty("--pct", `${pct}%`);
}
document.querySelectorAll("input[type=range]").forEach((r) => {
  r.addEventListener("input", () => paintRange(r));
  paintRange(r);
});

// ------------------------------------------------------ model & device
const modelSpec = (key) => state.system?.models[key] ?? { name: key, short: key };

function effectiveDevice() {
  const cuda = state.system?.devices.cuda_available;
  return state.deviceChoice === "cpu" || !cuda ? "cpu" : "cuda";
}

function renderModelOptions() {
  const box = $("model-options");
  box.innerHTML = "";
  for (const [key, m] of Object.entries(state.system.models)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "model-opt";
    btn.setAttribute("role", "radio");
    btn.disabled = !m.available;
    btn.dataset.key = key;
    const desc = m.available ? `${m.name}. ${m.description}` : `${m.name}: weights file not found in DelAny/.`;
    btn.innerHTML = `
      ${m.recommended ? '<span class="mo-badge">Recommended</span>' : ""}
      <span class="mo-name">${esc(m.short)}</span>${infoIcon(desc)}
      <span class="mo-sub">${esc(m.available ? m.tagline : "Not installed")}</span>`;
    btn.onclick = () => selectModel(key);
    box.appendChild(btn);
  }
  selectModel(state.model);
}

function selectModel(key) {
  state.model = key;
  const m = modelSpec(key);
  document.querySelectorAll(".model-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.key === key);
    b.setAttribute("aria-checked", b.dataset.key === key);
  });
  $("active-model-name").textContent = m.name;
  $("active-model-tag").textContent = m.recommended ? "Recommended" : "For large fields";
}

function renderDevicePicker() {
  const d = state.system.devices;
  const options = [["auto", "Auto"], ["cuda", "GPU"], ["cpu", "CPU"]];
  $("device").innerHTML = options.map(([k, l]) => `<button type="button" data-dev="${k}">${l}</button>`).join("");
  $("device").querySelectorAll("button").forEach((b) => {
    b.disabled = b.dataset.dev === "cuda" && !d.cuda_available;
    if (b.disabled) b.title = d.hint;
    b.onclick = () => selectDevice(b.dataset.dev);
  });
  const hint = $("device-hint");
  hint.classList.remove("hidden");
  hint.classList.toggle("ok", d.cuda_available);
  hint.innerHTML = d.cuda_available ? `GPU available: ${esc(d.gpu_name)}` : `${esc(d.hint_short || d.hint)} ${infoIcon(d.hint)}`;
  selectDevice(state.deviceChoice);
}

function selectDevice(choice) {
  state.deviceChoice = choice;
  $("device").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.dev === choice));
  const gpu = effectiveDevice() === "cuda";
  $("device-badge").textContent = gpu ? "GPU" : "CPU";
  $("device-badge").classList.toggle("gpu", gpu);
  $("device-badge").title = gpu ? state.system.devices.gpu_name : "Running on CPU";
  if (currentBBox()) schedulePlan();
}

// ------------------------------------------------------------- drawing
// One Geoman draw mode at a time: the bbox (rectangle), a new field
// (polygon) or a split line.
function startDraw(kind) {
  map.pm.disableDraw();
  state.drawKind = state.createKind = kind;
  if (kind === "bbox") {
    const hint = { ...BBOX_STYLE, dashArray: "4 6" };
    map.pm.enableDraw("Rectangle", { pathOptions: BBOX_STYLE, templineStyle: BBOX_STYLE, hintlineStyle: hint, snappable: false });
  } else {
    const shape = kind === "field" ? "Polygon" : "Line";
    map.pm.enableDraw(shape, { ...SNAP, allowSelfIntersection: false, pathOptions: DRAW_STYLE,
      templineStyle: DRAW_STYLE, hintlineStyle: { ...DRAW_STYLE, dashArray: "5 5" } });
  }
  syncVertexEdit();
  updateEditToolbar();
  $("btn-draw").classList.toggle("active", kind === "bbox");
}

function cancelDraw() {
  if (state.drawKind) map.pm.disableDraw();
}

map.on("pm:drawend", () => {
  state.drawKind = null;
  $("btn-draw").classList.remove("active");
  syncVertexEdit();
  updateEditToolbar();
});

map.on("pm:create", (e) => {
  const kind = state.createKind;
  state.createKind = null;
  map.removeLayer(e.layer);
  if (kind === "bbox") setBBox(e.layer.getBounds());
  else if (kind === "field") editRequest("/fields", { geometry: e.layer.toGeoJSON().geometry, avoid_overlap: avoidOverlap() });
  else if (kind === "split") {
    editRequest("/fields/split", { line: e.layer.toGeoJSON().geometry, field_ids: [...state.selected] });
  }
});

// ------------------------------------------------------------- bbox
$("btn-draw").onclick = () => { setEditing(false); startDraw("bbox"); };
$("btn-clear-bbox").onclick = () => setBBox(null);

function setBBox(bounds, { fit = false } = {}) {
  if (state.bboxLayer) map.removeLayer(state.bboxLayer);
  state.bboxLayer = null;
  if (!bounds) {
    ["w", "s", "e", "n"].forEach((k) => ($(`bbox-${k}`).value = ""));
    $("plan-info").innerHTML = "Draw a box on the map to start.";
    updateGenerateButton(false);
    return;
  }
  state.bboxLayer = L.rectangle(bounds, BBOX_STYLE).addTo(map);
  const onEdit = () => { writeBBoxInputs(state.bboxLayer.getBounds()); schedulePlan(); };
  state.bboxLayer.on("pm:edit", onEdit);
  state.bboxLayer.on("pm:dragend", onEdit);
  syncBBoxEditing();
  writeBBoxInputs(bounds);
  if (fit) fitView(bounds);
  schedulePlan();
}

// The box corners can be dragged, except while fields are being edited.
function syncBBoxEditing() {
  const b = state.bboxLayer;
  if (!b) return;
  if (state.editing) b.pm.enabled() && b.pm.disable();
  else if (!b.pm.enabled()) b.pm.enable({ snappable: false, draggable: false });
}

function writeBBoxInputs(b) {
  $("bbox-w").value = b.getWest().toFixed(6);
  $("bbox-s").value = b.getSouth().toFixed(6);
  $("bbox-e").value = b.getEast().toFixed(6);
  $("bbox-n").value = b.getNorth().toFixed(6);
}

function currentBBox() {
  const v = ["w", "s", "e", "n"].map((k) => parseFloat($(`bbox-${k}`).value));
  return v.some(Number.isNaN) ? null : v;
}

["w", "s", "e", "n"].forEach((k) =>
  $(`bbox-${k}`).addEventListener("change", () => {
    const b = currentBBox();
    if (b && b[0] < b[2] && b[1] < b[3]) setBBox(L.latLngBounds([b[1], b[0]], [b[3], b[2]]), { fit: true });
  })
);

$("btn-goto").onclick = () => {
  const m = $("goto").value.split(/[,\s]+/).filter(Boolean).map(parseFloat);
  if (m.length >= 2 && !m.some(Number.isNaN)) map.setView([m[0], m[1]], 15);
};
$("goto").addEventListener("keydown", (e) => e.key === "Enter" && $("btn-goto").click());

// ------------------------------------------------------------- params
function resolution() {
  const custom = parseFloat($("resolution-custom").value);
  return custom > 0 ? custom : state.resolution;
}

function renderResolutionPicker(choices, selected) {
  $("resolution").innerHTML = choices.map((r) => `<button type="button" data-res="${r}">${r} m</button>`).join("");
  $("resolution").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", parseFloat(b.dataset.res) === selected);
    b.onclick = () => {
      state.resolution = parseFloat(b.dataset.res);
      $("resolution-custom").value = "";
      $("resolution").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      schedulePlan();
    };
  });
}
$("resolution-custom").addEventListener("input", () => {
  const custom = parseFloat($("resolution-custom").value) > 0;
  $("resolution").querySelectorAll("button").forEach((x) =>
    x.classList.toggle("active", !custom && parseFloat(x.dataset.res) === state.resolution));
  schedulePlan();
});

function readParams() {
  return {
    bbox: currentBBox(),
    resolution_m: resolution(),
    model: state.model,
    device: state.deviceChoice,
    min_area_m2: parseFloat($("min_area_m2").value),
    simplify_m: parseFloat($("simplify_m").value),
    overlap: parseFloat($("overlap").value),
    iou: parseFloat($("iou").value),
    batch_size: parseInt($("batch_size").value, 10),
    name: $("run-name").value,
  };
}

let planTimer = null;
let planOk = false;
function schedulePlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(updatePlan, 250);
}

async function updatePlan() {
  const p = readParams();
  if (!p.bbox) return;
  try {
    const plan = await api(`/api/plan?overlap=${p.overlap}`, {
      method: "POST", body: { bbox: p.bbox, resolution_m: p.resolution_m },
    });
    const gpu = effectiveDevice() === "cuda";
    const secPerTile = gpu ? 0.08 : 1.6;
    const etaS = plan.model_tiles * secPerTile + plan.download_tiles * 0.03 + 5;
    const eta = etaS < 90 ? `${Math.ceil(etaS / 5) * 5} s` : `${Math.ceil(etaS / 60)} min`;
    const db = plan.database;
    $("plan-info").innerHTML = `
      <div class="kv">
        <span>Area</span><span><b>${fmt(plan.area_km2, 2)} km²</b> (${fmt(plan.area_km2 * 100)} ha)</span>
        <span>Raster</span><span>${fmt(plan.width_px)} × ${fmt(plan.height_px)} px</span>
        <span>Projection</span><span>${plan.crs} · zoom ${plan.zoom}</span>
        <span>Model tiles</span><span>${fmt(plan.model_tiles)} (${fmt(plan.download_tiles)} imagery tiles)</span>
        <span>Estimated time</span><span>~${eta} on ${gpu ? "GPU" : "CPU"}</span>
      </div>
      ${db.fields ? `<div class="db-hit"><b>${fmt(db.fields)}</b> saved fields already in this area
        (${db.runs.map(esc).join(", ")}). <a href="#" id="plan-show-db">Show them</a></div>` : ""}
      ${plan.too_large ? `<div class="warn" style="margin-top:6px">Too large (limit ${state.config.max_output_pixels / 1e6} Mpx). Shrink the box or use a coarser resolution.</div>` : ""}`;
    $("plan-show-db")?.addEventListener("click", (e) => { e.preventDefault(); setDbLayer(true); });
    planOk = !plan.too_large;
  } catch (err) {
    $("plan-info").innerHTML = `<span class="warn">${esc(err.message)}</span>`;
    planOk = false;
  }
  updateGenerateButton();
}

function updateGenerateButton() {
  const busy = state.activeJob && ["queued", "running"].includes(state.activeJob.status);
  $("btn-generate").disabled = !(planOk && currentBBox()) || busy;
}

["overlap"].forEach((id) => $(id).addEventListener("input", schedulePlan));
for (const id of ["overlap", "iou"]) {
  $(id).addEventListener("input", () => ($(`${id}-val`).textContent = parseFloat($(id).value).toFixed(2)));
}

// ------------------------------------------------------------- jobs
$("btn-generate").onclick = async () => {
  try {
    const job = await api("/api/jobs", { method: "POST", body: readParams() });
    await selectJob(job.id);
    refreshJobList();
  } catch (err) {
    toast(`Could not start: ${err.message}`, "error");
  }
};

$("btn-cancel").onclick = async () => {
  if (state.activeJob) await api(`/api/jobs/${state.activeJob.id}/cancel`, { method: "POST" });
};

async function selectJob(jobId) {
  clearInterval(state.pollTimer);
  setEditing(false);
  clearResults();
  const job = await api(`/api/jobs/${jobId}`);
  state.activeJob = job;
  history.replaceState(null, "", `?run=${encodeURIComponent(job.id)}`);
  renderJob(job);
  if (job.status === "queued" || job.status === "running") {
    state.pollTimer = setInterval(pollJob, 1000);
  } else if (job.status === "done") {
    await showResults(job, { fit: true });
  }
  highlightJobList();
  if (state.dbLayer) loadDbLayer();
}

async function pollJob() {
  const job = await api(`/api/jobs/${state.activeJob.id}`);
  state.activeJob = job;
  renderJob(job);
  if (!["queued", "running"].includes(job.status)) {
    clearInterval(state.pollTimer);
    refreshJobList();
    if (job.status === "done") await showResults(job, { fit: true });
  }
}

const STAGE_ORDER = ["imagery", "inference", "vectorize"];
function renderJob(job) {
  const active = job.status === "queued" || job.status === "running";
  $("progress").classList.toggle("hidden", !(active || job.status === "failed" || job.status === "cancelled"));
  $("btn-cancel").classList.toggle("hidden", !active);
  $("progress-fill").style.width = `${Math.round(job.progress * 100)}%`;
  $("progress-msg").textContent = job.message;
  $("progress-msg").className = `small ${job.status === "failed" ? "warn" : "muted"}`;
  const current = STAGE_ORDER.indexOf(job.stage);
  document.querySelectorAll(".stepper li").forEach((li, i) => {
    li.classList.toggle("done", i < current || job.status === "done");
    li.classList.toggle("active", active && i === current);
  });
  updateGenerateButton();
}

function clearResults() {
  if (state.fieldsLayer) map.removeLayer(state.fieldsLayer);
  if (state.imageryLayer) map.removeLayer(state.imageryLayer);
  state.fieldsLayer = state.imageryLayer = state.geojson = state.vertexLayer = state.hovered = null;
  state.layers.clear();
  state.selected.clear();
  $("results").classList.add("hidden");
  $("map-legend").classList.add("hidden");
}

async function showResults(job, { fit = false } = {}) {
  // Restore the run's bbox and parameters in the form so it can be tweaked/re-run.
  const [w, s, e, n] = job.params.bbox;
  setBBox(L.latLngBounds([s, w], [n, e]));
  $("pp-min-area").value = job.params.min_area_m2;
  $("pp-simplify").value = job.params.simplify_m;

  state.geojson = await api(`/api/jobs/${job.id}/fields`);

  if (state.imageryLayer) map.removeLayer(state.imageryLayer);
  state.imageryLayer = L.imageOverlay(`/api/jobs/${job.id}/preview.png?t=${Date.now()}`,
    job.result.mosaic.preview_bounds, { opacity: parseFloat($("imagery-opacity").value) });
  if ($("toggle-imagery").checked) state.imageryLayer.addTo(map);

  renderFields();
  renderJobActions();
  $("results").classList.remove("hidden");
  if (fit) fitView(L.latLngBounds([s, w], [n, e]));
}

function minConfidence() {
  return parseFloat($("confidence").value);
}

function visibleFeatures() {
  const c = minConfidence();
  return state.geojson ? state.geojson.features.filter((f) => f.properties.confidence >= c) : [];
}

// ---------------------------------------------------- field rendering
const fieldId = (layer) => layer.feature.properties.field_id;

function styleOf(layer) {
  if (state.selected.has(fieldId(layer))) return FIELD_SELECTED;
  return layer === state.hovered ? FIELD_HOVER : FIELD_STYLE;
}

// Only one field is highlighted at a time: the previous one is always reset,
// even when the browser skips its mouseout (e.g. when moving fast).
function setHover(layer) {
  const prev = state.hovered;
  state.hovered = layer;
  if (prev && prev !== layer) prev.setStyle(styleOf(prev));
  if (layer) layer.setStyle(styleOf(layer));
}

function renderFields() {
  if (!state.geojson) return;
  if (state.fieldsLayer) map.removeLayer(state.fieldsLayer);
  state.layers.clear();
  state.hovered = state.vertexLayer = null;
  const visible = visibleFeatures();
  const ids = new Set(visible.map((f) => f.properties.field_id));
  state.selected.forEach((id) => ids.has(id) || state.selected.delete(id));
  state.fieldsLayer = L.geoJSON({ type: "FeatureCollection", features: visible }, {
    style: (f) => (state.selected.has(f.properties.field_id) ? FIELD_SELECTED : FIELD_STYLE),
    onEachFeature: bindField,
  });
  if ($("toggle-fields").checked || state.editing) state.fieldsLayer.addTo(map);
  syncVertexEdit();
  renderStats();
  updateEditToolbar();
}
map.on("mouseout", () => setHover(null));

function renderStats() {
  const job = state.activeJob;
  const r = job.result;
  const visible = visibleFeatures();
  const total = state.geojson.features.length;
  const areaHa = visible.reduce((s, f) => s + f.properties.area_ha, 0);
  const meanHa = visible.length ? areaHa / visible.length : 0;
  const stat = (v, l) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`;
  const usedModel = modelSpec(job.params.model ?? "finetuned");
  $("result-name").textContent = job.name;
  $("result-stats").innerHTML =
    stat(fmt(visible.length), `fields shown of ${fmt(total)}`) +
    stat(`${fmt(areaHa, 1)} ha`, "total field area") +
    stat(`${fmt(meanHa, 2)} ha`, "mean field size") +
    stat(r.runtime_s ? `${Math.round(r.runtime_s)} s` : "–", `${job.params.resolution_m} m · ${r.mosaic.crs} · ${(r.device ?? "cpu").startsWith("cuda") ? "GPU" : "CPU"}`) +
    `<div class="stat stat-model"><span>Model</span><b>${esc(usedModel.name)}</b></div>` +
    (r.mosaic.missing_tiles ? `<div class="stat warn-box">${r.mosaic.missing_tiles} of ${r.mosaic.total_tiles} imagery tiles failed to download</div>` : "");

  $("map-legend").innerHTML = `<span class="legend-swatch"></span><b>${fmt(visible.length)}</b> fields · ${fmt(areaHa, 1)} ha
    <div class="muted" style="margin-top:2px">${esc(usedModel.name)} · confidence ≥ ${minConfidence().toFixed(2)}</div>
    ${state.dbLayer ? '<div class="muted" style="margin-top:4px"><span class="legend-swatch db"></span>saved fields of other runs</div>' : ""}`;
  $("map-legend").classList.remove("hidden");
}

function bindField(feature, layer) {
  state.layers.set(feature.properties.field_id, layer);
  layer.on("mouseover", () => setHover(layer));
  layer.on("mouseout", () => state.hovered === layer && setHover(null));
  layer.on("click", (e) => {
    if (state.drawKind) return;            // let the click reach the drawing tool
    L.DomEvent.stop(e);
    if (state.editing) selectField(fieldId(layer), e.originalEvent.shiftKey || e.originalEvent.ctrlKey || e.originalEvent.metaKey);
    else openFieldPopup(feature, e.latlng);
  });
}

const SOURCE_LABEL = { model: "Model", edited: "Edited", manual: "Drawn" };

function openFieldPopup(feature, latlng) {
  const p = feature.properties;
  const div = document.createElement("div");
  div.className = "popup";
  div.innerHTML = `
    <h4>Field #${p.field_id} <span class="src ${p.source}">${SOURCE_LABEL[p.source] ?? "Model"}</span></h4>
    <table>
      <tr><td>Area</td><td>${fmt(p.area_ha, 2)} ha</td></tr>
      <tr><td>Perimeter</td><td>${fmt(p.perimeter_m)} m</td></tr>
      <tr><td>Confidence</td><td>${p.confidence.toFixed(2)}
        <div class="conf-bar"><div style="width:${p.confidence * 100}%"></div></div></td></tr>
    </table>
    <div class="grid2 popup-actions">
      <button class="btn btn-outline btn-sm" data-act="edit">Edit shape</button>
      <button class="btn btn-ghost-danger btn-sm" data-act="delete">Delete</button>
    </div>`;
  div.querySelector("[data-act=edit]").onclick = () => {
    map.closePopup();
    setEditing(true);
    selectField(p.field_id, false);
  };
  div.querySelector("[data-act=delete]").onclick = () => {
    map.closePopup();
    editRequest("/fields/delete", { field_ids: [p.field_id] });
  };
  L.popup({ minWidth: 210 }).setLatLng(latlng).setContent(div).openOn(map);
}

let confFrame = null;
$("confidence").addEventListener("input", () => {
  $("confidence-val").textContent = minConfidence().toFixed(2);
  cancelAnimationFrame(confFrame);
  confFrame = requestAnimationFrame(renderFields);
});

$("toggle-fields").onchange = (e) => state.fieldsLayer && (e.target.checked ? state.fieldsLayer.addTo(map) : map.removeLayer(state.fieldsLayer));
$("toggle-imagery").onchange = (e) => {
  $("imagery-opacity").classList.toggle("hidden", !e.target.checked);
  if (!state.imageryLayer) return;
  if (e.target.checked) { state.imageryLayer.addTo(map); state.fieldsLayer?.bringToFront(); }
  else map.removeLayer(state.imageryLayer);
};
$("imagery-opacity").oninput = (e) => state.imageryLayer?.setOpacity(parseFloat(e.target.value));

$("btn-postprocess").onclick = async () => {
  const btn = $("btn-postprocess");
  btn.disabled = true; btn.textContent = "Applying…";
  try {
    const job = await api(`/api/jobs/${state.activeJob.id}/postprocess`, {
      method: "POST",
      body: { min_area_m2: parseFloat($("pp-min-area").value), simplify_m: parseFloat($("pp-simplify").value) },
    });
    state.activeJob = job;
    await showResults(job);
    refreshJobList();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Apply";
  }
};

$("btn-export").onclick = () => {
  const q = new URLSearchParams({
    format: $("export-format").value, crs: $("export-crs").value, min_confidence: minConfidence(),
  });
  window.location = `/api/jobs/${state.activeJob.id}/export?${q}`;
};

// ------------------------------------------------------------- editing
const avoidOverlap = () => $("et-avoid").checked;

function setEditing(on) {
  if (on === state.editing) return;
  if (on && !state.geojson) return;
  state.editing = on;
  cancelDraw();
  map.closePopup();
  if (!on) state.selected.clear();
  if (on && state.fieldsLayer && !map.hasLayer(state.fieldsLayer)) {
    $("toggle-fields").checked = true;
    state.fieldsLayer.addTo(map);
  }
  document.body.classList.toggle("editing", on);
  $("edit-toolbar").classList.toggle("hidden", !on);
  $("btn-edit").classList.toggle("active", on);
  $("btn-edit").querySelector("span").textContent = on ? "Stop editing" : "Edit polygons";
  syncBBoxEditing();
  refreshSelection();
}

function selectField(id, additive) {
  if (additive) state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  else state.selected = new Set([id]);
  refreshSelection();
}

function refreshSelection() {
  state.layers.forEach((layer) => layer.setStyle(styleOf(layer)));
  syncVertexEdit();
  updateEditToolbar();
}

map.on("click", () => {
  if (state.editing && !state.drawKind && state.selected.size) {
    state.selected.clear();
    refreshSelection();
  }
});

// Vertex editing is on for exactly one selected field.
function syncVertexEdit() {
  const only = state.selected.size === 1 ? state.layers.get([...state.selected][0]) : null;
  const target = state.editing && !state.drawKind ? only : null;
  const current = state.vertexLayer;
  if (current && current !== target) {
    current.off("pm:edit", onVertexEdit);
    if (current.pm.enabled()) current.pm.disable();
  }
  if (target && current !== target) {
    target.pm.enable({ ...SNAP, allowSelfIntersection: false, draggable: false });
    target.on("pm:edit", onVertexEdit);
    target.bringToFront();
  }
  state.vertexLayer = target;
}

function onVertexEdit(e) {
  const layer = e.layer ?? e.target;
  editRequest(`/fields/${fieldId(layer)}`, { geometry: layer.toGeoJSON().geometry, avoid_overlap: avoidOverlap() }, "PUT");
}

// Edits are sent one after another; the server answers with the changed
// fields, which are patched into the local copy. Resolves to true on success.
let editChain = Promise.resolve();
function editRequest(path, body, method = "POST") {
  const jobId = state.activeJob.id;
  editChain = editChain.then(async () => {
    $("edit-toolbar").classList.add("busy");
    try {
      applyEdit(await api(`/api/jobs/${jobId}${path}`, { method, body }));
      return true;
    } catch (err) {
      toast(err.message, "error");
      renderFields();   // put back the last saved shapes
      return false;
    } finally {
      $("edit-toolbar").classList.remove("busy");
    }
  });
  return editChain;
}

function applyEdit(res) {
  if (res.job.id !== state.activeJob?.id) return;
  state.activeJob = res.job;
  if (res.all) {
    state.geojson = res.all;
  } else {
    const changed = res.features.features;
    const drop = new Set([...res.removed, ...changed.map((f) => f.properties.field_id)]);
    state.geojson.features = state.geojson.features.filter((f) => !drop.has(f.properties.field_id)).concat(changed);
  }
  state.selected = new Set(res.select ?? []);
  renderFields();
  renderJobActions();
  scheduleJobListRefresh();
}

function updateEditToolbar() {
  if (!state.editing) return;
  const n = state.selected.size;
  const tb = $("edit-toolbar");
  tb.querySelector("[data-tool=draw]").classList.toggle("active", state.drawKind === "field");
  tb.querySelector("[data-tool=split]").classList.toggle("active", state.drawKind === "split");
  $("et-merge").disabled = n < 2;
  $("et-delete").disabled = n < 1;
  $("et-merge").lastChild.textContent = n >= 2 ? `Merge ${n}` : "Merge";
  $("et-delete").lastChild.textContent = n > 1 ? `Delete ${n}` : "Delete";
  $("et-undo").disabled = !(state.activeJob?.result.undo_depth > 0);
  $("et-hint").textContent =
    state.drawKind === "field" ? "Click to place vertices; click the first vertex to finish. Snaps to nearby fields. Esc cancels." :
    state.drawKind === "split" ? `Draw a line across ${n === 1 ? "the selected field" : "the fields to split"}; click the last point again to finish. Esc cancels.` :
    n === 0 ? "Click a field to select it. Shift+click to select several. Esc to stop editing." :
    n === 1 ? "Drag a vertex to reshape, drag a midpoint to add one, right-click a vertex to remove it. Shift+click more fields to merge." :
    `${n} fields selected: merge or delete them.`;
}

$("btn-edit").onclick = () => setEditing(!state.editing);
$("et-done").onclick = () => setEditing(false);
$("edit-toolbar").querySelector("[data-tool=draw]").onclick = () => (state.drawKind === "field" ? cancelDraw() : startDraw("field"));
$("edit-toolbar").querySelector("[data-tool=split]").onclick = () => (state.drawKind === "split" ? cancelDraw() : startDraw("split"));
$("et-merge").onclick = () => state.selected.size >= 2 && editRequest("/fields/merge", { field_ids: [...state.selected], avoid_overlap: avoidOverlap() });
$("et-delete").onclick = () => state.selected.size && editRequest("/fields/delete", { field_ids: [...state.selected] });
$("et-undo").onclick = () => editRequest("/fields/undo");
L.DomEvent.disableClickPropagation($("edit-toolbar"));

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea")) return;
  if (e.key === "Escape") {
    if (state.drawKind) cancelDraw();
    else if (state.editing && state.selected.size) { state.selected.clear(); refreshSelection(); }
    else if (state.editing) setEditing(false);
    return;
  }
  if (!state.editing) return;
  const key = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && key === "z") { e.preventDefault(); $("et-undo").click(); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); $("et-delete").click(); }
  else if (e.ctrlKey || e.metaKey || e.altKey) return;
  else if (key === "n") startDraw("field");
  else if (key === "s") startDraw("split");
  else if (key === "m") $("et-merge").click();
});

// ------------------------------------------------- database & QGIS (run)
function renderJobActions() {
  const job = state.activeJob;
  if (!job) return;
  const saved = job.in_database;
  const stale = saved && job.result.edited && job.result.edited > saved;
  const btn = $("btn-db-save");
  btn.classList.toggle("saved", !!saved && !stale);
  btn.classList.toggle("attention", !!stale);
  btn.querySelector("span").textContent = !saved ? "Save to database" : stale ? "Update in database (edited since save)" : "Saved in database ✓";
  btn.title = saved ? `Last saved ${fmtDate(saved)}. Click to save again with the current fields and confidence.` : "";
  const edits = job.result.edited;
  $("edited-note").classList.toggle("hidden", !edits);
  $("edited-note").textContent = edits ? `edited ${fmtDate(edits)}` : "";
  $("qgis-import").classList.toggle("hidden", !job.result.qgis_file);
}

$("btn-db-save").onclick = async () => {
  const btn = $("btn-db-save");
  btn.disabled = true;
  try {
    const res = await api(`/api/jobs/${state.activeJob.id}/database?min_confidence=${minConfidence()}`, { method: "POST" });
    state.activeJob = res.job;
    renderJobActions();
    const o = res.overlap;
    toast(`Saved ${fmt(res.entry.field_count)} fields to the database.` +
      (o.fields ? ` ${fmt(o.fields)} of them overlap saved fields of: ${o.runs.join(", ")}.` : ""), o.fields ? "warn" : "ok", o.fields ? 8000 : 4500);
    refreshJobList();
    refreshDatabase();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
};

$("btn-qgis").onclick = async () => {
  try {
    const job = await api(`/api/jobs/${state.activeJob.id}/qgis/open?min_confidence=${minConfidence()}`, { method: "POST" });
    state.activeJob = job;
    renderJobActions();
    toast("Opening QGIS with the field boundaries on Esri World Imagery. Save your edits there, then click Load edits.", "ok", 7000);
  } catch (err) {
    toast(err.message, "error");
  }
};

$("btn-qgis-import").onclick = async () => {
  if (!confirm("Replace the fields of this run with the layer saved in QGIS? You can undo this in the edit toolbar.")) return;
  if (await editRequest("/qgis/import")) toast("Loaded the fields from QGIS.", "ok");
};

// ------------------------------------------------------------- history
let jobListTimer = null;
function scheduleJobListRefresh() {
  clearTimeout(jobListTimer);
  jobListTimer = setTimeout(refreshJobList, 800);
}

async function refreshJobList() {
  const jobs = (state.jobs = await api("/api/jobs"));
  $("job-list").innerHTML = jobs.length ? "" : '<li class="muted small empty">No runs yet</li>';
  $("btn-delete-all").classList.toggle("hidden", jobs.length < 2);
  for (const job of jobs) {
    const li = document.createElement("li");
    li.dataset.id = job.id;
    li.title = job.message;
    const short = modelSpec(job.params.model ?? "finetuned").short;
    const info = job.status === "done"
      ? `${fmt(job.result.field_count)} fields · ${job.params.resolution_m} m · ${short}` : `${job.status} · ${short}`;
    li.innerHTML = `<span class="dot ${job.status}"></span>
      <div class="meta"><div class="name-row"><span class="name"></span>
        ${job.in_database ? '<span class="db-badge" title="Saved in the field database">DB</span>' : ""}</div>
        <div class="sub">${fmtDate(job.created)} · ${info}</div></div>
      <button class="del" title="Delete run">✕</button>`;
    li.querySelector(".name").textContent = job.name;
    li.onclick = () => selectJob(job.id);
    li.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      const keep = job.in_database ? " Its fields saved in the database are kept." : "";
      if (!confirm(`Delete run "${job.name}" and its files?${keep}`)) return;
      try {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        if (state.activeJob?.id === job.id) forgetActiveJob();
        refreshJobList();
      } catch (err) { toast(err.message, "error"); }
    };
    $("job-list").appendChild(li);
  }
  highlightJobList();
  renderDatabaseList();
}

function forgetActiveJob() {
  setEditing(false);
  clearResults();
  state.activeJob = null;
  $("progress").classList.add("hidden");
  history.replaceState(null, "", "/");
}

$("btn-delete-all").onclick = async () => {
  const n = state.jobs.length;
  if (!confirm(`Delete all ${n} runs and their files (imagery and model output)? This cannot be undone.\n\nFields saved in the field database are kept.`)) return;
  try {
    const res = await api("/api/jobs", { method: "DELETE" });
    if (state.activeJob && state.activeJob.status !== "running") forgetActiveJob();
    toast(`Deleted ${res.deleted} runs.` + (res.skipped ? ` ${res.skipped} running run was kept.` : ""), "ok");
    refreshJobList();
  } catch (err) { toast(err.message, "error"); }
};

function highlightJobList() {
  document.querySelectorAll("#job-list li").forEach((li) =>
    li.classList.toggle("active", li.dataset.id === state.activeJob?.id));
}

// ------------------------------------------------------------ database
async function refreshDatabase() {
  state.database = await api("/api/database");
  renderDatabaseList();
  if (state.dbLayer) loadDbLayer();
}

function renderDatabaseList() {
  const db = state.database;
  if (!db) return;
  $("db-summary").textContent = db.run_count ? `${fmt(db.field_count)} fields · ${fmt(db.total_area_ha)} ha` : "";
  $("db-export").classList.toggle("hidden", !db.run_count);
  const list = $("db-list");
  list.innerHTML = db.run_count ? "" : '<li class="muted small empty">Nothing saved yet. Use <i>Save to database</i> under Results.</li>';
  const existing = new Set(state.jobs.map((j) => j.id));
  for (const run of db.runs) {
    const li = document.createElement("li");
    li.title = "Zoom to these fields";
    li.innerHTML = `<span class="dot db"></span>
      <div class="meta"><div class="name"></div>
        <div class="sub">${fmt(run.field_count)} fields · ${fmt(run.total_area_ha, 1)} ha · saved ${fmtDate(run.saved)}</div></div>
      ${existing.has(run.run_id) ? '<button class="open" title="Open this run">↗</button>' : ""}
      <button class="del" title="Remove from the database">✕</button>`;
    li.querySelector(".name").textContent = run.name;
    li.onclick = () => {
      const [w, s, e, n] = run.bbox;
      setDbLayer(true);
      fitView(L.latLngBounds([s, w], [n, e]));
    };
    li.querySelector(".open")?.addEventListener("click", (e) => { e.stopPropagation(); selectJob(run.run_id); });
    li.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Remove the ${fmt(run.field_count)} saved fields of "${run.name}" from the database? The run itself is not deleted.`)) return;
      try {
        await api(`/api/database/${encodeURIComponent(run.run_id)}`, { method: "DELETE" });
        if (state.activeJob?.id === run.run_id) {
          state.activeJob.in_database = null;
          renderJobActions();
        }
        refreshDatabase();
        refreshJobList();
      } catch (err) { toast(err.message, "error"); }
    };
    list.appendChild(li);
  }
}

function setDbLayer(on) {
  $("toggle-db").checked = on;
  if (!on) {
    if (state.dbLayer) map.removeLayer(state.dbLayer);
    state.dbLayer = null;
  } else if (!state.dbLayer) {
    state.dbLayer = L.geoJSON(null, { style: DB_STYLE, pmIgnore: true, snapIgnore: true, onEachFeature: bindDbField }).addTo(map);
    loadDbLayer();
  }
  if (state.geojson) renderStats();
}
$("toggle-db").onchange = (e) => setDbLayer(e.target.checked);

let dbRequest = 0, dbTruncatedWarned = false;
async function loadDbLayer() {
  if (!state.dbLayer) return;
  const b = map.getBounds();
  const q = new URLSearchParams({
    bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map((v) => v.toFixed(5)).join(","),
    exclude: state.activeJob?.id ?? "",
  });
  const req = ++dbRequest;
  try {
    const fc = await api(`/api/database/fields?${q}`);
    if (req !== dbRequest || !state.dbLayer) return;
    state.dbLayer.clearLayers();
    state.dbLayer.addData(fc);
    if (fc.truncated && !dbTruncatedWarned) {
      toast("Showing the 5,000 largest saved fields in view. Zoom in to see all.", "info");
      dbTruncatedWarned = true;
    }
  } catch (err) { toast(err.message, "error"); }
}
let dbMoveTimer = null;
map.on("moveend", () => { clearTimeout(dbMoveTimer); dbMoveTimer = setTimeout(loadDbLayer, 250); });

function bindDbField(feature, layer) {
  layer.on("click", (e) => {
    if (state.drawKind || state.editing) return;
    L.DomEvent.stop(e);
    const p = feature.properties;
    const exists = state.jobs.some((j) => j.id === p.run_id);
    const div = document.createElement("div");
    div.className = "popup";
    div.innerHTML = `
      <h4>Saved field <span class="src db">Database</span></h4>
      <table>
        <tr><td>Run</td><td class="name"></td></tr>
        <tr><td>Area</td><td>${fmt(p.area_ha, 2)} ha</td></tr>
        <tr><td>Confidence</td><td>${p.confidence.toFixed(2)}</td></tr>
        <tr><td>Saved</td><td>${fmtDate(p.saved)}</td></tr>
      </table>
      ${exists ? '<button class="btn btn-outline btn-sm btn-block">Open this run</button>' : ""}`;
    div.querySelector(".name").textContent = p.run_name;
    div.querySelector("button")?.addEventListener("click", () => { map.closePopup(); selectJob(p.run_id); });
    L.popup({ minWidth: 210 }).setLatLng(e.latlng).setContent(div).openOn(map);
  });
}

$("btn-db-export").onclick = () => {
  const q = new URLSearchParams({ format: $("db-format").value, crs: $("db-crs").value });
  window.location = `/api/database/export?${q}`;
};
$("btn-db-qgis").onclick = async () => {
  try {
    await api("/api/database/qgis", { method: "POST" });
    toast("Opening all saved fields in QGIS.", "ok");
  } catch (err) { toast(err.message, "error"); }
};

function renderQgisButtons() {
  const q = state.system.qgis;
  for (const id of ["btn-qgis", "btn-db-qgis"]) {
    $(id).disabled = !q.available;
    if (!q.available) $(id).title = "QGIS was not found on this computer. Install QGIS, or set DELTOOL_QGIS to its executable.";
  }
}

// ------------------------------------------------------------- init
async function init() {
  const cfg = (state.config = await api("/api/config"));
  const d = cfg.defaults;

  state.resolution = d.resolution_m;
  renderResolutionPicker(cfg.resolutions, d.resolution_m);
  const formats = Object.entries(cfg.export_formats).map(([k, label]) => `<option value="${k}">${label}</option>`).join("");
  $("export-format").innerHTML = $("db-format").innerHTML = formats;

  for (const key of ["confidence", "min_area_m2", "simplify_m", "overlap", "iou", "batch_size"]) $(key).value = d[key];
  $("confidence-val").textContent = d.confidence.toFixed(2);
  $("overlap-val").textContent = d.overlap.toFixed(2);
  $("iou-val").textContent = d.iou.toFixed(2);
  document.querySelectorAll("input[type=range]").forEach(paintRange);

  await refreshJobList();
  await refreshDatabase();

  await window.appReady;
  state.system = await api("/api/system");
  state.model = state.system.default_model;
  renderModelOptions();
  renderDevicePicker();
  renderQgisButtons();
  await refreshJobList();

  const linked = new URLSearchParams(location.search).get("run");
  const running = state.jobs.find((j) => j.status === "running" || j.status === "queued");
  if (linked) selectJob(linked).catch(() => history.replaceState(null, "", "/"));
  else if (running) selectJob(running.id);
}

init();
