/* Startup loading screen: animated field patchwork while the server loads the model.
 * Exposes window.appReady, a promise resolved with the model status once it's loaded. */
"use strict";

(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";
  // The patchwork fills the whole screen: about one field per 170 x 150 px.
  const W = Math.max(window.innerWidth, 640), H = Math.max(window.innerHeight, 480);
  const COLS = Math.ceil(W / 170), ROWS = Math.ceil(H / 150);
  const PAD = 0, JITTER = Math.min(W / COLS, H / ROWS) * 0.18, GAP = 5;
  // muted earth tones so the ground reads like satellite imagery
  const GROUND = ["#22392a", "#2a3b28", "#30402b", "#263a2e", "#34412f", "#2c3826"];
  const CYCLE_S = 12;

  function el(name, attrs, parent) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    parent.appendChild(node);
    return node;
  }

  function drawPatchwork(svg) {
    const defs = el("defs", {}, svg);
    const glow = el("filter", { id: "glow", x: "-20%", y: "-20%", width: "140%", height: "140%" }, defs);
    el("feGaussianBlur", { stdDeviation: 1.6, result: "b" }, glow);
    const merge = el("feMerge", {}, glow);
    el("feMergeNode", { in: "b" }, merge);
    el("feMergeNode", { in: "SourceGraphic" }, merge);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    el("rect", { x: 0, y: 0, width: W, height: H, class: "ground" }, svg);
    const groundLayer = el("g", {}, svg);

    // jittered grid of shared vertices -> irregular quads that tile the area
    const pts = [];
    for (let r = 0; r <= ROWS; r++) {
      pts.push([]);
      for (let c = 0; c <= COLS; c++) {
        const edgeX = c === 0 || c === COLS, edgeY = r === 0 || r === ROWS;
        const x = PAD + (c / COLS) * (W - 2 * PAD) + (edgeX ? 0 : (Math.random() - 0.5) * 2 * JITTER);
        const y = PAD + (r / ROWS) * (H - 2 * PAD) + (edgeY ? 0 : (Math.random() - 0.5) * 2 * JITTER);
        pts[r].push([x, y]);
      }
    }

    const fields = [];
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const quad = [pts[r][c], pts[r][c + 1], pts[r + 1][c + 1], pts[r + 1][c]];
        // occasionally split a cell diagonally into two fields
        const polys = Math.random() < 0.3
          ? [[quad[0], quad[1], quad[2]], [quad[0], quad[2], quad[3]]]
          : [quad];
        fields.push(...polys);
      }
    }

    // static "imagery": each field a slightly different tone, plus some trees
    fields.forEach((poly, i) => {
      el("polygon", { points: poly.map((p) => p.join(",")).join(" "),
        fill: GROUND[i % GROUND.length] }, groundLayer);
    });
    for (let i = 0; i < (W * H) / 4500; i++) {
      el("circle", { cx: Math.random() * W, cy: Math.random() * H, r: 1.2 + Math.random() * 2.8,
        fill: "#132619", opacity: 0.85 }, groundLayer);
    }

    // delineation outlines, shrunk towards the centroid to leave field margins
    const order = fields.map((_, i) => i).sort(() => Math.random() - 0.5);
    fields.forEach((poly, i) => {
      const cx = poly.reduce((s, p) => s + p[0], 0) / poly.length;
      const cy = poly.reduce((s, p) => s + p[1], 0) / poly.length;
      const shrunk = poly.map(([x, y]) => {
        const dx = x - cx, dy = y - cy, d = Math.hypot(dx, dy);
        return [x - (dx / d) * GAP, y - (dy / d) * GAP];
      });
      const node = el("polygon", {
        points: shrunk.map((p) => p.map((v) => v.toFixed(1)).join(",")).join(" "),
        class: "field", pathLength: 1, filter: "url(#glow)",
      }, svg);
      const stagger = (CYCLE_S * 0.55) / fields.length;
      node.style.setProperty("--cycle", `${CYCLE_S}s`);
      node.style.animationDelay = `${order[i] * stagger}s`;
    });
  }

  drawPatchwork(document.getElementById("loader-art"));

  const loader = document.getElementById("loader");
  const msg = document.getElementById("loader-msg");
  const checkList = document.getElementById("loader-checks");

  // Warm-up checklist: installed packages and model weights.
  function renderChecks(checks) {
    checkList.innerHTML = "";
    for (const c of checks || []) {
      const li = document.createElement("li");
      li.className = c.ok ? "ok" : c.optional ? "optional" : "fail";
      li.textContent = c.ok && c.detail ? `${c.name} ${c.detail}` : c.name;
      li.title = c.ok ? "Installed" : c.detail + (c.optional ? " (optional)" : "");
      checkList.appendChild(li);
    }
  }
  const firstVisit = !sessionStorage.getItem("deltool-loaded");
  const minShowUntil = Date.now() + (firstVisit ? 3500 : 900);

  window.appReady = new Promise((resolve) => {
    async function poll() {
      try {
        const s = await (await fetch("/api/status")).json();
        msg.textContent = s.message + "…";
        renderChecks(s.checks);
        if (s.state === "error") {
          msg.parentElement.classList.add("error");
          msg.textContent = s.message;
          return;
        }
        if (s.state === "ready") {
          msg.textContent = "Model ready";
          setTimeout(() => {
            loader.classList.add("done");
            sessionStorage.setItem("deltool-loaded", "1");
            resolve(s);
          }, Math.max(0, minShowUntil - Date.now()));
          return;
        }
      } catch (_) {
        msg.textContent = "Waiting for server…";
      }
      setTimeout(poll, 400);
    }
    poll();
  });
})();
