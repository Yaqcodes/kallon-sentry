/* Sentinel — tower console front-end logic (vanilla, no build step).
 * Ingest-only: talks solely to the local gateway (/api/*). No hardware access. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  cameras: [],
  activeCamera: 1,
  seenAlertKeys: new Set(),
  alertCount: 0,
};

/* ------------------------------- routing ------------------------------- */
function showView(name) {
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === name));
  try { location.hash = name; } catch (_) {}
}

function initRouter() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
  const initial = (location.hash || "#feed").replace("#", "");
  showView(initial === "monitor" ? "monitor" : "feed");
}

/* ------------------------------ http utils ----------------------------- */
async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function setConn(ok) {
  const el = $("#conn");
  el.classList.remove("conn--ok", "conn--bad", "conn--unknown");
  el.classList.add(ok ? "conn--ok" : "conn--bad");
  $("#conn-label").textContent = ok ? "connected" : "gateway offline";
}

/* --------------------------------- HLS --------------------------------- */
function attachHls(video, url) {
  const note = video.closest(".cam")?.querySelector(".cam-note");

  function showNote(msg) {
    if (!note) return;
    note.style.display = "";
    note.textContent = msg;
  }

  // Desktop Chromium reports native HLS support but cannot play mediamtx master
  // playlists reliably — always prefer hls.js when available.
  if (window.Hls && window.Hls.isSupported()) {
    if (video._hls) {
      try { video._hls.destroy(); } catch (_) {}
      video._hls = null;
    }
    const hls = new Hls({
      liveSyncDurationCount: 1,
      manifestLoadingRetryDelay: 1500,
      backBufferLength: 4,
      lowLatencyMode: true,
    });
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => showNote("tap to play (browser policy)"));
      setTimeout(() => {
        if (note.style.display === "none") return;
        if (video.readyState < 2 && !video.currentTime) {
          showNote("set substream to H.264 in camera web UI");
        }
      }, 8000);
    });
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (!data || !data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        showNote("stream reconnecting…");
        setTimeout(() => { try { hls.startLoad(); } catch (_) {} }, 1500);
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        try { hls.recoverMediaError(); } catch (_) {
          showNote("set substream to H.264 in camera web UI");
        }
      } else {
        showNote("stream unavailable");
        try { hls.destroy(); } catch (_) {}
        setTimeout(() => attachHls(video, url), 3000);
      }
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    video._hls = hls;
    return;
  }

  // Safari / iOS native HLS fallback.
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    video.play().catch(() => showNote("tap to play"));
    return;
  }

  showNote("HLS not supported in this browser");
}

/* -------------------------------- MJPEG -------------------------------- */
function attachMjpeg(tile, url) {
  const note = $(".cam-note", tile);

  function showNote(msg) { note.style.display = ""; note.textContent = msg; }

  const img = document.createElement("img");
  img.className = "cam-video";
  img.alt = "";
  img.src = url;

  img.addEventListener("load", () => { note.style.display = "none"; });
  img.addEventListener("error", () => {
    showNote("stream unavailable — retrying…");
    setTimeout(() => { img.src = url + "?t=" + Date.now(); }, 3000);
  });

  tile.insertBefore(img, tile.firstChild);
}

/* --------------------------- camera grid + config ---------------------- */
function buildCameraGrid() {
  const grid = $("#camera-grid");
  grid.innerHTML = "";
  grid.dataset.count = String(state.cameras.length);
  state.cameras.forEach((cam) => {
    const tile = document.createElement("div");
    tile.className = "cam";
    tile.dataset.camera = String(cam.camera);
    tile.innerHTML = `
      <div class="cam-tag"><span class="dot"></span><span class="name">${cam.label}</span></div>
      <div class="cam-note">connecting…</div>`;

    if (cam.mjpeg_url) {
      attachMjpeg(tile, cam.mjpeg_url);
    } else {
      const video = document.createElement("video");
      video.muted = true; video.playsInline = true; video.autoplay = true;
      tile.insertBefore(video, tile.firstChild);
      const note = $(".cam-note", tile);
      video.addEventListener("playing", () => (note.style.display = "none"));
      video.addEventListener("waiting", () => { note.style.display = ""; note.textContent = "buffering…"; });
      attachHls(video, cam.hls_url);
    }

    tile.addEventListener("click", () => setActiveCamera(cam.camera));
    grid.appendChild(tile);
  });
  setActiveCamera(state.activeCamera);
}

function buildPtzCameraButtons() {
  const wrap = $("#ptz-cameras");
  wrap.innerHTML = "";
  state.cameras.forEach((cam) => {
    const b = document.createElement("button");
    b.className = "ptz-cam";
    b.dataset.camera = String(cam.camera);
    b.textContent = cam.label;
    b.addEventListener("click", () => setActiveCamera(cam.camera));
    wrap.appendChild(b);
  });
}

function setActiveCamera(n) {
  state.activeCamera = n;
  $$(".cam").forEach((c) => c.classList.toggle("is-active", Number(c.dataset.camera) === n));
  $$(".ptz-cam").forEach((c) => c.classList.toggle("is-active", Number(c.dataset.camera) === n));
  const label = (state.cameras.find((c) => c.camera === n) || {}).label || `cam${n}`;
  $("#ptz-active-label").textContent = label;
}

async function loadConfig() {
  const cfg = await getJSON("/api/config");
  $("#device-id").textContent = cfg.device_id || "unknown-device";
  state.cameras = cfg.cameras || [];
  if (state.cameras.length && !state.cameras.find((c) => c.camera === state.activeCamera)) {
    state.activeCamera = state.cameras[0].camera;
  }
  buildCameraGrid();
  buildPtzCameraButtons();
}

/* ------------------------- stream readiness poll ----------------------- */
async function pollStreams() {
  try {
    const data = await getJSON("/api/streams");
    const byName = {};
    (data.paths || []).forEach((p) => (byName[p.name] = p));
    state.cameras.forEach((cam) => {
      const tag = $(`.cam[data-camera="${cam.camera}"] .cam-tag`);
      if (!tag) return;
      const info = byName[cam.path];
      tag.classList.toggle("ready", !!(info && info.ready));
      tag.classList.toggle("down", !!(info && !info.ready));
    });
  } catch (_) { /* leave badges as-is */ }
}

/* ------------------------------ status poll ---------------------------- */
function pill(state_) {
  const map = { ok: "OK", warn: "WARN", crit: "ALERT", unknown: "—" };
  return `<span class="tile-pill pill--${state_}">${map[state_] || state_}</span>`;
}

function tile(label, level, valueHtml, subHtml) {
  return `<div class="tile tile--${level}">
    <div class="tile-head"><span class="tile-label">${label}</span>${pill(level)}</div>
    ${valueHtml || ""}${subHtml || ""}</div>`;
}

function fmtUptime(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

function renderStatus(s) {
  const tiles = [];
  const avail = s && s.available;

  // Door
  const door = (s && s.door) || {};
  if (!avail || door.open == null) tiles.push(tile("Enclosure door", "unknown", `<div class="tile-value">—</div>`));
  else tiles.push(tile("Enclosure door", door.open ? "crit" : "ok",
    `<div class="tile-value">${door.open ? "Open" : "Closed"}</div>`));

  // Cover / light
  const light = (s && s.light) || {};
  if (!avail || light.exposed == null) tiles.push(tile("Cover / light", "unknown", `<div class="tile-value">—</div>`));
  else tiles.push(tile("Cover / light", light.exposed ? "crit" : "ok",
    `<div class="tile-value">${light.exposed ? "Exposed" : "Enclosed"}</div>`));

  // Temperature
  const t = (s && s.temperature) || {};
  if (!avail || t.celsius == null) {
    tiles.push(tile("Temperature", "unknown", `<div class="tile-value">—</div>`));
  } else {
    const trig = t.trigger_c || 80;
    const level = t.critical ? "crit" : (t.celsius >= trig - 5 ? "warn" : "ok");
    const pct = Math.max(0, Math.min(100, (t.celsius / trig) * 100));
    const color = level === "crit" ? "var(--crit)" : level === "warn" ? "var(--warn)" : "var(--ok)";
    tiles.push(tile("Temperature", level,
      `<div class="tile-value">${t.celsius.toFixed(1)}<span class="unit">°C</span></div>`,
      `<div class="gauge"><div class="gauge-fill" style="width:${pct}%;background:${color}"></div></div>
       <div class="tile-sub">${t.zone || ""} · trip ${trig}°C</div>`));
  }

  // Impact
  const im = (s && s.impact) || {};
  if (!avail) tiles.push(tile("Impact / motion", "unknown", `<div class="tile-value">—</div>`));
  else {
    const recent = im.last_impact_utc && (Date.now() - Date.parse(im.last_impact_utc)) < 120000;
    tiles.push(tile("Impact / motion", recent ? "warn" : "ok",
      `<div class="tile-value">${recent ? "Recent hit" : "Stable"}</div>`,
      `<div class="tile-sub">Δ ${im.last_delta_mg != null ? im.last_delta_mg : "—"} / ${im.threshold_mg || "—"} mg${im.last_impact_utc ? " · last " + fmtClock(im.last_impact_utc) : ""}</div>`));
  }

  // Streams
  const streams = (s && s.streams) || [];
  if (!avail || !streams.length) tiles.push(tile("Camera streams", "unknown", `<div class="tile-value">—</div>`));
  else {
    const down = streams.filter((x) => !x.ok).length;
    const rows = streams.map((x) =>
      `<div class="stream-row"><span class="name">${x.path}</span>
        <span class="state ${x.ok ? "ready" : "down"}"><span class="dot"></span>${x.ok ? "live" : "down"}</span></div>`).join("");
    tiles.push(tile("Camera streams", down ? "crit" : "ok", `<div class="tile-streams">${rows}</div>`));
  }

  // Disk
  const disk = (s && s.disk) || {};
  if (!avail) tiles.push(tile("Disk (NVMe)", "unknown", `<div class="tile-value">—</div>`));
  else if (!disk.enabled) tiles.push(tile("Disk (NVMe)", "unknown", `<div class="tile-value">Disabled</div>`, `<div class="tile-sub">ENABLE_NVME=0</div>`));
  else {
    const level = disk.faulted ? "crit" : "ok";
    let value = disk.faulted ? "Fault" : "Healthy";
    if (disk.space_free_gb != null && disk.space_total_gb != null) {
      value = `${disk.space_free_gb}<span class="unit"> GB free</span>`;
    }
    const sub = [];
    if (disk.space_used_gb != null && disk.space_total_gb != null) {
      sub.push(`${disk.space_used_gb} / ${disk.space_total_gb} GB used`);
    }
    if (disk.percentage_used != null) sub.push(`wear ${disk.percentage_used}%`);
    if (disk.available_spare != null) sub.push(`spare ${disk.available_spare}%`);
    if (disk.smart_temp_c != null && disk.smart_temp_c !== "") sub.push(`${disk.smart_temp_c}°C`);
    tiles.push(tile(
      "Disk (NVMe)",
      level,
      `<div class="tile-value">${value}</div>`,
      sub.length ? `<div class="tile-sub">${sub.join(" · ")}</div>` : "",
    ));
  }

  // System / health
  if (avail) {
    tiles.push(tile("System", "ok",
      `<div class="tile-value" style="font-size:16px">${s.mpu_present ? "MPU ready" : "MPU absent"}</div>`,
      `<div class="tile-sub">up ${fmtUptime(s.uptime_sec)} · poll ${s.poll_interval_sec}s</div>`));
  } else {
    tiles.push(tile("Watchdog status API", "unknown",
      `<div class="tile-value" style="font-size:15px">Unavailable</div>`,
      `<div class="tile-sub">enable TOWER_STATUS_API / dashboard</div>`));
  }

  $("#sensor-tiles").innerHTML = tiles.join("");
}

function fmtClock(iso) {
  try { return new Date(iso).toLocaleTimeString(); } catch (_) { return iso; }
}

async function pollStatus() {
  try {
    const s = await getJSON("/api/status");
    setConn(true);
    renderStatus(s);
  } catch (_) {
    setConn(false);
    renderStatus({ available: false });
  }
}

/* -------------------------------- alerts ------------------------------- */
function alertKey(a) {
  return a.nonce || `${a.alert_type}|${a.timestamp_utc || ""}|${a.received_utc || ""}`;
}

function addAlert(a, replay) {
  const key = alertKey(a);
  if (state.seenAlertKeys.has(key)) return;
  state.seenAlertKeys.add(key);
  if (state.seenAlertKeys.size > 500) {
    state.seenAlertKeys = new Set(Array.from(state.seenAlertKeys).slice(-300));
  }

  $("#alert-empty").style.display = "none";
  const li = document.createElement("li");
  const sev = ["critical", "warning", "info"].includes(a.severity) ? a.severity : "info";
  li.className = `alert alert--${sev}`;
  const det = a.details && Object.keys(a.details).length ? JSON.stringify(a.details) : "";
  const when = a.timestamp_utc || a.received_utc;
  li.innerHTML = `
    <div class="alert-top">
      <span class="alert-type">${a.alert_type}</span>
      <span class="alert-time">${when ? fmtClock(when) : ""}</span>
    </div>
    ${det ? `<div class="alert-det">${escapeHtml(det)}</div>` : ""}`;
  const list = $("#alert-list");
  list.insertBefore(li, list.firstChild);
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

function escapeHtml(str) {
  return str.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { try { addAlert(JSON.parse(e.data)); } catch (_) {} };
  // EventSource reconnects automatically on error; nothing to do here.
}

$("#alerts-clear").addEventListener("click", () => {
  $("#alert-list").innerHTML = "";
  $("#alert-empty").style.display = "";
});

/* --------------------------------- PTZ --------------------------------- */
function currentSpeed() { return parseFloat($("#ptz-speed").value) || 0.5; }

async function ptz(method, params) {
  try {
    const r = await fetch("/api/ptz", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ method, params }),
    });
    return r.json();
  } catch (e) {
    return { ok: false, error: { code: "GATEWAY", message: String(e) } };
  }
}

function ptzFeedback(res, verb) {
  const el = $("#ptz-feedback");
  if (res && res.ok === false) {
    el.className = "ptz-feedback err";
    el.textContent = `${verb} failed: ${(res.error && res.error.message) || "error"}`;
  } else {
    el.className = "ptz-feedback ok";
    el.textContent = `${verb} ok`;
  }
}

let holdTimer = null;
let holding = false;

function pulseMove(pan, tilt, zoom) {
  const sp = currentSpeed();
  return ptz("move_continuous", {
    camera: state.activeCamera,
    pan: pan * sp, tilt: tilt * sp, zoom: zoom * sp, seconds: 0.4,
  });
}

function startHold(pan, tilt, zoom, btn) {
  if (holding) return;
  holding = true;
  if (btn) btn.classList.add("is-held");
  pulseMove(pan, tilt, zoom).then((r) => ptzFeedback(r, "move"));
  holdTimer = setInterval(() => { if (holding) pulseMove(pan, tilt, zoom); }, 380);
}

function endHold(btn) {
  if (!holding) return;
  holding = false;
  if (holdTimer) { clearInterval(holdTimer); holdTimer = null; }
  if (btn) btn.classList.remove("is-held");
  ptz("stop", { camera: state.activeCamera });
}

function wireHoldButton(btn, pan, tilt, zoom) {
  btn.addEventListener("pointerdown", (e) => { e.preventDefault(); startHold(pan, tilt, zoom, btn); });
  btn.addEventListener("pointerup", () => endHold(btn));
  btn.addEventListener("pointerleave", () => endHold(btn));
  btn.addEventListener("pointercancel", () => endHold(btn));
}

function initPtz() {
  $("#ptz-speed").addEventListener("input", (e) => ($("#ptz-speed-val").textContent = parseFloat(e.target.value).toFixed(2)));

  $$(".ptz-pad .ptz-btn").forEach((btn) => {
    if (btn.dataset.stop) {
      btn.addEventListener("click", () => ptz("stop", { camera: state.activeCamera }).then((r) => ptzFeedback(r, "stop")));
    } else {
      wireHoldButton(btn, Number(btn.dataset.pan || 0), Number(btn.dataset.tilt || 0), 0);
    }
  });
  $$(".ptz-zoom .ptz-btn").forEach((btn) => wireHoldButton(btn, 0, 0, Number(btn.dataset.zoom || 0)));
  $(".ptz-home").addEventListener("click", () => ptz("home", { camera: state.activeCamera }).then((r) => ptzFeedback(r, "home")));

  // Keyboard control (only while the feed view is visible).
  const keyMap = {
    ArrowUp: [0, 1, 0], ArrowDown: [0, -1, 0], ArrowLeft: [-1, 0, 0], ArrowRight: [1, 0, 0],
    "+": [0, 0, 1], "=": [0, 0, 1], "-": [0, 0, -1], "_": [0, 0, -1],
  };
  const held = new Set();
  document.addEventListener("keydown", (e) => {
    if ($("#view-feed").hidden) return;
    if (e.key === " ") { e.preventDefault(); endHold(); ptz("stop", { camera: state.activeCamera }); return; }
    const m = keyMap[e.key];
    if (!m || held.has(e.key)) return;
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) e.preventDefault();
    held.add(e.key);
    startHold(m[0], m[1], m[2]);
  });
  document.addEventListener("keyup", (e) => {
    if (held.has(e.key)) { held.delete(e.key); endHold(); }
  });
}

/* -------------------------------- boot --------------------------------- */
async function boot() {
  initRouter();
  initPtz();
  connectEvents();
  try {
    await loadConfig();
    setConn(true);
  } catch (_) {
    setConn(false);
  }
  pollStreams();
  pollStatus();
  setInterval(pollStreams, 3000);
  setInterval(pollStatus, 2000);
}

boot();
