/* DelTool frontend: bbox selection, job submission/polling, results display,
 * polygon editing & export. */
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
const fmtDay = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
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
  // imagery source
  imagerySource: "google",
  wayback: { key: null, result: null, selected: null, showAll: false },
  runSelection: new Set(),  // runs ticked in the list, for exporting together
  waybackLayer: null,
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
map.createPane("wayback").style.zIndex = 250;    // dated imagery preview above the basemap
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
}
$("btn-labels").onclick = (e) => { e.preventDefault(); setLabels(!map.hasLayer(esriLabels)); };
setLabels(true);   // place names and borders are on whenever the page opens

const CYAN = "#00A6D6", YELLOW = "#FFB81C", GREEN = "#6CC24A";
const BBOX_STYLE = { color: "#ffffff", weight: 2, fill: false, dashArray: "8 6", interactive: false, snapIgnore: true };
const FIELD_STYLE = { color: CYAN, weight: 1.8, fillColor: CYAN, fillOpacity: 0.14 };
const FIELD_HOVER = { color: YELLOW, weight: 2.6, fillColor: YELLOW, fillOpacity: 0.22 };
const FIELD_SELECTED = { color: YELLOW, weight: 3, fillColor: YELLOW, fillOpacity: 0.32 };
const DRAW_STYLE = { color: YELLOW, weight: 2.5, fillColor: YELLOW, fillOpacity: 0.15 };
const snapOpts = () => ({ snappable: $("et-snap").checked, snapDistance: 14 });

// ---------------------------------------------------------------- tabs
// The panel is split into steps: 1 Area, 2 Imagery, 3 Run, 4 Results.
function showTab(name) {
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
    b.setAttribute("aria-selected", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === name));
  document.querySelector("#panel .scroll").scrollTop = 0;
  if (name !== "results") setEditing(false);
  syncResultLayers();
  try { localStorage.setItem("deltool-tab", name); } catch (_) {}
}
document.querySelectorAll(".tabs [data-tab]").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const link = e.target.closest?.("[data-goto]");
  if (link) { e.preventDefault(); showTab(link.dataset.goto); }
});

// Small markers on the tabs: done, needs attention, running.
function updateTabStates() {
  const set = (id, cls, title) => { $(id).className = `tab-state ${cls}`; $(id).title = title; };
  set("tab-state-area", currentBBox() ? "done" : "", currentBBox() ? "Box drawn" : "No box yet");
  const imageryOk = state.imagerySource === "google" || state.wayback.selected;
  set("tab-state-imagery", imageryOk ? "done" : "todo", imageryOk ? "Imagery chosen" : "Pick an imagery date");
  const running = state.jobs.some((jb) => ["running", "queued"].includes(jb.status));
  set("tab-state-run", running ? "busy" : "", running ? "A run is in progress" : "");
  $("tab-runs-count").textContent = state.jobs.length || "";
}

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
  stopSmooth();
  map.pm.disableDraw();
  state.drawKind = state.createKind = kind;
  if (kind === "bbox") {
    const hint = { ...BBOX_STYLE, dashArray: "4 6" };
    map.pm.enableDraw("Rectangle", { pathOptions: BBOX_STYLE, templineStyle: BBOX_STYLE, hintlineStyle: hint, snappable: false });
  } else {
    const shape = kind === "field" ? "Polygon" : "Line";
    map.pm.enableDraw(shape, { ...snapOpts(), allowSelfIntersection: false, pathOptions: DRAW_STYLE,
      templineStyle: DRAW_STYLE, hintlineStyle: { ...DRAW_STYLE, dashArray: "5 5" } });
  }
  syncVertexEdit();
  updateEditToolbar();
  $("btn-draw").classList.toggle("active", kind === "bbox");
}

function cancelDraw() {
  if (state.drawKind === "smooth") stopSmooth();
  else if (state.drawKind) map.pm.disableDraw();
}

map.on("pm:drawend", () => {
  state.drawKind = null;
  $("btn-draw").classList.remove("active");
  syncVertexEdit();
  updateEditToolbar();
});

// A split line is just two points: finish it as soon as the second one is placed.
map.on("pm:vertexadded", (e) => {
  if (state.drawKind !== "split" || (e.workingLayer?.getLatLngs().length ?? 0) < 2) return;
  const draw = map.pm.Draw.Line;
  setTimeout(() => state.drawKind === "split" && draw?._finishShape?.({}), 0);
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

// ------------------------------------------------------------- smooth brush
// Hold the left button and brush over an outline: every vertex under the
// brush is removed, so its neighbours are joined by a straight edge.
const SMOOTH_RADIUS_PX = 12;
const SMOOTH_STYLE = { color: YELLOW, weight: 2 * SMOOTH_RADIUS_PX, opacity: 0.3, lineCap: "round", lineJoin: "round",
  interactive: false, pmIgnore: true, snapIgnore: true };
const smoothBrush = L.circleMarker([0, 0], { radius: SMOOTH_RADIUS_PX, color: "#fff", weight: 1.5, fill: false,
  interactive: false, pmIgnore: true, snapIgnore: true });
let smoothStroke = null;   // { lls, line } while the button is held

function startSmooth() {
  map.pm.disableDraw();
  state.drawKind = "smooth";
  map.dragging.disable();
  map.getContainer().classList.add("smoothing");
  syncVertexEdit();
  updateEditToolbar();
}

function stopSmooth() {
  if (state.drawKind !== "smooth") return;
  state.drawKind = null;
  if (smoothStroke) map.removeLayer(smoothStroke.line);
  smoothStroke = null;
  smoothBrush.remove();
  map.dragging.enable();
  map.getContainer().classList.remove("smoothing");
  syncVertexEdit();
  updateEditToolbar();
}

{
  const el = map.getContainer();
  let pan = null;   // last pointer position while panning with the right button
  el.addEventListener("contextmenu", (e) => state.drawKind === "smooth" && e.preventDefault());
  el.addEventListener("pointerdown", (e) => {
    if (state.drawKind !== "smooth" || e.target.closest(".leaflet-control")) return;
    if (e.button === 2) {
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      pan = L.point(e.clientX, e.clientY);
      el.classList.add("panning");
      return;
    }
    if (e.button !== 0) return;
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    const ll = map.mouseEventToLatLng(e);
    smoothStroke = { lls: [ll], line: L.polyline([ll], SMOOTH_STYLE).addTo(map) };
  });
  el.addEventListener("pointermove", (e) => {
    if (state.drawKind !== "smooth") return;
    if (pan) {
      const p = L.point(e.clientX, e.clientY);
      map.panBy(pan.subtract(p), { animate: false });
      pan = p;
    }
    const ll = map.mouseEventToLatLng(e);
    smoothBrush.setLatLng(ll);
    if (!map.hasLayer(smoothBrush)) smoothBrush.addTo(map);
    if (smoothStroke) { smoothStroke.lls.push(ll); smoothStroke.line.addLatLng(ll); }
  });
  el.addEventListener("pointerleave", () => smoothBrush.remove());
  const end = () => {
    if (pan) { pan = null; el.classList.remove("panning"); }
    if (!smoothStroke) return;
    map.removeLayer(smoothStroke.line);
    const lls = smoothStroke.lls;
    smoothStroke = null;
    applySmoothStroke(lls);
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

function applySmoothStroke(lls) {
  if (!$("et-show-fields").checked) return;   // don't change fields you can't see
  const R = SMOOTH_RADIUS_PX;
  const pts = lls.map((ll) => map.latLngToContainerPoint(ll));
  const sb = L.bounds(pts);
  const reach = L.bounds(sb.min.subtract([R, R]), sb.max.add([R, R]));
  const near = ([lng, lat]) => {
    const p = map.latLngToContainerPoint([lat, lng]);
    return pts.length === 1 ? p.distanceTo(pts[0]) <= R
      : pts.some((a, i) => i > 0 && L.LineUtil.pointToSegmentDistance(p, pts[i - 1], a) <= R);
  };
  // A ring that drops below three vertices disappears; for the outer ring
  // that means the whole polygon, which the brush never does.
  const thinRing = (ring) => {
    const open = ring.slice(0, -1);
    const kept = open.filter((c) => !near(c));
    return kept.length >= 3 ? [...kept, kept[0]] : null;
  };
  const thinPolygon = ([outer, ...holes]) => {
    const o = thinRing(outer);
    return o && [o, ...holes.map(thinRing).filter(Boolean)];
  };
  let tooMuch = false;
  state.layers.forEach((layer, id) => {
    const lb = layer.getBounds();
    const box = L.bounds(map.latLngToContainerPoint(lb.getNorthWest()), map.latLngToContainerPoint(lb.getSouthEast()));
    if (!box.intersects(reach)) return;
    const geom = layer.toGeoJSON().geometry;
    const before = JSON.stringify(geom.coordinates);
    let coords;
    if (geom.type === "Polygon") coords = thinPolygon(geom.coordinates);
    else coords = geom.coordinates.map(thinPolygon).filter(Boolean);
    if (!coords || !coords.length) { tooMuch = true; return; }
    if (JSON.stringify(coords) === before) return;
    editRequest(`/fields/${id}`, { geometry: { type: geom.type, coordinates: coords }, avoid_overlap: avoidOverlap() }, "PUT");
  });
  if (tooMuch) toast("That stroke would remove the whole field; brush over fewer vertices.", "error");
}

// ------------------------------------------------------------- bbox
$("btn-draw").onclick = () => { setEditing(false); startDraw("bbox"); };
$("btn-clear-bbox").onclick = () => setBBox(null);

function setBBox(bounds, { fit = false } = {}) {
  if (state.bboxLayer) map.removeLayer(state.bboxLayer);
  state.bboxLayer = null;
  if (!bounds) {
    ["w", "s", "e", "n"].forEach((k) => ($(`bbox-${k}`).value = ""));
    $("plan-info").innerHTML = "Draw a box on the map to start.";
    state.wayback.key = null;
    clearWaybackResult();
    renderWaybackStatus({ status: "idle", message: "" });
    $("wayback-status").textContent = "Draw a box to see the available dates.";
    updateGenerateButton();
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
// The chosen resolution, for both sources. A Wayback date can't be finer than
// its real detail (the capture resolution, but no finer than the deepest zoom
// Esri serves it at).
function waybackResolution(c) {
  return c ? Math.round(Math.max(c.resolution_m || 0.5, c.max_zoom_res_m) * 100) / 100 : 0;
}

function chosenResolution() {
  const custom = parseFloat($("resolution-custom").value);
  return custom > 0 ? custom : state.resolution;
}

function resolution() {
  const res = chosenResolution();
  return state.imagerySource === "wayback" ? Math.max(res, waybackResolution(state.wayback.selected)) : res;
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

function imageryParams() {
  const c = state.imagerySource === "wayback" ? state.wayback.selected : null;
  return {
    source: state.imagerySource,
    wayback_release: c?.release ?? null,
    max_zoom: c?.max_zoom ?? null,
    imagery_date: c?.capture_date ?? "",
    imagery_label: c ? `${c.satellite}, ${c.resolution_m} m, captured ${c.capture_date} (Esri Wayback)` : "",
  };
}

function readParams() {
  return {
    bbox: currentBBox(),
    resolution_m: resolution(),
    ...imageryParams(),
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
    lookupWayback();
    const plan = await api(`/api/plan?overlap=${p.overlap}`, {
      method: "POST", body: { bbox: p.bbox, resolution_m: p.resolution_m, source: p.source,
        wayback_release: p.wayback_release, max_zoom: p.max_zoom },
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
    if (state.wayback.result && state.imagerySource === "wayback") renderWaybackList();   // resolution note
  } catch (err) {
    $("plan-info").innerHTML = `<span class="warn">${esc(err.message)}</span>`;
    planOk = false;
  }
  updateGenerateButton();
}

function updateGenerateButton() {
  const busy = state.activeJob && ["queued", "running"].includes(state.activeJob.status);
  const needsDate = state.imagerySource === "wayback" && !state.wayback.selected;
  const reason = !currentBBox() ? 'Draw a box first, under <a href="#" data-goto="area">1 · Area</a>.'
    : needsDate ? 'Pick an imagery date first, under <a href="#" data-goto="imagery">2 · Imagery</a>.'
    : busy ? "A run is in progress."
    : !planOk ? 'The box is too large for this resolution; see <a href="#" data-goto="area">1 · Area</a>.' : "";
  $("btn-generate").disabled = !!reason;
  $("generate-hint").innerHTML = reason;
  $("generate-hint").classList.toggle("hidden", !reason);
  updateTabStates();
}

// ------------------------------------------------------- imagery source
const WAYBACK_TILES = "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{release}/{z}/{y}/{x}";
const WAYBACK_SHOWN = 5;

function setImagerySource(src) {
  state.imagerySource = src;
  $("imagery-source").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.src === src);
    b.setAttribute("aria-checked", b.dataset.src === src);
  });
  $("wayback-panel").classList.toggle("hidden", src !== "wayback");
  try { localStorage.setItem("deltool-source", src); } catch (_) {}
  updateWaybackLayer();
  if (currentBBox()) schedulePlan();
  updateGenerateButton();
}
$("imagery-source").querySelectorAll("button").forEach((b) => (b.onclick = () => setImagerySource(b.dataset.src)));

// Look up the dates for the current box (and target zoom); the server does
// this in the background, so poll until it is done.
let waybackPoll = null;
async function lookupWayback() {
  const bbox = currentBBox();
  if (state.imagerySource !== "wayback" || !bbox) return;
  const key = bbox.join(",");
  if (key === state.wayback.key) return;
  state.wayback.key = key;
  clearTimeout(waybackPoll);
  renderWaybackStatus({ status: "running", progress: 0, message: "Looking up imagery dates" });
  try {
    // look up at the finest zoom, so every date shows its full detail
    let task = await api("/api/wayback", { method: "POST", body: { bbox, resolution_m: 0.3 } });
    while (task.status === "running") {
      if (key !== state.wayback.key) return;
      renderWaybackStatus(task);
      await new Promise((r) => (waybackPoll = setTimeout(r, 900)));
      task = await api(`/api/wayback/${encodeURIComponent(task.id)}`);
    }
    if (key !== state.wayback.key) return;
    if (task.status === "failed") throw new Error(task.message);
    showWaybackResult(task.result);
  } catch (err) {
    if (key !== state.wayback.key) return;
    state.wayback.key = null;   // allow a retry
    clearWaybackResult();
    renderWaybackStatus({ status: "failed", message: err.message });
  }
}

function renderWaybackStatus(task) {
  const el = $("wayback-status");
  el.classList.toggle("hidden", task.status === "done");
  if (task.status === "failed") {
    el.innerHTML = `<span class="warn">${esc(task.message)}</span> <a href="#" id="wayback-retry">Try again</a>`;
    $("wayback-retry").onclick = (e) => { e.preventDefault(); lookupWayback(); };
    return;
  }
  el.innerHTML = `<div class="wb-progress"><span class="spinner"></span>${esc(task.message || "Looking up imagery dates")}…</div>
    <div class="bar"><div style="width:${Math.round((task.progress || 0) * 100)}%"></div></div>
    <div class="muted small">The first lookup for an area can take up to a minute.</div>`;
  if (task.status === "running") clearWaybackResult();
}

// Drop the previous area's dates and notes (counts, failed versions, resolution).
function clearWaybackResult() {
  Object.assign(state.wayback, { result: null, selected: null });
  $("wayback-list").innerHTML = "";
  $("wayback-res").innerHTML = "";
  $("wayback-more").classList.add("hidden");
  updateWaybackLayer();
}

function showWaybackResult(result) {
  state.wayback.result = result;
  const want = state.wayback.prefer ?? state.wayback.selected?.release;
  state.wayback.prefer = null;
  const keep = result.candidates.find((c) => c.release === want);
  state.wayback.selected = keep ?? result.candidates[0] ?? null;
  renderWaybackStatus({ status: "done" });
  renderWaybackList();
  updateWaybackLayer();
  schedulePlan();
}

const TIER_LABEL = { recommended: "Recommended", good: "Good", fair: "Fair", poor: "Poor" };

function renderWaybackList() {
  const { result, selected, showAll } = state.wayback;
  const list = $("wayback-list");
  list.innerHTML = "";
  if (!result) return;
  const cands = result.candidates;
  const shown = showAll ? cands : cands.slice(0, WAYBACK_SHOWN);
  if (selected && !shown.includes(selected)) shown.push(selected);
  for (const c of shown) {
    const li = document.createElement("li");
    li.className = `wb-item ${c.tier}${c === selected ? " active" : ""}`;
    li.setAttribute("role", "radio");
    li.setAttribute("aria-checked", c === selected);
    li.tabIndex = 0;
    const m = c.measures;
    const detail = m ? `Preview: edges ${m.edges}, contrast ${m.contrast}, colour ${m.colour}, haze ${m.haze}, cloud ${Math.round(m.cloud * 100)}%. ` : "";
    const caps = c.captures.map((x) => `${fmtDay(x.date)} ${x.satellite} ${x.resolution_m} m (${Math.round(x.share * 100)}%)`).join("; ");
    li.innerHTML = `
      ${c.thumb ? `<img class="wb-thumb" src="${c.thumb}" alt="">` : '<div class="wb-thumb"></div>'}
      <div class="wb-body">
        <div class="wb-head"><b>${fmtDay(c.capture_date)}</b>
          <span class="wb-badge ${c.tier}">${TIER_LABEL[c.tier]}</span>
          <span class="wb-score" title="Boundary clarity score (0-100)">${c.score}</span></div>
        <div class="wb-sub">${esc(c.satellite)} · ${waybackResolution(c)} m</div>
        <div class="wb-reasons">${c.reasons.map(esc).join(" · ")}</div>
      </div>
      ${infoIcon(`${detail}Captures in the box: ${caps}. Served up to zoom ${c.max_zoom} (~${c.max_zoom_res_m} m).`)}`;
    const pick = () => selectWayback(c);
    li.onclick = pick;
    li.onkeydown = (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), pick());
    list.appendChild(li);
  }
  const checked = result.versions_checked, failed = result.versions_failed || 0;
  $("wayback-res").innerHTML = (checked > cands.length
    ? `<div>${checked} Esri versions changed this area; they show ${cands.length} different ${cands.length === 1 ? "capture" : "captures"}.</div>` : "")
    + (failed ? `<div class="warn">${failed} older ${failed === 1 ? "version" : "versions"} could not be read: Esri's server did not respond. <a href="#" id="wayback-reload">Look up again</a></div>` : "")
    + (selected ? `<div>Finest detail of this date: <b>${waybackResolution(selected)} m</b>. ${resolutionNote(selected)}</div>` : "");
  const more = $("wayback-more");
  more.classList.toggle("hidden", cands.length <= WAYBACK_SHOWN);
  more.textContent = showAll ? "Show fewer dates" : `Show all ${cands.length} dates`;
}
document.addEventListener("click", (e) => {
  if (e.target.id !== "wayback-reload") return;
  e.preventDefault();
  api("/api/wayback/forget", { method: "POST", body: { bbox: currentBBox(), resolution_m: 0.3 } })
    .then(() => { state.wayback.key = null; lookupWayback(); })
    .catch((err) => toast(err.message, "error"));
});
$("wayback-more").onclick = () => { state.wayback.showAll = !state.wayback.showAll; renderWaybackList(); };

// The model was fine-tuned on ~1 m imagery: at much finer resolutions fields
// look larger than it is used to and the outlines get ragged.
function resolutionNote(c) {
  const res = resolution();
  return res > waybackResolution(c) + 0.005
    ? `The run downsamples it to <b>${res} m</b> (set under Resolution).`
    : `The run uses <b>${res} m</b>.`;
}

function selectWayback(c) {
  state.wayback.selected = c;
  renderWaybackList();
  updateWaybackLayer();
  schedulePlan();
  updateGenerateButton();
}

// Preview of the selected date on the map, above the basemap.
function updateWaybackLayer() {
  const c = state.wayback.selected;
  const show = state.imagerySource === "wayback" && c && $("wayback-preview").checked;
  if (state.waybackLayer && (!show || state.waybackLayer.options.release !== c.release)) {
    map.removeLayer(state.waybackLayer);
    state.waybackLayer = null;
  }
  if (show && !state.waybackLayer) {
    state.waybackLayer = L.tileLayer(WAYBACK_TILES, {
      release: c.release, maxZoom: 20, maxNativeZoom: c.max_zoom, pane: "wayback",
      attribution: `Imagery © Esri World Imagery Wayback (${fmtDay(c.capture_date)})`,
    }).addTo(map);
  }
}
$("wayback-preview").onchange = updateWaybackLayer;

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
  syncRunOverlays();
  renderJob(job);
  if (job.status === "queued" || job.status === "running") {
    state.pollTimer = setInterval(pollJob, 1000);
  } else if (job.status === "done") {
    await showResults(job, { fit: true });
    showTab("results");
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
    if (job.status === "done") {
      await showResults(job, { fit: true });
      showTab("results");
    }
  }
}

const STAGE_ORDER = ["imagery", "inference", "vectorize"];
function renderJob(job) {
  const j = state.jobs.find((x) => x.id === job.id);
  if (j) j.status = job.status;
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
  $("results-empty").classList.remove("hidden");
  $("map-legend").classList.add("hidden");
  $("edit-toolbar").classList.add("hidden");
}

async function showResults(job, { fit = false } = {}) {
  // Restore the run's bbox and parameters in the form so it can be tweaked/re-run.
  const [w, s, e, n] = job.params.bbox;
  setBBox(L.latLngBounds([s, w], [n, e]));
  if (job.params.source === "wayback") state.wayback.prefer = job.params.wayback_release;
  if (job.params.source === "wayback" || state.imagerySource === "wayback") setImagerySource(job.params.source || "google");
  $("pp-min-area").value = job.params.min_area_m2;
  $("pp-simplify").value = job.params.simplify_m;

  state.geojson = await api(`/api/jobs/${job.id}/fields`);

  if (state.imageryLayer) map.removeLayer(state.imageryLayer);
  state.imageryLayer = L.imageOverlay(`/api/jobs/${job.id}/preview.png?t=${Date.now()}`,
    job.result.mosaic.preview_bounds, { opacity: parseFloat($("imagery-opacity").value) });
  renderFields();
  renderJobActions();
  $("results").classList.remove("hidden");
  $("results-empty").classList.add("hidden");
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
  syncResultLayers();
  syncVertexEdit();
  renderStats();
  renderFieldList();
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
    `<div class="stat stat-model"><span>Imagery</span><b>${esc(imageryText(r.imagery))}</b></div>` +
    (r.mosaic.missing_tiles ? `<div class="stat warn-box">${r.mosaic.missing_tiles} of ${r.mosaic.total_tiles} imagery tiles failed to download</div>` : "");

  $("map-legend").innerHTML = `<span class="legend-swatch"></span><b>${fmt(visible.length)}</b> fields · ${fmt(areaHa, 1)} ha
    <div class="muted" style="margin-top:2px">${esc(usedModel.name)} · confidence ≥ ${minConfidence().toFixed(2)}</div>`;
  syncResultLayers();
}

function imageryText(img) {
  if (!img || img.source === "google") return "Google · date unknown";
  return `${img.source_label} · ${img.date ? fmtDay(img.date) : "date unknown"}`;
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
    showTab("results");
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
  renderRunsExport();
  cancelAnimationFrame(confFrame);
  confFrame = requestAnimationFrame(renderFields);
});

$("toggle-fields").onchange = syncResultLayers;
$("toggle-imagery").onchange = (e) => {
  $("imagery-opacity").classList.toggle("hidden", !e.target.checked);
  syncResultLayers();
};
$("imagery-opacity").oninput = (e) => state.imageryLayer?.setOpacity(parseFloat(e.target.value));

$("btn-postprocess").onclick = async () => {
  const btn = $("btn-postprocess");
  btn.disabled = true; btn.lastChild.textContent = "Applying…";
  try {
    const job = await api(`/api/jobs/${state.activeJob.id}/postprocess`, {
      method: "POST",
      body: { min_area_m2: parseFloat($("pp-min-area").value), simplify_m: parseFloat($("pp-simplify").value) },
    });
    state.activeJob = job;
    await showResults(job);
    refreshJobList();
    $("et-refilter-toggle").parentElement.classList.remove("open");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false; btn.lastChild.textContent = "Apply";
  }
};

// Re-filter is a flyout on the dock; a click elsewhere closes it.
$("et-refilter-toggle").onclick = () => $("et-refilter-toggle").parentElement.classList.toggle("open");
document.addEventListener("click", (e) => {
  if (!e.target.closest(".et-refilter")) $("et-refilter-toggle").parentElement.classList.remove("open");
});

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
  if (!on) { state.selected.clear(); state.bulk = false; }
  if (on) $("toggle-fields").checked = $("et-show-fields").checked = true;
  syncResultLayers();
  document.body.classList.toggle("editing", on);
  $("edit-toolbar").classList.toggle("idle", !on);
  syncBBoxEditing();
  refreshSelection();
}

function selectField(id, additive) {
  if (additive || state.bulk) state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  else state.selected = new Set([id]);
  refreshSelection();
}

function refreshSelection() {
  state.layers.forEach((layer) => layer.setStyle(styleOf(layer)));
  syncVertexEdit();
  updateEditToolbar();
}

map.on("click", () => {
  if (state.editing && !state.drawKind && !state.bulk && state.selected.size) {
    state.selected.clear();
    refreshSelection();
  }
});

const vertexEditOpts = () => ({ ...snapOpts(), allowSelfIntersection: false, draggable: false });

// Toggling Snap applies right away to the line being drawn and the field
// being reshaped. (No overlaps is read when each edit is sent.)
$("et-snap").addEventListener("change", () => {
  if (state.drawKind === "field" || state.drawKind === "split") {
    const draw = map.pm.Draw[state.drawKind === "field" ? "Polygon" : "Line"];
    draw.setOptions(snapOpts());
    if (draw._hintMarker) draw._hintMarker._snapped = false;
  }
  const layer = state.vertexLayer;
  if (layer?.pm.enabled()) {
    layer.pm.disable();
    layer.pm.enable(vertexEditOpts());
  }
});

// Hiding the fields while editing shows the imagery; nothing stays selected.
$("et-show-fields").addEventListener("change", (e) => {
  if (!e.target.checked) { state.selected.clear(); setHover(null); }
  syncResultLayers();
  refreshSelection();
});

// Vertex editing is on for exactly one selected field.
function syncVertexEdit() {
  const only = state.selected.size === 1 && !state.bulk ? state.layers.get([...state.selected][0]) : null;
  const target = state.editing && !state.drawKind && $("et-show-fields").checked ? only : null;
  const current = state.vertexLayer;
  if (current && current !== target) {
    current.off("pm:edit", onVertexEdit);
    if (current.pm.enabled()) current.pm.disable();
  }
  if (target && current !== target) {
    target.pm.enable(vertexEditOpts());
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
  const n = state.selected.size;
  $("edit-panel").querySelector("[data-tool=draw]").classList.toggle("active", state.drawKind === "field");
  $("edit-panel").querySelector("[data-tool=split]").classList.toggle("active", state.drawKind === "split");
  $("edit-panel").querySelector("[data-tool=smooth]").classList.toggle("active", state.drawKind === "smooth");
  $("et-merge").disabled = false;
  $("et-delete").disabled = n < 1;
  $("et-merge").querySelector("span").textContent = n >= 2 ? `Merge ${n}` : "Merge";
  $("et-delete").querySelector("span").textContent = n > 1 ? `Delete ${n}` : "Delete";
  document.querySelectorAll("[data-action=undo]").forEach((b) => (b.disabled = !(state.activeJob?.result.undo_depth > 0)));
  document.querySelectorAll("[data-action=redo]").forEach((b) => (b.disabled = !(state.activeJob?.result.redo_depth > 0)));
  syncEditPanelSelection();
  const total = state.geojson?.features.length ?? 0;
  const merging = !!state.bulk && state.bulkKind === "merge";
  $("et-bulk").classList.toggle("active", !!state.bulk && !merging);
  $("et-merge").classList.toggle("active", merging);
  $("et-bulkbar").classList.toggle("hidden", !state.bulk);
  $("et-bulk-discard").classList.toggle("hidden", merging);
  $("et-bulk-keep").classList.toggle("hidden", merging);
  $("et-bulk-merge").classList.toggle("hidden", !merging);
  $("et-bulk-merge").disabled = n < 2;
  $("et-bulk-merge").textContent = n >= 2 ? `Merge ${n} fields` : "Merge selected";
  $("et-bulk-discard").disabled = n < 1;
  $("et-bulk-keep").disabled = n < 1 || n >= total;
  $("et-bulk-discard").textContent = `Discard ${n} selected`;
  $("et-bulk-keep").textContent = `Keep ${n} selected, discard the rest`;
  $("et-hint").textContent =
    merging ? `Merge: click the fields you want to join (${n} selected), then press Merge. Esc cancels.` :
    state.bulk ? `Bulk select: click every field you want to keep or discard (${n} selected), then press an OK button. Esc cancels.` :
    state.drawKind === "field" ? "Click to place vertices; click the first vertex to finish. Snaps to nearby fields. Esc cancels." :
    state.drawKind === "smooth" ? "Hold the left mouse button and brush over a jagged edge: the vertices under the brush are removed and the edge is redrawn straight. Drag with the right button to move the map. Esc stops." :
    state.drawKind === "split" ? `Click two points to draw a line across ${n === 1 ? "the selected field" : "the fields to split"}; it splits as soon as you place the second. Esc cancels.` :
    n === 0 ? "" :
    n === 1 ? "Drag a vertex to reshape, drag a midpoint to add one, right-click a vertex to remove it. Shift+click more fields to merge." :
    `${n} fields selected: merge or delete them.`;
}

$("et-start").onclick = () => setEditing(true);
$("et-done").onclick = () => setEditing(false);
$("edit-panel").querySelector("[data-tool=draw]").onclick = () => (state.drawKind === "field" ? cancelDraw() : startDraw("field"));
$("edit-panel").querySelector("[data-tool=split]").onclick = () => (state.drawKind === "split" ? cancelDraw() : startDraw("split"));
$("edit-panel").querySelector("[data-tool=smooth]").onclick = () => (state.drawKind === "smooth" ? cancelDraw() : startSmooth());
const mergeSelected = () => editRequest("/fields/merge", { field_ids: [...state.selected], avoid_overlap: avoidOverlap() });
// With two or more fields selected, Merge joins them right away. Otherwise it
// starts a merge mode where you click the fields to join, then confirm.
$("et-merge").onclick = () => {
  if (state.bulk && state.bulkKind === "merge") setBulk(false);
  else if (!state.bulk && state.selected.size >= 2) mergeSelected();
  else setBulk(true, "merge");
};
$("et-bulk-merge").onclick = async () => {
  if (state.selected.size >= 2 && await mergeSelected()) setBulk(false);
};
$("et-delete").onclick = () => state.selected.size && editRequest("/fields/delete", { field_ids: [...state.selected] });
const undo = () => editRequest("/fields/undo");
const redo = () => editRequest("/fields/redo");
document.querySelectorAll("[data-action=undo]").forEach((b) => (b.onclick = undo));
document.querySelectorAll("[data-action=redo]").forEach((b) => (b.onclick = redo));

function setBulk(on, kind = "select") {
  state.bulk = on;
  state.bulkKind = on ? kind : null;
  if (on) cancelDraw();
  state.selected.clear();
  refreshSelection();
}
$("et-bulk").onclick = () => setBulk(!(state.bulk && state.bulkKind === "select"), "select");
$("et-bulk-cancel").onclick = () => setBulk(false);
$("et-bulk-discard").onclick = async () => {
  if (!state.selected.size) return;
  if (await editRequest("/fields/delete", { field_ids: [...state.selected] })) setBulk(false);
};
$("et-bulk-keep").onclick = async () => {
  const keep = state.selected;
  const drop = state.geojson.features.map((f) => f.properties.field_id).filter((id) => !keep.has(id));
  if (!keep.size || !drop.length) return;
  if (await editRequest("/fields/delete", { field_ids: drop })) setBulk(false);
};
L.DomEvent.disableClickPropagation($("edit-toolbar"));

// ------------------------------------------------------------- edit panel
// The sidebar while editing: tools, options and a field list.
const zoomToLayers = (layers) => {
  if (!layers.length) return;
  const bounds = layers.reduce((b, l) => b.extend(l.getBounds()), L.latLngBounds(layers[0].getBounds()));
  map.fitBounds(bounds, { padding: [80, 80], maxZoom: Math.max(map.getZoom(), 16) });
};

function renderFieldList() {
  const visible = visibleFeatures().slice().sort((a, b) => b.properties.area_ha - a.properties.area_ha);
  $("ep-count").textContent = `${fmt(visible.length)} shown`;
  $("ep-fields").innerHTML = visible.map(({ properties: p }) => `
    <li data-id="${p.field_id}"><b>#${p.field_id}</b><span class="grow">${fmt(p.area_ha, 2)} ha</span>
      <span class="muted">${p.source === "model" ? `conf ${p.confidence.toFixed(2)}` : esc(p.source)}</span></li>`).join("");
  syncEditPanelSelection();
}

function syncEditPanelSelection() {
  const n = state.selected.size;
  $("ep-fields").querySelectorAll("li").forEach((li) => li.classList.toggle("selected", state.selected.has(+li.dataset.id)));
  const area = [...state.selected].reduce((s, id) => s + (state.layers.get(id)?.feature.properties.area_ha ?? 0), 0);
  $("ep-selection").innerHTML = n === 0 ? "Click a field to select it; Shift+click to add more."
    : `<b>${n}</b> field${n > 1 ? "s" : ""} selected · ${fmt(area, 2)} ha` +
      (n === 1 ? " · drag its vertices to reshape" : " · Merge or Delete them");
}

$("ep-fields").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  const id = +li.dataset.id;
  selectField(id, e.shiftKey || e.ctrlKey || e.metaKey);
  if (state.selected.has(id)) zoomToLayers([state.layers.get(id)].filter(Boolean));
});
$("ep-fields").addEventListener("mouseover", (e) => {
  const li = e.target.closest("li");
  if (li) setHover(state.layers.get(+li.dataset.id) ?? null);
});
$("ep-fields").addEventListener("mouseleave", () => setHover(null));

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea")) return;
  if (e.key === "Escape") {
    if (state.drawKind) cancelDraw();
    else if (state.bulk) setBulk(false);
    else if (state.editing && state.selected.size) { state.selected.clear(); refreshSelection(); }
    else if (state.editing) setEditing(false);
    return;
  }
  const key = e.key.toLowerCase();
  if (!state.editing) {
    if (key === "e" && !e.ctrlKey && !e.metaKey && !e.altKey && !$("edit-toolbar").classList.contains("hidden")) setEditing(true);
    return;
  }
  if ((e.ctrlKey || e.metaKey) && key === "z" && e.shiftKey) { e.preventDefault(); redo(); }
  else if ((e.ctrlKey || e.metaKey) && key === "y") { e.preventDefault(); redo(); }
  else if ((e.ctrlKey || e.metaKey) && key === "z") { e.preventDefault(); undo(); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); $("et-delete").click(); }
  else if (e.ctrlKey || e.metaKey || e.altKey) return;
  else if (key === "n") startDraw("field");
  else if (key === "s") startDraw("split");
  else if (key === "b") $("edit-panel").querySelector("[data-tool=smooth]").click();
  else if (key === "m") $("et-merge").click();
  else if (key === "h") $("et-show-fields").click();
});

// A run's imagery, fields and legend are only on the map under Results, so
// the other steps show the plain basemap (and the Wayback preview).
const onResultsTab = () => document.querySelector('.tab-pane[data-pane="results"]')?.classList.contains("active");

function syncResultLayers() {
  const on = onResultsTab();
  const show = (layer, wanted) => {
    if (!layer) return;
    if (on && wanted && !map.hasLayer(layer)) layer.addTo(map);
    if (!(on && wanted) && map.hasLayer(layer)) map.removeLayer(layer);
  };
  show(state.imageryLayer, $("toggle-imagery").checked);
  show(state.fieldsLayer, state.editing ? $("et-show-fields").checked : $("toggle-fields").checked);
  if (state.fieldsLayer && map.hasLayer(state.fieldsLayer)) state.fieldsLayer.bringToFront();
  $("map-legend").classList.toggle("hidden", !(on && state.geojson));
  $("edit-toolbar").classList.toggle("hidden", !(on && state.geojson));
  renderRunOverlays();
}

// ------------------------------------------------------- QGIS (run)
function renderJobActions() {
  const job = state.activeJob;
  if (!job) return;
  const edits = job.result.edited;
  $("edited-note").classList.toggle("hidden", !edits);
  $("edited-note").textContent = edits ? `edited ${fmtDate(edits)}` : "";
  $("qgis-import").classList.toggle("hidden", !job.result.qgis_file);
}

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
    const imgDate = job.params.source === "wayback" && job.params.imagery_date ? ` · imagery ${job.params.imagery_date.slice(0, 7)}` : "";
    const tick = job.status === "done"
      ? `<input type="checkbox" class="run-check" title="Select for export"${state.runSelection.has(job.id) ? " checked" : ""}>`
      : '<span class="run-check-spacer"></span>';
    li.innerHTML = `${tick}<span class="dot ${job.status}"></span>
      <div class="meta"><div class="name-row"><span class="name"></span>
</div>
        <div class="sub">${fmtDate(job.created)} · ${info}${imgDate}</div></div>
      <button class="del" title="Delete run">✕</button>`;
    li.querySelector(".name").textContent = job.name;
    li.onclick = () => selectJob(job.id);
    const check = li.querySelector(".run-check");
    if (check) {
      check.onclick = (e) => e.stopPropagation();
      check.onchange = () => {
        check.checked ? state.runSelection.add(job.id) : state.runSelection.delete(job.id);
        renderRunsExport();
      };
    }
    li.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete run "${job.name}" and its files?`)) return;
      try {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        if (state.activeJob?.id === job.id) forgetActiveJob();
        refreshJobList();
      } catch (err) { toast(err.message, "error"); }
    };
    $("job-list").appendChild(li);
  }
  const done = new Set(jobs.filter((j) => j.status === "done").map((j) => j.id));
  state.runSelection.forEach((id) => done.has(id) || state.runSelection.delete(id));
  renderRunsExport();
  highlightJobList();
  updateTabStates();
}

// Several runs ticked in the list: download their fields in one file.
function renderRunsExport() {
  const n = state.runSelection.size;
  $("runs-export").classList.toggle("hidden", n === 0);
  $("runs-sel-count").textContent = `${n} ${n === 1 ? "run" : "runs"} selected`;
  syncRunOverlays();
  $("btn-export-runs").textContent = `Download fields of ${n} ${n === 1 ? "run" : "runs"}`;
  $("runs-export-note").textContent = `One file; the run_name and run_id columns tell the runs apart. `
    + `Only fields with confidence ≥ ${minConfidence().toFixed(2)} (the slider above).`;
}

// Fields of every ticked run, drawn on the map (non-interactive) in the normal field style.
const runOverlays = new Map();   // run id -> { layer, features }
let overlaySeq = 0;

async function syncRunOverlays() {
  const seq = ++overlaySeq;
  const wanted = new Set(state.runSelection);
  if (state.activeJob) wanted.delete(state.activeJob.id);   // the open run is drawn as the main layer
  for (const [id, o] of runOverlays) {
    if (!wanted.has(id)) { map.removeLayer(o.layer); runOverlays.delete(id); }
  }
  for (const id of wanted) {
    if (runOverlays.has(id)) continue;
    try {
      const gj = await api(`/api/jobs/${id}/fields`);
      if (seq !== overlaySeq) return;   // a newer sync superseded this one
      const layer = L.geoJSON({ type: "FeatureCollection", features: [] }, {
        interactive: false,
        style: FIELD_STYLE,
      });
      runOverlays.set(id, { layer, features: gj.features });
    } catch (_) { /* run may have been deleted meanwhile */ }
  }
  renderRunOverlays();
}

function renderRunOverlays() {
  const c = minConfidence();
  const on = onResultsTab();
  for (const o of runOverlays.values()) {
    o.layer.clearLayers();
    o.layer.addData({ type: "FeatureCollection", features: o.features.filter((f) => f.properties.confidence >= c) });
    if (on && !map.hasLayer(o.layer)) o.layer.addTo(map);
    if (!on && map.hasLayer(o.layer)) map.removeLayer(o.layer);
  }
}

$("runs-sel-all").onclick = (e) => {
  e.preventDefault();
  state.jobs.filter((j) => j.status === "done").forEach((j) => state.runSelection.add(j.id));
  document.querySelectorAll("#job-list .run-check").forEach((c) => (c.checked = true));
  renderRunsExport();
};
$("runs-sel-clear").onclick = (e) => {
  e.preventDefault();
  state.runSelection.clear();
  document.querySelectorAll("#job-list .run-check").forEach((c) => (c.checked = false));
  renderRunsExport();
};

$("btn-export-runs").onclick = async () => {
  const q = new URLSearchParams({
    ids: [...state.runSelection].join(","), format: $("runs-format").value,
    crs: $("runs-crs").value, min_confidence: minConfidence(),
  });
  const btn = $("btn-export-runs");
  btn.disabled = true;
  try {
    // fetched first, so an error (e.g. runs in different UTM zones) shows as a message
    const res = await api(`/api/export?${q}`);
    const name = /filename="?([^";]+)"?/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? "fields";
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await res.blob());
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
};

function forgetActiveJob() {
  setEditing(false);
  clearResults();
  state.activeJob = null;
  $("progress").classList.add("hidden");
  history.replaceState(null, "", "/");
}

$("btn-delete-all").onclick = async () => {
  const n = state.jobs.length;
  if (!confirm(`Delete all ${n} runs and their files (imagery and model output)? This cannot be undone.`)) return;
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

function renderQgisButtons() {
  const q = state.system.qgis;
  $("btn-qgis").disabled = !q.available;
  if (!q.available) $("btn-qgis").title = "QGIS was not found on this computer. Install QGIS, or set DELTOOL_QGIS to its executable.";
}

// ------------------------------------------------------------- init
async function init() {
  const cfg = (state.config = await api("/api/config"));
  const d = cfg.defaults;

  state.resolution = d.resolution_m;
  let savedSource = "google";
  try { savedSource = localStorage.getItem("deltool-source") || "google"; } catch (_) {}
  setImagerySource(savedSource === "wayback" ? "wayback" : "google");
  renderResolutionPicker(cfg.resolutions, d.resolution_m);
  const formats = Object.entries(cfg.export_formats).map(([k, label]) => `<option value="${k}">${label}</option>`).join("");
  $("export-format").innerHTML = formats;
  $("runs-format").innerHTML = formats;

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
  renderQgisButtons();
  await refreshJobList();

  showTab("area");
  updateTabStates();

  const linked = new URLSearchParams(location.search).get("run");
  const running = state.jobs.find((j) => j.status === "running" || j.status === "queued");
  const backToArea = () => showTab("area");
  if (linked) selectJob(linked).catch(() => history.replaceState(null, "", "/")).then(backToArea);
  else if (running) selectJob(running.id).then(backToArea);
}

// Easter egg: click the TU Delft / TAHMO logos 3 times for rainbow mode.
let rainbowTimer = null;
let rainbowAudio = null;

// 10 seconds of synthesized turbo dance music (150 BPM): four-on-the-floor kick,
// off-beat hats, a driving sawtooth bass and a fast rising arpeggio.
function playTurboDance(seconds) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  const ac = new Ctx();
  const t0 = ac.currentTime + 0.05, end = t0 + seconds;
  const master = ac.createGain();
  master.gain.setValueAtTime(0.5, t0);
  master.gain.setValueAtTime(0.5, end - 0.3);
  master.gain.linearRampToValueAtTime(0, end);
  master.connect(ac.destination);

  const beat = 60 / 150;
  const noise = ac.createBuffer(1, ac.sampleRate * 0.2, ac.sampleRate);
  noise.getChannelData(0).forEach((_, i, d) => { d[i] = Math.random() * 2 - 1; });

  const tone = (type, freq, t, dur, vol, freqEnd) => {
    const o = ac.createOscillator(), g = ac.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    if (freqEnd) o.frequency.exponentialRampToValueAtTime(freqEnd, t + dur);
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    o.connect(g).connect(master);
    o.start(t); o.stop(t + dur + 0.02);
  };
  const hat = (t, vol) => {
    const s = ac.createBufferSource(), f = ac.createBiquadFilter(), g = ac.createGain();
    s.buffer = noise; f.type = "highpass"; f.frequency.value = 7000;
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.07);
    s.connect(f).connect(g).connect(master);
    s.start(t); s.stop(t + 0.1);
  };

  const root = [55, 55, 65.41, 49];            // A1 A1 C2 G1, one per bar
  const scale = [1, 1.2, 1.5, 1.8, 2, 2.4, 3, 2.4];   // minor arpeggio ratios
  for (let b = 0, t = t0; t < end; b++, t += beat / 4) {
    const step = b % 16, bar = Math.floor(b / 16) % 4;
    if (step % 4 === 0) tone("sine", 160, t, 0.18, 1, 40);            // kick
    if (step % 4 === 2) hat(t, 0.35);                                 // off-beat hat
    if (step % 4 === 1 || step % 4 === 3) hat(t, 0.12);
    if (step === 4 || step === 12) hat(t, 0.5);                       // clap-ish accent
    if (step % 2 === 1) tone("sawtooth", root[bar], t, beat / 4, 0.28); // bass pulse
    tone("square", root[bar] * 4 * scale[b % scale.length], t, beat / 5, 0.1); // arp
  }
  return ac;
}

function stopRainbow() {
  clearTimeout(rainbowTimer);
  document.body.classList.remove("rainbow");
  rainbowAudio?.close().catch(() => {});
  rainbowAudio = null;
}

function rainbow(ms = 10000) {
  stopRainbow();
  document.body.classList.add("rainbow");
  try { rainbowAudio = playTurboDance(ms / 1000); } catch (_) { rainbowAudio = null; }
  rainbowTimer = setTimeout(stopRainbow, ms);
}
(() => {
  let clicks = 0, last = 0;
  document.querySelector(".credit-logos")?.addEventListener("click", () => {
    const now = Date.now();
    clicks = now - last < 1200 ? clicks + 1 : 1;
    last = now;
    if (clicks >= 3) {
      clicks = 0;
      if (document.body.classList.contains("rainbow")) stopRainbow();
      else rainbow();
    }
  });
})();

init();
