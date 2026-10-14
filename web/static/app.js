/* DelTool frontend: bbox selection, job submission/polling, results display & export. */
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

const state = {
  config: null,
  system: null,
  model: "finetuned",
  deviceChoice: "auto",
  resolution: 1,
  bboxLayer: null,
  activeJob: null,
  geojson: null,
  fieldsLayer: null,
  imageryLayer: null,
  pollTimer: null,
};

// ------------------------------------------------------------------ map
const map = L.map("map", { zoomControl: false }).setView([9.9, -0.9], 11);
L.control.zoom({ position: "topright" }).addTo(map);

const esriImagery = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Basemap © Esri, Maxar, Earthstar Geographics" }
).addTo(map);
const esriLabels = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19 }
);
L.control.layers({ "Esri World Imagery": esriImagery }, { "Place labels": esriLabels }, { position: "topright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);

const CYAN = "#00A6D6", YELLOW = "#FFB81C";
const BBOX_STYLE = { color: "#ffffff", weight: 2, fill: false, dashArray: "8 6" };
const FIELD_STYLE = { color: CYAN, weight: 1.8, fillColor: CYAN, fillOpacity: 0.14 };
const FIELD_HOVER = { color: YELLOW, weight: 3, fillColor: YELLOW, fillOpacity: 0.25 };

// ------------------------------------------------------------ tooltips
const tip = $("tooltip");
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest?.(".info");
  if (!t) return;
  tip.textContent = t.dataset.tip;
  const r = t.getBoundingClientRect();
  tip.classList.add("show");
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 8) top = r.top - h - 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
});
document.addEventListener("mouseout", (e) => e.target.closest?.(".info") && tip.classList.remove("show"));

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
    btn.innerHTML = `
      <div class="mo-head"><span class="mo-name"></span>
        <span class="mo-badge ${m.recommended ? "rec" : "alt"}">${m.recommended ? "Recommended" : "Large fields"}</span></div>
      <div class="mo-desc"></div>`;
    btn.querySelector(".mo-name").textContent = m.name;
    btn.querySelector(".mo-desc").textContent = m.available ? m.description : "Weights file not found in DelAny/.";
    btn.onclick = () => selectModel(key);
    btn.dataset.key = key;
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
  const options = [
    ["auto", "Auto"],
    ["cuda", "GPU"],
    ["cpu", "CPU"],
  ];
  $("device").innerHTML = options.map(([k, l]) => `<button type="button" data-dev="${k}">${l}</button>`).join("");
  $("device").querySelectorAll("button").forEach((b) => {
    b.disabled = b.dataset.dev === "cuda" && !d.cuda_available;
    if (b.disabled) b.title = d.hint;
    b.onclick = () => selectDevice(b.dataset.dev);
  });
  const hint = $("device-hint");
  hint.classList.remove("hidden");
  hint.classList.toggle("ok", d.cuda_available);
  hint.textContent = d.cuda_available ? `GPU available: ${d.gpu_name}` : d.hint;
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

// ------------------------------------------------------------- bbox
const drawer = new L.Draw.Rectangle(map, { shapeOptions: BBOX_STYLE, showArea: false });

$("btn-draw").onclick = () => { drawer.enable(); $("btn-draw").classList.add("active"); };
map.on("draw:drawstop", () => $("btn-draw").classList.remove("active"));
$("btn-clear-bbox").onclick = () => setBBox(null);
map.on(L.Draw.Event.CREATED, (e) => setBBox(e.layer.getBounds()));

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
  state.bboxLayer.editing.enable();
  state.bboxLayer.on("edit", () => { writeBBoxInputs(state.bboxLayer.getBounds()); schedulePlan(); });
  writeBBoxInputs(bounds);
  if (fit) map.fitBounds(bounds, { padding: [40, 40] });
  schedulePlan();
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
    $("plan-info").innerHTML = `
      <div class="kv">
        <span>Area</span><span><b>${fmt(plan.area_km2, 2)} km²</b> (${fmt(plan.area_km2 * 100)} ha)</span>
        <span>Raster</span><span>${fmt(plan.width_px)} × ${fmt(plan.height_px)} px</span>
        <span>Projection</span><span>${plan.crs} · zoom ${plan.zoom}</span>
        <span>Model tiles</span><span>${fmt(plan.model_tiles)} (${fmt(plan.download_tiles)} imagery tiles)</span>
        <span>Estimated time</span><span>~${eta} on ${gpu ? "GPU" : "CPU"}</span>
      </div>
      ${plan.too_large ? `<div class="warn" style="margin-top:6px">Too large (limit ${state.config.max_output_pixels / 1e6} Mpx). Shrink the box or use a coarser resolution.</div>` : ""}`;
    planOk = !plan.too_large;
  } catch (err) {
    $("plan-info").innerHTML = `<span class="warn">${err.message}</span>`;
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
    alert(`Could not start: ${err.message}`);
  }
};

$("btn-cancel").onclick = async () => {
  if (state.activeJob) await api(`/api/jobs/${state.activeJob.id}/cancel`, { method: "POST" });
};

async function selectJob(jobId) {
  clearInterval(state.pollTimer);
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
  state.fieldsLayer = state.imageryLayer = state.geojson = null;
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
  $("results").classList.remove("hidden");
  if (fit) map.fitBounds(L.latLngBounds([s, w], [n, e]), { padding: [30, 30], paddingTopLeft: [400, 30] });
}

function minConfidence() {
  return parseFloat($("confidence").value);
}

function visibleFeatures() {
  const c = minConfidence();
  return state.geojson ? state.geojson.features.filter((f) => f.properties.confidence >= c) : [];
}

function renderFields() {
  if (!state.geojson) return;
  if (state.fieldsLayer) map.removeLayer(state.fieldsLayer);
  state.fieldsLayer = L.geoJSON({ type: "FeatureCollection", features: visibleFeatures() },
    { style: FIELD_STYLE, onEachFeature: bindField });
  if ($("toggle-fields").checked) state.fieldsLayer.addTo(map);
  renderStats();
}

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
    `<div class="stat stat-model"><span>Model</span><b>${usedModel.name}</b></div>` +
    (r.mosaic.missing_tiles ? `<div class="stat warn-box">${r.mosaic.missing_tiles} of ${r.mosaic.total_tiles} imagery tiles failed to download</div>` : "");

  $("map-legend").innerHTML = `<span class="legend-swatch"></span><b>${fmt(visible.length)}</b> fields · ${fmt(areaHa, 1)} ha
    <div class="muted" style="margin-top:2px">${usedModel.name} · confidence ≥ ${minConfidence().toFixed(2)}</div>`;
  $("map-legend").classList.remove("hidden");
}

function bindField(feature, layer) {
  const p = feature.properties;
  layer.on("mouseover", () => { layer.setStyle(FIELD_HOVER); layer.bringToFront(); });
  layer.on("mouseout", () => state.fieldsLayer.resetStyle(layer));
  layer.bindPopup(() => {
    const div = document.createElement("div");
    div.className = "popup";
    div.innerHTML = `
      <h4>Field #${p.field_id}</h4>
      <table>
        <tr><td>Area</td><td>${fmt(p.area_ha, 2)} ha</td></tr>
        <tr><td>Perimeter</td><td>${fmt(p.perimeter_m)} m</td></tr>
        <tr><td>Confidence</td><td>${p.confidence.toFixed(2)}
          <div class="conf-bar"><div style="width:${p.confidence * 100}%"></div></div></td></tr>
      </table>
      <button class="btn btn-ghost-danger btn-sm btn-block">Delete field</button>`;
    div.querySelector("button").onclick = async () => {
      const job = await api(`/api/jobs/${state.activeJob.id}/fields/${p.field_id}`, { method: "DELETE" });
      state.activeJob = job;
      state.geojson.features = state.geojson.features.filter((f) => f.properties.field_id !== p.field_id);
      map.closePopup();
      renderFields();
      refreshJobList();
    };
    return div;
  }, { minWidth: 200 });
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
    alert(err.message);
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
$("btn-imagery").onclick = () => (window.location = `/api/jobs/${state.activeJob.id}/imagery`);

// ------------------------------------------------------------- history
async function refreshJobList() {
  const jobs = await api("/api/jobs");
  $("job-list").innerHTML = jobs.length ? "" : '<li class="muted small">No runs yet</li>';
  for (const job of jobs) {
    const li = document.createElement("li");
    li.dataset.id = job.id;
    li.title = job.message;
    const date = new Date(job.created).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    const short = modelSpec(job.params.model ?? "finetuned").short;
    const info = job.status === "done"
      ? `${fmt(job.result.field_count)} fields · ${job.params.resolution_m} m · ${short}` : `${job.status} · ${short}`;
    li.innerHTML = `<span class="dot ${job.status}"></span>
      <div class="meta"><div class="name"></div><div class="sub">${date} · ${info}</div></div>
      <button class="del" title="Delete run">✕</button>`;
    li.querySelector(".name").textContent = job.name;
    li.onclick = () => selectJob(job.id);
    li.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete run "${job.name}" and its files?`)) return;
      try {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        if (state.activeJob?.id === job.id) {
          clearResults(); state.activeJob = null; $("progress").classList.add("hidden");
        }
        refreshJobList();
      } catch (err) { alert(err.message); }
    };
    $("job-list").appendChild(li);
  }
  highlightJobList();
}

function highlightJobList() {
  document.querySelectorAll("#job-list li").forEach((li) =>
    li.classList.toggle("active", li.dataset.id === state.activeJob?.id));
}

// ------------------------------------------------------------- init
async function init() {
  const cfg = (state.config = await api("/api/config"));
  const d = cfg.defaults;

  state.resolution = d.resolution_m;
  renderResolutionPicker(cfg.resolutions, d.resolution_m);
  $("export-format").innerHTML = Object.entries(cfg.export_formats)
    .map(([k, label]) => `<option value="${k}">${label}</option>`).join("");

  for (const key of ["confidence", "min_area_m2", "simplify_m", "overlap", "iou", "batch_size"]) $(key).value = d[key];
  $("confidence-val").textContent = d.confidence.toFixed(2);
  $("overlap-val").textContent = d.overlap.toFixed(2);
  $("iou-val").textContent = d.iou.toFixed(2);
  document.querySelectorAll("input[type=range]").forEach(paintRange);

  await refreshJobList();

  await window.appReady;
  state.system = await api("/api/system");
  state.model = state.system.default_model;
  renderModelOptions();
  renderDevicePicker();
  await refreshJobList();

  const linked = new URLSearchParams(location.search).get("run");
  const running = (await api("/api/jobs")).find((j) => j.status === "running" || j.status === "queued");
  if (linked) selectJob(linked).catch(() => history.replaceState(null, "", "/"));
  else if (running) selectJob(running.id);
}

init();
