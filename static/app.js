/* ────────────────────────────────────────────────────────────
 *  PASCO PS-2600A — Web GUI client
 *  WebSocket live feed + uPlot scope + Canvas2D heatmap + CIE 1931
 * ──────────────────────────────────────────────────────────── */

'use strict';

// ── Scope overlay series layout ──────────────────────────────
// All overlays are extra series on the SAME uPlot, sharing the ADC y-axis.
// scopeData index map (kept here so makeScopePlot's series list and the
// per-frame builders in handleFrame never drift out of sync):
const PERSIST_N = 6;            // fading "afterglow" trail series
const SI = {
  X:      0,                    // wavelengths
  LIVE:   1,                    // live intensity (teal, filled)
  REF:    2,                    // reference library  (amber dashed, 0.9·Ymax)
  CSVBG:  3,                    // CSV background ref (cyan dashed,  0.9·Ymax)
  FREEZE: 4,                    // frozen snapshot    (grey, absolute ADC)
  DIFF:   5,                    // live − freeze      (magenta, absolute ADC)
  ENV:    6,                    // peak-hold envelope (yellow, absolute ADC)
  TRAIL0: 7,                    // persistence trail 0..PERSIST_N-1 (faded teal)
};
const SERIES_COUNT = SI.TRAIL0 + PERSIST_N;   // 13 total (1 x + 12 y)
const emptyScopeData = () => Array.from({ length: SERIES_COUNT }, () => []);

// ── Global state ─────────────────────────────────────────────
const State = {
  ws: null,
  wsConnected: false,
  scopePlot: null,
  scopeData: emptyScopeData(),   // SERIES_COUNT arrays (see SI map above).
                             // Empty arrays, not null: uPlot reads .length on
                             // each series at construction, and null.length throws.
  referenceValues: null,     // active reference spectrum, normalised 0..1,
                             // aligned to the device wavelength axis (or null)
  referenceName: 'None',
  // Overlay cluster (client-only, reset on reload — these hold captured data
  // that can't meaningfully persist across a page load).
  overlay: { diff: false, peakHold: false, persist: false },
  freeze: null,              // { xs, ys } captured snapshot (absolute ADC)
  csvbg: null,               // { xs, ys, name } loaded-from-file background ref
  env: null,                 // Float64 per-pixel running max (peak-hold)
  persistRing: [],           // recent live frames (newest first), len ≤ PERSIST_N
  prevLive: null,            // previous frame's intensities (feeds the trail)
  measureA: null,
  measureB: null,
  measureMode: false,
  peaksOn: false,
  paused: false,
  activeTab: 'scope',
  colorSub: 'overview',      // colour tab inner sub-tab: 'overview' | 'tm30'
  xView: null,               // user wheel/drag x-zoom [min,max] (overrides config X)
  cfg: {},                   // last-known server config
  hardwareConnected: false,
  // Heatmap rolling state
  heatmapData: null,
  // Last frame for CSV export client-side
  lastFrame: null,
  // Throttled config-push
  cfgPushTimer: null,
};

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// ── Toast ────────────────────────────────────────────────────
function toast(msg, ms = 1800) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.classList.remove('show'), ms);
}

// ── WebSocket ────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  State.ws = new WebSocket(`${proto}://${location.host}/ws/stream`);
  State.ws.onopen = () => {
    State.wsConnected = true;
    $('#sb-ws').textContent = 'connected';
  };
  State.ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'hello') handleHello(msg);
      else if (msg.type === 'frame') handleFrame(msg);
      else if (msg.type === 'config' && msg.config) {
        // Live server-driven config update (e.g. auto-exposure). Merge and
        // reflect in the UI without echoing back to the server.
        Object.assign(State.cfg, msg.config);
        applyConfigToUI(State.cfg);
      }
    } catch (e) {
      console.warn('Bad WS message', e);
    }
  };
  State.ws.onclose = () => {
    State.wsConnected = false;
    $('#sb-ws').textContent = 'reconnecting…';
    setTimeout(connectWS, 1500);
  };
  State.ws.onerror = () => State.ws.close();
}

function sendWS(obj) {
  if (State.ws && State.ws.readyState === 1) {
    State.ws.send(JSON.stringify(obj));
  }
}

function handleHello(msg) {
  State.cfg = msg.config || {};
  State.hardwareConnected = !!msg.connected;
  applyConfigToUI(State.cfg);
  updateConnectButton();
  loadReferenceNames();

  // Populate backend dropdown
  const bsel = $('#backend-select');
  const backends = msg.backends || [];
  if (backends.length > 0 && bsel.options.length === 0) {
    bsel.innerHTML = '';
    for (const b of backends) bsel.appendChild(new Option(b, b));
  }
  if (msg.backend && bsel.value !== msg.backend) bsel.value = msg.backend;

  // Populate device dropdown
  const sel = $('#device-select');
  const devices = msg.devices || [];
  if (devices.length > 0) {
    sel.innerHTML = '';
    for (const path of devices) sel.appendChild(new Option(path, path));
    if (msg.device_path) sel.value = msg.device_path;
    $('#btn-connect').disabled = false;
  } else if (msg.device_path) {
    sel.innerHTML = '';
    sel.appendChild(new Option(msg.device_path, msg.device_path));
    sel.value = msg.device_path;
    $('#btn-connect').disabled = false;
  }
}

function handleFrame(msg) {
  State.lastFrame = msg;
  if (msg.color) State.lastColor = msg.color;

  // Top-bar live indicators
  $('#livepill').className = 'livepill ' + (msg.fps > 0 ? 'ok' : 'idle');
  $('#livepill').textContent = msg.fps > 0 ? `● ${msg.fps.toFixed(1)} Hz` : '● Idle';
  $('#conn-dot').classList.add('ok');
  $('#stat-fps').innerHTML = `${msg.fps.toFixed(1)}<span class="unit"> Hz</span>`;
  $('#stat-exp').innerHTML = `${msg.integration_ms.toFixed(0)}<span class="unit"> ms</span>`;
  $('#stat-dark').innerHTML = `${msg.ob_mean.toFixed(1)}<span class="unit"> ADC</span>`;
  $('#sb-integ').textContent = `${msg.integration_ms.toFixed(1)} ms`;
  $('#sb-dark').textContent  = `${msg.ob_mean.toFixed(2)} ADC`;

  // Device-aware dark readout. PASCO has optical-black pixels → show "OB mean"
  // plus the parametric Bias/Rate fields. LR-2T / HDX have no OB → the value is
  // an estimated baseline ("Baseline"); hide Bias/Rate since they don't apply.
  const supportsOb = msg.supports_ob !== false;   // default true if absent
  const darkK = $('#sb-dark-k');
  if (darkK) darkK.textContent = msg.dark_label || 'Dark';
  const heroLbl = $('#stat-dark-lbl');
  if (heroLbl) heroLbl.textContent = supportsOb ? 'OB MEAN' : 'BASELINE';
  const showBR = (el, on) => { if (el) el.style.display = on ? '' : 'none'; };
  showBR($('#sb-bias-wrap'),  supportsOb);
  showBR($('#sb-bias-sep'),   supportsOb);
  showBR($('#sb-rate-wrap'),  supportsOb);
  showBR($('#sb-rate-sep'),   supportsOb);
  if (supportsOb) {
    const bias = State.cfg.dark_bias_adc, rate = State.cfg.dark_rate_adc_per_s;
    if ($('#sb-bias') && bias != null) $('#sb-bias').textContent = `${Number(bias).toFixed(2)} ADC`;
    if ($('#sb-rate') && rate != null) $('#sb-rate').textContent = `${Number(rate).toFixed(2)} ADC/s`;
  }

  // Sensor chip
  const sensorEl = $('#sb-sensor');
  if (msg.sensor_temp_delta == null) {
    sensorEl.className = 'chip neutral';
    sensorEl.textContent = 'Sensor: —';
  } else {
    const d = msg.sensor_temp_delta;
    let kind = 'ok', label = 'cold baseline';
    if (d >= 4)      { kind = 'danger'; label = 'warm';          }
    else if (d >= 1) { kind = 'warn';   label = 'slightly warm'; }
    sensorEl.className = 'chip ' + kind;
    sensorEl.textContent = `Sensor: ${d >= 0 ? '+' : ''}${d.toFixed(1)} ADC · ${label}`;
  }

  // Keep the latest spectrum in scopeData regardless of active tab (the
  // heatmap and CSV export read it too).
  State.scopeData[0] = msg.wavelengths;
  State.scopeData[1] = msg.intensities;
  // Reference overlay: scale the normalised (0..1) library spectrum to 90% of
  // the current Y maximum so it always fits the displayed signal. Aligned to
  // the device axis at fetch time; null-filled (drawn as a gap) when off or on
  // a length mismatch (e.g. mid device-switch, before re-fetch).
  State.scopeData[SI.REF] = buildReferenceSeries(msg.intensities);
  // CSV background, freeze, difference, peak-hold envelope and the persistence
  // trail — all computed here so a single setData() draws everything in step.
  updateOverlays(msg.intensities);

  // Scope plot
  if (State.scopePlot && State.activeTab === 'scope') {
    // setData() with the default resetScales=true does BOTH things we need
    // every frame: it rebuilds the series path (so the line/fill are actually
    // drawn) and it re-runs the x/y range functions (so the Y axis auto-fits
    // when auto_y is on). We deliberately do NOT call setScale() here — an
    // explicit setScale pins the scale and stops uPlot's per-frame
    // auto-ranging (uPlot issue #1001). A previous attempt used
    // setData(data,false) inside batch() + setScale; that left the path
    // un-rebuilt, so the axes scaled but no line was drawn.
    State.scopePlot.setData(State.scopeData);
    drawPeaksOverlay(msg.peaks);

    // One-time refit: if the plot was constructed before the flex layout had
    // a height, the first real frame is a good moment to size it correctly.
    if (!State._scopeFitted) {
      const c = $('#scope-plot');
      if (c.clientWidth > 0 && c.clientHeight > 0) {
        State.scopePlot.setSize({ width: c.clientWidth, height: c.clientHeight });
        State._scopeFitted = true;
      }
    }
  }

  // Heatmap — accumulate history every frame so switching to the tab shows
  // recent data; rollHeatmap only repaints when the heatmap tab is active.
  rollHeatmap(msg.intensities);

  // Color tab
  if (State.activeTab === 'color' && msg.color) {
    // Only refresh the visible sub-tab: redrawing the hidden Overview (CIE
    // diagram canvas + CRI bars) every frame while on TM-30 wasted a lot of CPU
    // on high-DPI screens. TM-30 itself is fetched throttled.
    if (State.colorSub === 'tm30') Tm30.onFrame();
    else updateColorTab(msg.color);
  }

  // Filter tab — keep the latest frame so a baseline can be captured, and drive
  // the live transmission trace when the tab is open.
  Filter.onFrame(msg.wavelengths, msg.intensities);
}

// ── Scope plot (uPlot) ───────────────────────────────────────
// The full series list in SI order. Every overlay series is null-filled when
// inactive (see handleFrame) so uPlot draws nothing for it; no series are ever
// added/removed at runtime (uPlot fixes the count at construction).
function buildScopeSeries() {
  const s = [];
  s[SI.X] = {};
  s[SI.LIVE] = {
    stroke: '#2dd4bf',          // fixed bright teal — visible on both themes
    width: 2,
    spanGaps: true,
    fill: makeSpectrumFill,     // wavelength-colored area fill under the curve
    points: { show: false },
  };
  s[SI.REF] = {                 // reference library: amber dashed, 0.9·Ymax
    stroke: '#f59e0b', width: 1.5, dash: [6, 4], spanGaps: true,
    points: { show: false },
  };
  s[SI.CSVBG] = {               // CSV background reference: cyan dashed, 0.9·Ymax
    stroke: '#38bdf8', width: 1.5, dash: [2, 4], spanGaps: true,
    points: { show: false },
  };
  s[SI.FREEZE] = {              // frozen snapshot: grey solid, absolute ADC
    stroke: '#9aa0a6', width: 1.5, spanGaps: true, points: { show: false },
  };
  s[SI.DIFF] = {                // live − freeze: magenta, absolute ADC (signed)
    stroke: '#e879f9', width: 1.5, spanGaps: true, points: { show: false },
  };
  s[SI.ENV] = {                 // peak-hold envelope: yellow, absolute ADC
    stroke: '#facc15', width: 1, spanGaps: true, points: { show: false },
  };
  // Persistence trails: oldest faintest. Teal with decaying alpha.
  for (let k = 0; k < PERSIST_N; k++) {
    const a = 0.42 * (1 - k / PERSIST_N);   // 0.42 → ~0.07
    s[SI.TRAIL0 + k] = {
      stroke: `rgba(45,212,191,${a.toFixed(3)})`,
      width: 1, spanGaps: false, points: { show: false },
    };
  }
  return s;
}

function makeScopePlot() {
  const container = $('#scope-plot');
  const opts = {
    width: container.clientWidth,
    height: container.clientHeight,
    pxAlign: false,
    cursor: { drag: { x: true, y: false }, points: { size: 6 } },
    select: { show: true },
    legend: { show: false },
    axes: [
      {
        stroke: getCss('--fg-3'),
        grid: { stroke: getCss('--border-1'), width: 1 },
        ticks: { stroke: getCss('--border-2') },
        font: '11px JetBrains Mono',
        labelFont: '10px Inter',
        label: 'Wavelength (nm)',
        // Numeric wavelength labels, never time-of-day.
        values: (u, splits) => splits.map(v => v.toFixed(0)),
      },
      {
        stroke: getCss('--fg-3'),
        grid: { stroke: getCss('--border-1'), width: 1 },
        ticks: { stroke: getCss('--border-2') },
        font: '11px JetBrains Mono',
        labelFont: '10px Inter',
        label: 'Intensity (ADC)',
      },
    ],
    series: buildScopeSeries(),
    scales: {
      // x is WAVELENGTH (nm), not time. Without time:false uPlot renders the
      // x-axis as Unix timestamps (the "1:07am 1/1/70" axis) and mis-places
      // every point.
      x: { time: false, range: () => xRange() },
      y: { range: () => autoYRange() },
    },
    plugins: [
      hoverReadoutPlugin(),
      measureModePlugin(),
      wheelZoomPlugin(),
    ],
  };
  // Build with a guaranteed non-zero size. During init the flex layout may
  // not have resolved yet, so clientWidth/Height can be 0 — a 0-height uPlot
  // canvas draws nothing (the "invisible spectrum" symptom). Fall back to
  // sensible sizes, then a ResizeObserver fits it once layout settles and on
  // every later resize / tab switch.
  const w0 = container.clientWidth  || 800;
  const h0 = container.clientHeight || 480;
  opts.width = w0;
  opts.height = h0;
  State.scopePlot = new uPlot(opts, State.scopeData, container);

  const fitScope = () => {
    if (!State.scopePlot) return;
    const w = container.clientWidth, h = container.clientHeight;
    if (w > 0 && h > 0 &&
        (w !== State.scopePlot.width || h !== State.scopePlot.height)) {
      State.scopePlot.setSize({ width: w, height: h });
    }
  };
  // Fit after the first paint (when flex heights exist) and keep fitted.
  requestAnimationFrame(fitScope);
  if (window.ResizeObserver) {
    State._scopeRO = new ResizeObserver(fitScope);
    State._scopeRO.observe(container);
  }
  window.addEventListener('resize', fitScope);
}

function autoYRange() {
  // Signed extent across the live trace plus any ABSOLUTE-ADC overlays that can
  // exceed it (freeze, peak-hold, signed difference). The 0.9·Ymax overlays
  // (reference / CSV bg) are excluded on purpose — they are scaled TO this
  // range, so including them would be circular.
  let mx = 0, mn = 0;
  const consider = (arr) => {
    if (!arr) return;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (v == null || !isFinite(v)) continue;
      if (v > mx) mx = v;
      if (v < mn) mn = v;
    }
  };
  consider(State.scopeData[SI.LIVE]);
  if (State.freeze)           consider(State.scopeData[SI.FREEZE]);
  if (State.overlay.peakHold) consider(State.scopeData[SI.ENV]);
  if (State.overlay.diff)     consider(State.scopeData[SI.DIFF]);   // may be negative
  const lo = Math.min(-20, mn * 1.1);
  if (State.cfg.auto_y === false) {
    return [lo, State.cfg.y_max_adc || 4000];
  }
  const live = State.scopeData[SI.LIVE];
  if ((!live || live.length === 0) && !State.freeze) return [lo, 4000];
  return [lo, Math.max(50, mx * 1.1)];
}

function configXRange() {
  // The Display X min / X max config. Fall back to the data extent, then to a
  // wide default — never to a time axis.
  const lo = Number(State.cfg.x_min_nm);
  const hi = Number(State.cfg.x_max_nm);
  if (isFinite(lo) && isFinite(hi) && hi > lo) return [lo, hi];
  const xs = State.scopeData[0];
  if (xs && xs.length) return [xs[0], xs[xs.length - 1]];
  return [380, 1050];
}

function xRange() {
  // A user wheel/drag zoom (State.xView) overrides the config range until it's
  // reset (double-click, or editing the X-range fields). setData(resetScales=
  // true) re-runs this every frame, so returning xView here makes the zoom
  // survive live updates instead of snapping back.
  return State.xView || configXRange();
}

// Mouse-wheel zoom on the x (wavelength) axis, centred on the cursor, matching
// the desktop app. Writes State.xView so the zoom persists across frames; the
// shared State.xView also keeps the Scope and Filter plots on the same span.
// Double-click resets to the configured range.
function wheelZoomPlugin() {
  const STEP = 0.88;   // span multiplier per wheel notch (in = shrink span)
  return { hooks: { ready: u => {
    const over = u.over;
    over.addEventListener('wheel', (e) => {
      e.preventDefault();
      let lo = u.scales.x.min, hi = u.scales.x.max;
      if (!isFinite(lo) || !isFinite(hi)) { const r = xRange(); lo = r[0]; hi = r[1]; }
      const cl = u.cursor.left;
      const piv = (cl != null && cl >= 0) ? u.posToVal(cl, 'x') : (lo + hi) / 2;
      const f = e.deltaY < 0 ? STEP : 1 / STEP;
      const nlo = piv - (piv - lo) * f;
      const nhi = piv + (hi - piv) * f;
      if (nhi - nlo < 1) return;            // don't zoom past a 1 nm span
      State.xView = [nlo, nhi];
      u.setScale('x', { min: nlo, max: nhi });
    }, { passive: false });
    over.addEventListener('dblclick', () => {
      State.xView = null;
      const r = configXRange();
      u.setScale('x', { min: r[0], max: r[1] });
    });
  }}};
}

function getCss(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

// Resize a canvas's backing store to its displayed CSS size × devicePixelRatio
// and return a context scaled so all drawing can use CSS-pixel coordinates.
// Without this, canvases with a fixed width/height attribute look blurry once
// CSS stretches them to fill a larger box. Returns null if not laid out yet.
function prepCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.getAttribute('width') | 0;
  const cssH = canvas.clientHeight || canvas.getAttribute('height') | 0;
  if (cssW <= 0 || cssH <= 0) return null;
  const needW = Math.round(cssW * dpr), needH = Math.round(cssH * dpr);
  let resized = false;
  if (canvas.width !== needW || canvas.height !== needH) {
    canvas.width = needW; canvas.height = needH; resized = true;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, W: cssW, H: cssH, dpr, resized };
}

// ── Reference library overlay ────────────────────────────────
// The library lives in a shared file (reference_spectra.csv) that the server
// also uses for the desktop app; the server interpolates a chosen spectrum
// onto the active device axis and returns it normalised 0..1. We scale it to
// 90% of the current Y max each frame so it always fits the live signal.
function buildReferenceSeries(intensities) {
  const ref = State.referenceValues;
  const n = intensities ? intensities.length : 0;
  if (!ref || ref.length !== n || n === 0) return new Array(n).fill(null);
  const yhi = autoYRange()[1];
  const scale = Math.max(yhi * 0.9, 50);
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = ref[i] * scale;
  return out;
}

async function loadReferenceNames() {
  const sel = $('#cfg-reference_library');
  if (!sel) return;
  try {
    const r = await fetch('/api/references').then(r => r.json());
    const names = r.names || [];
    const current = State.cfg.reference_library || 'None';
    sel.innerHTML = '';
    sel.appendChild(new Option('None', 'None'));
    for (const n of names) sel.appendChild(new Option(n, n));
    sel.value = (current === 'None' || names.includes(current)) ? current : 'None';
    await applyReference(sel.value);
  } catch (e) { /* library not generated yet — leave the default option */ }
}

async function applyReference(name) {
  State.referenceName = name || 'None';
  if (!name || name === 'None') {
    State.referenceValues = null;
    refreshScopeOverlay();
    return;
  }
  try {
    const r = await fetch('/api/reference?name=' + encodeURIComponent(name))
      .then(r => r.json());
    State.referenceValues = (r.values && r.values.length)
      ? Float64Array.from(r.values) : null;
  } catch (e) {
    State.referenceValues = null;
    toast('Reference load failed');
  }
  refreshScopeOverlay();
}

// ── Overlay cluster engine ───────────────────────────────────
// All overlays live on the scope uPlot and share its ADC y-axis. Two entry
// points: updateOverlays() advances stateful overlays on a NEW frame (peak-hold
// max, persistence ring) then rebuilds; rebuildOverlays() only recomputes the
// derived series from current state (used when toggling a button while paused).

function updateOverlays(live) {
  const n = live ? live.length : 0;
  // Peak-hold: per-pixel running maximum.
  if (State.overlay.peakHold) {
    if (!State.env || State.env.length !== n) State.env = Float64Array.from(live);
    else for (let i = 0; i < n; i++) if (live[i] > State.env[i]) State.env[i] = live[i];
  }
  // Persistence: push the PREVIOUS frame into the trail ring (newest first).
  if (State.overlay.persist && State.prevLive && State.prevLive.length === n) {
    State.persistRing.unshift(State.prevLive);
    if (State.persistRing.length > PERSIST_N) State.persistRing.length = PERSIST_N;
  }
  State.prevLive = n ? Float64Array.from(live) : null;
  rebuildOverlays(live);
}

function rebuildOverlays(live) {
  const n = live ? live.length : 0;
  const nullArr = () => new Array(n).fill(null);

  // CSV background reference (cyan) — interpolated onto the live axis,
  // normalised, scaled to 90% of the current Y max (like the library overlay).
  State.scopeData[SI.CSVBG] = State.csvbg ? buildCsvBgSeries(live) : nullArr();

  // Frozen snapshot (grey) — kept in absolute ADC so drift against the live
  // trace is visible. Re-aligned to the current axis if the device changed.
  const fz = State.freeze ? freezeAligned(live) : null;
  State.scopeData[SI.FREEZE] = fz || nullArr();

  // Difference live − freeze (magenta), absolute ADC, signed.
  if (State.overlay.diff && fz && n) {
    const d = new Array(n);
    for (let i = 0; i < n; i++) {
      const a = live[i], b = fz[i];
      d[i] = (a == null || b == null || !isFinite(a) || !isFinite(b)) ? null : a - b;
    }
    State.scopeData[SI.DIFF] = d;
  } else {
    State.scopeData[SI.DIFF] = nullArr();
  }

  // Peak-hold envelope (yellow).
  State.scopeData[SI.ENV] =
    (State.overlay.peakHold && State.env && State.env.length === n)
      ? Array.from(State.env) : nullArr();

  // Persistence trails (faded teal echoes of recent frames).
  for (let k = 0; k < PERSIST_N; k++) {
    const f = State.persistRing[k];
    State.scopeData[SI.TRAIL0 + k] =
      (State.overlay.persist && f && f.length === n) ? f : nullArr();
  }
}

function refreshScopeOverlay() {
  if (!State.scopePlot) return;
  const live = State.scopeData[SI.LIVE];
  State.scopeData[SI.REF] = buildReferenceSeries(live);
  rebuildOverlays(live);
  if (State.activeTab === 'scope') State.scopePlot.setData(State.scopeData);
}

// Linear interpolation of (sx, sy) onto the dstXs axis. Handles ascending or
// descending source; returns null outside the source range (drawn as a gap).
function interpToAxis(sx, sy, dx) {
  const n = dx.length, m = sx.length, out = new Array(n);
  if (m === 0) { out.fill(null); return out; }
  const descending = m > 1 && sx[m - 1] < sx[0];
  // Work on an ascending view.
  const X = descending ? Float64Array.from(sx).reverse() : sx;
  const Y = descending ? Float64Array.from(sy).reverse() : sy;
  const xLo = X[0], xHi = X[m - 1];
  for (let i = 0; i < n; i++) {
    const x = dx[i];
    if (x < xLo || x > xHi) { out[i] = null; continue; }
    // binary search for the bracket
    let lo = 0, hi = m - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (X[mid] <= x) lo = mid; else hi = mid; }
    const x0 = X[lo], x1 = X[hi];
    const t = (x1 === x0) ? 0 : (x - x0) / (x1 - x0);
    out[i] = Y[lo] * (1 - t) + Y[hi] * t;
  }
  return out;
}

function freezeAligned(live) {
  const xs = State.scopeData[SI.X];
  const f = State.freeze;
  if (!f || !xs || !xs.length) return null;
  if (f.ys.length === live.length) return f.ys;   // same device/axis: use as-is
  return interpToAxis(f.xs, f.ys, xs);             // device changed: resample
}

function buildCsvBgSeries(live) {
  const xs = State.scopeData[SI.X];
  const n = live.length;
  if (!State.csvbg || !xs || xs.length !== n || n === 0) return new Array(n).fill(null);
  const interp = interpToAxis(State.csvbg.xs, State.csvbg.ys, xs);
  let mx = 0;
  for (const v of interp) if (v != null && isFinite(v) && v > mx) mx = v;
  if (mx <= 0) return new Array(n).fill(null);
  const scale = Math.max(autoYRange()[1] * 0.9, 50) / mx;
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = (interp[i] == null) ? null : interp[i] * scale;
  return out;
}

// Tolerant 2-column spectrum parser (λ, intensity). Accepts ';'/','/tab/space
// delimiters, optional header/comment lines, and 3-column Pixel;λ;ADC dumps
// (uses the last two columns). Mirrors the Replay single-spectrum branch.
function parseTwoColCsv(text) {
  const lines = text.split(/\r?\n/).filter(l => l.trim().length && !l.trim().startsWith('#'));
  if (!lines.length) throw new Error('empty file');
  const sample = lines[0];
  let delim;
  if (sample.indexOf(';') >= 0)       delim = ';';
  else if (sample.indexOf('\t') >= 0) delim = '\t';
  else if (sample.indexOf(',') >= 0)  delim = ',';
  else                                delim = /\s+/;
  const split = (l) => l.trim().split(delim);
  const first = split(lines[0]).map(s => s.trim());
  const firstIsHeader = first.some(s => /[a-zA-Z]/.test(s));
  const xs = [], ys = [];
  for (let li = firstIsHeader ? 1 : 0; li < lines.length; li++) {
    const p = split(lines[li]);
    if (p.length < 2) continue;
    const w = parseFloat(p[p.length - 2]);
    const v = parseFloat(p[p.length - 1]);
    if (!isFinite(w) || !isFinite(v)) continue;
    xs.push(w); ys.push(v);
  }
  if (!xs.length) throw new Error('no numeric rows');
  return { xs: Float64Array.from(xs), ys: Float64Array.from(ys) };
}

// ── Overlay toolbar handlers ─────────────────────────────────
function setOverlay(key, on) {
  State.overlay[key] = on;
  if (key === 'peakHold') State.env = null;             // (re)arm on any toggle
  if (key === 'persist' && !on) State.persistRing = [];
  syncOverlayButtons();
  refreshScopeOverlay();
}

function toggleFreeze() {
  if (State.freeze) {
    State.freeze = null;
    if (State.overlay.diff) State.overlay.diff = false;  // diff needs a freeze
  } else {
    const live = State.scopeData[SI.LIVE], xs = State.scopeData[SI.X];
    if (!live || !live.length) { toast('No live spectrum to freeze'); return; }
    State.freeze = { xs: Float64Array.from(xs), ys: Float64Array.from(live) };
  }
  syncOverlayButtons();
  refreshScopeOverlay();
}

function toggleDiff() {
  if (!State.overlay.diff && !State.freeze) { toast('Freeze a reference first'); return; }
  setOverlay('diff', !State.overlay.diff);
}
function togglePeakHold() { setOverlay('peakHold', !State.overlay.peakHold); }
function togglePersist()  { setOverlay('persist',  !State.overlay.persist); }

function onCsvBgFile(ev) {
  const f = ev.target.files && ev.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = parseTwoColCsv(String(reader.result));
      State.csvbg = { xs: parsed.xs, ys: parsed.ys, name: f.name };
      const lbl = $('#csvbg-name'); if (lbl) lbl.textContent = f.name;
      toast(`Background: ${f.name} (${parsed.xs.length} pts)`);
      syncOverlayButtons();
      refreshScopeOverlay();
    } catch (e) { toast('CSV parse failed: ' + e.message); }
  };
  reader.onerror = () => toast('File read failed');
  reader.readAsText(f);
  ev.target.value = '';   // allow re-loading the same file
}

function clearCsvBg() {
  State.csvbg = null;
  const lbl = $('#csvbg-name'); if (lbl) lbl.textContent = '';
  syncOverlayButtons();
  refreshScopeOverlay();
}

function syncOverlayButtons() {
  const set = (id, on) => { const b = $(id); if (b) b.classList.toggle('on', !!on); };
  set('#btn-freeze',   !!State.freeze);
  set('#btn-diff',     State.overlay.diff);
  set('#btn-peakhold', State.overlay.peakHold);
  set('#btn-persist',  State.overlay.persist);
  const fb = $('#btn-freeze');
  if (fb) fb.innerHTML = State.freeze ? '✕&nbsp; FROZEN' : '❄&nbsp; FREEZE';
  const cc = $('#btn-csvbg-clear');
  if (cc) cc.style.display = State.csvbg ? '' : 'none';
}


function makeSpectrumFill(u, seriesIdx) {
  try {
    const { ctx } = u;
    const canW = u.bbox && u.bbox.width;
    if (!ctx || !canW || canW <= 0) return 'rgba(120,180,255,0.18)';
    const xLo = u.scales.x.min;
    const xHi = u.scales.x.max;
    const left = u.bbox.left;
    if (!isFinite(xLo) || !isFinite(xHi) || xHi <= xLo)
      return 'rgba(120,180,255,0.18)';
    const grad = ctx.createLinearGradient(left, 0, left + canW, 0);
    const stops = 24;
    for (let i = 0; i <= stops; i++) {
      const t = i / stops;
      const wl = xLo + t * (xHi - xLo);
      const [r, g, b] = wavelengthToRGB(wl);
      grad.addColorStop(t, `rgba(${r},${g},${b},0.55)`);
    }
    return grad;
  } catch (e) {
    return 'rgba(120,180,255,0.18)';
  }
}

function wavelengthToRGB(nm) {
  let r = 0, g = 0, b = 0;
  if (nm < 380 || nm > 780) return [90, 90, 115];
  if (nm < 440) { r = -(nm - 440) / 60; b = 1; }
  else if (nm < 490) { g = (nm - 440) / 50; b = 1; }
  else if (nm < 510) { g = 1; b = -(nm - 510) / 20; }
  else if (nm < 580) { r = (nm - 510) / 70; g = 1; }
  else if (nm < 645) { r = 1; g = -(nm - 645) / 65; }
  else { r = 1; }
  let factor = 1;
  if (nm < 420) factor = 0.3 + 0.7 * (nm - 380) / 40;
  else if (nm > 700) factor = 0.3 + 0.7 * (780 - nm) / 80;
  return [
    Math.round(255 * Math.pow(Math.max(0, r) * factor, 0.8)),
    Math.round(255 * Math.pow(Math.max(0, g) * factor, 0.8)),
    Math.round(255 * Math.pow(Math.max(0, b) * factor, 0.8)),
  ];
}

// Hover crosshair → updates the top-bar readout cells.
function hoverReadoutPlugin() {
  return {
    hooks: {
      setCursor(u) {
        if (u.cursor.idx == null) {
          $('#ro-pixel').textContent = '—';
          $('#ro-wave').innerHTML  = '—<span class="unit"> nm</span>';
          $('#ro-int').innerHTML   = '—<span class="unit"> ADC</span>';
          return;
        }
        const idx = u.cursor.idx;
        const wl = State.scopeData[0]?.[idx];
        const it = State.scopeData[1]?.[idx];
        if (wl == null || it == null) return;
        $('#ro-pixel').textContent = idx;
        $('#ro-wave').innerHTML  = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
        $('#ro-int').innerHTML   = `${it.toFixed(0)}<span class="unit"> ADC</span>`;
      },
    },
  };
}

// Peak labels overlay — drawn after the curve.
function drawPeaksOverlay(peaks) {
  if (!State.scopePlot || !peaks) return;
  const u = State.scopePlot;
  const over = u.over;
  // Remove old peak labels
  over.querySelectorAll('.peak-label').forEach(e => e.remove());
  if (!State.peaksOn) return;
  for (const p of peaks) {
    const x = u.valToPos(p.wavelength_nm, 'x');
    const y = u.valToPos(p.intensity_adc, 'y');
    const dot = document.createElement('div');
    dot.className = 'peak-label';
    dot.style.cssText = `
      position: absolute; left: ${x}px; top: ${y - 28}px;
      transform: translateX(-50%);
      color: var(--fg-1); background: rgba(0,0,0,0.55);
      padding: 2px 6px; border: 1px solid var(--accent); border-radius: 3px;
      font: 500 10pt JetBrains Mono; pointer-events: none;
      white-space: nowrap;
    `;
    dot.textContent = `${p.wavelength_nm.toFixed(1)} nm`;
    over.appendChild(dot);
    // Marker dot
    const marker = document.createElement('div');
    marker.className = 'peak-label';
    marker.style.cssText = `
      position: absolute; left: ${x - 5}px; top: ${y - 5}px;
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--accent); border: 2px solid var(--bg-0);
      pointer-events: none;
    `;
    over.appendChild(marker);
  }
}

// Measure mode — two draggable vertical lines.
function measureModePlugin() {
  let lineA, lineB, dragging = null;
  let measureBox;
  return {
    hooks: {
      init(u) {
        const over = u.over;
        // Create the two cursor elements lazily
        const mk = (color) => {
          const el = document.createElement('div');
          el.style.cssText = `
            position: absolute; top: 0; bottom: 0; width: 14px;
            margin-left: -7px;
            cursor: ew-resize; z-index: 5; display: none;
          `;
          const inner = document.createElement('div');
          inner.style.cssText = `
            position: absolute; left: 6px; top: 0; bottom: 0;
            width: 2px; background: ${color};
            border-left: 1px dashed ${color};
          `;
          el.appendChild(inner);
          return el;
        };
        lineA = mk(getCss('--measure-a'));
        lineB = mk(getCss('--measure-b'));
        over.appendChild(lineA);
        over.appendChild(lineB);

        measureBox = document.createElement('div');
        measureBox.style.cssText = `
          position: absolute; left: 50%; top: 12px;
          transform: translateX(-50%);
          background: rgba(0,0,0,0.55);
          color: var(--warn); padding: 4px 10px;
          border: 1px solid var(--warn); border-radius: 4px;
          font: 500 11px JetBrains Mono; pointer-events: none;
          display: none; z-index: 6;
        `;
        over.appendChild(measureBox);

        const startDrag = (which) => (e) => {
          dragging = which;
          e.preventDefault(); e.stopPropagation();
        };
        lineA.addEventListener('mousedown',  startDrag('A'));
        lineB.addEventListener('mousedown',  startDrag('B'));
        lineA.addEventListener('touchstart', startDrag('A'), { passive: false });
        lineB.addEventListener('touchstart', startDrag('B'), { passive: false });

        const move = (clientX) => {
          if (!dragging) return;
          const rect = over.getBoundingClientRect();
          const x = clientX - rect.left;
          const wl = u.posToVal(x, 'x');
          if (dragging === 'A') State.measureA = wl;
          else                  State.measureB = wl;
          renderLines();
          pushCfg({ [dragging === 'A' ? 'measure_a_nm' : 'measure_b_nm']: wl });
        };
        window.addEventListener('mousemove', (e) => move(e.clientX));
        window.addEventListener('mouseup',   () => dragging = null);
        window.addEventListener('touchmove', (e) => {
          if (dragging && e.touches[0]) {
            move(e.touches[0].clientX); e.preventDefault();
          }
        }, { passive: false });
        window.addEventListener('touchend',  () => dragging = null);
      },
      draw(u) { renderLines(); },
      setSize(u) { renderLines(); },
    },
  };

  function renderLines() {
    const u = State.scopePlot;
    if (!u || !lineA || !lineB) return;
    if (!State.measureMode) {
      lineA.style.display = lineB.style.display = measureBox.style.display = 'none';
      return;
    }
    if (State.measureA == null) State.measureA = State.cfg.measure_a_nm || 500;
    if (State.measureB == null) State.measureB = State.cfg.measure_b_nm || 600;
    const xa = u.valToPos(State.measureA, 'x');
    const xb = u.valToPos(State.measureB, 'x');
    lineA.style.display = lineB.style.display = '';
    lineA.style.left = `${xa}px`;
    lineB.style.left = `${xb}px`;
    measureBox.style.display = '';
    const dx = Math.abs(State.measureB - State.measureA);
    const ws = State.scopeData[0], vs = State.scopeData[1];
    let yA = 0, yB = 0;
    if (ws && vs) {
      const findY = (wl) => {
        if (!ws.length) return 0;
        let bestI = 0, bestD = Math.abs(ws[0] - wl);
        for (let i = 1; i < ws.length; i++) {
          const d = Math.abs(ws[i] - wl);
          if (d < bestD) { bestD = d; bestI = i; }
        }
        return vs[bestI];
      };
      yA = findY(State.measureA);
      yB = findY(State.measureB);
    }
    const dy = Math.abs(yB - yA);
    measureBox.innerHTML =
      `A ${State.measureA.toFixed(1)} · B ${State.measureB.toFixed(1)} · ` +
      `Δλ ${dx.toFixed(2)} nm · Δy ${dy.toFixed(0)} ADC`;
  }
}

// ── Heatmap (Canvas2D rolling buffer) ────────────────────────
// We keep a logical history of the last N frames, each stored at the FULL
// incoming spectrum resolution (no 512-column down-mix) together with the
// frame's wavelength axis, so the waterfall shows the device's native line
// resolution. The canvas is fully redrawn from this history every frame, so
// resizing never wipes data and the buffer always scrolls.
const HM_ROWS = 240;        // frames of history kept (taller waterfall)

function initHeatmap() {
  State.hm = {
    rows: [],               // [{vals:Float32Array(N) normalized, } ...] newest first
    xMin: null, xMax: null, // wavelength span the rows were normalized against
    cursor: null,           // {x,y} in CSS px while hovering, else null
    ro: null,
  };
  const canvas = $('#heatmap-canvas');
  const fit = () => { redrawHeatmap(); };
  if (window.ResizeObserver) {
    State.hm.ro = new ResizeObserver(fit);
    State.hm.ro.observe(canvas);
  }
  window.addEventListener('resize', fit);

  // Measuring crosshair: mouse over the waterfall shows wavelength (from the
  // x position) and the intensity of the hovered frame/wavelength, mirroring
  // the desktop app's heatmap cursor.
  canvas.addEventListener('mousemove', (ev) => {
    const rect = canvas.getBoundingClientRect();
    State.hm.cursor = { x: ev.clientX - rect.left, y: ev.clientY - rect.top,
                        w: rect.width, h: rect.height };
    redrawHeatmap();
  });
  canvas.addEventListener('mouseleave', () => {
    State.hm.cursor = null;
    $('#ro-pixel').textContent = '—';
    $('#ro-wave').innerHTML = '—<span class="unit"> nm</span>';
    $('#ro-int').innerHTML  = '—<span class="unit"> ADC</span>';
    redrawHeatmap();
  });
}

function rollHeatmap(intensities) {
  const xs = State.scopeData[0];
  const ys = intensities;
  if (!xs || !ys || xs.length === 0) return;
  if (!State.hm) initHeatmap();

  const xMin = (isFinite(State.cfg.x_min_nm) ? State.cfg.x_min_nm : xs[0]);
  const xMax = (isFinite(State.cfg.x_max_nm) ? State.cfg.x_max_nm : xs[xs.length - 1]);
  const yMax = State.cfg.auto_y === false
    ? (State.cfg.y_max_adc || 4000)
    : Math.max(50, Math.max(...ys) * 1.1);

  // Store the full-resolution normalized row plus its raw values + axis so the
  // crosshair can report true ADC and wavelength.
  const N = ys.length;
  const norm = new Float32Array(N);
  for (let i = 0; i < N; i++) norm[i] = Math.max(0, Math.min(1, ys[i] / yMax));

  State.hm.xMin = xMin; State.hm.xMax = xMax;
  State.hm.rows.unshift({ norm, raw: ys, xs });   // index 0 = newest (drawn at bottom)
  if (State.hm.rows.length > HM_ROWS) State.hm.rows.length = HM_ROWS;

  if (State.activeTab === 'heatmap') redrawHeatmap();
}

// For a canvas x pixel (0..cw) return the nearest sample index into a row's
// wavelength axis, given the display wavelength span.
function _hmIndexForX(px, cw, xs, xMin, xMax) {
  const wl = xMin + (xMax - xMin) * (px / Math.max(1, cw - 1));
  // binary-ish nearest search (xs ascending)
  let lo = 0, hi = xs.length - 1;
  if (wl <= xs[0]) return 0;
  if (wl >= xs[hi]) return hi;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < wl) lo = mid; else hi = mid;
  }
  return (Math.abs(xs[lo] - wl) <= Math.abs(xs[hi] - wl)) ? lo : hi;
}

function redrawHeatmap() {
  if (!State.hm) return;
  const canvas = $('#heatmap-canvas');
  const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
  if (cssW <= 0 || cssH <= 0) return;          // tab hidden / not laid out yet

  const dpr = window.devicePixelRatio || 1;
  const cw = Math.round(cssW * dpr), ch = Math.round(cssH * dpr);
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw; canvas.height = ch;
  }
  const ctx = canvas.getContext('2d');
  const rows = State.hm.rows;

  ctx.fillStyle = getCss('--bg-1') || '#101317';
  ctx.fillRect(0, 0, cw, ch);
  if (rows.length === 0) return;

  const xMin = State.hm.xMin, xMax = State.hm.xMax;
  const bandH = ch / HM_ROWS;
  for (let r = 0; r < rows.length; r++) {
    // Newest row (index 0) at the BOTTOM, history scrolling upward — matches the
    // desktop app's waterfall direction.
    const y0 = Math.floor(ch - (r + 1) * bandH);
    const y1 = Math.floor(ch - r * bandH);
    const bh = Math.max(1, y1 - y0);
    const band = ctx.createImageData(cw, bh);
    const row = rows[r];
    const xs = row.xs, norm = row.norm;
    // Map each canvas column to the nearest full-resolution sample by λ.
    for (let px = 0; px < cw; px++) {
      const idx = _hmIndexForX(px, cw, xs, xMin, xMax);
      const [rr, gg, bb] = infernoColor(norm[idx]);
      for (let dy = 0; dy < bh; dy++) {
        const o = (dy * cw + px) * 4;
        band.data[o] = rr; band.data[o+1] = gg; band.data[o+2] = bb; band.data[o+3] = 255;
      }
    }
    ctx.putImageData(band, 0, y0);
  }

  // Measuring crosshair + readout (CSS-pixel coords from the hover handler).
  const cur = State.hm.cursor;
  if (cur && cur.w > 0) {
    const cx = cur.x * dpr, cy = cur.y * dpr;
    ctx.save();
    ctx.strokeStyle = getCss('--accent') || '#2dd4bf';
    ctx.globalAlpha = 0.85;
    ctx.lineWidth = Math.max(1, dpr);
    ctx.setLineDash([4 * dpr, 4 * dpr]);
    ctx.beginPath();
    ctx.moveTo(cx, 0); ctx.lineTo(cx, ch);
    ctx.moveTo(0, cy); ctx.lineTo(cw, cy);
    ctx.stroke();
    ctx.setLineDash([]);

    // Which frame (row) and wavelength is under the cursor? Newest is at the
    // bottom, so invert the vertical position.
    const rIdx = Math.min(rows.length - 1,
                          Math.max(0, Math.floor((1 - cur.y / cssH) * HM_ROWS)));
    const row = rows[rIdx];
    let label = '';
    if (row) {
      const idx = _hmIndexForX(cx, cw, row.xs, xMin, xMax);
      const wl = row.xs[idx], adc = row.raw[idx];
      const age = rIdx;   // 0 = most recent (bottom); older rows increase upward
      label = `${wl.toFixed(1)} nm · ${Math.round(adc)} ADC · −${age} frame${age === 1 ? '' : 's'}`;
      // Also drive the top-bar readout, like the scope tab.
      $('#ro-pixel').textContent = idx;
      $('#ro-wave').innerHTML = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
      $('#ro-int').innerHTML  = `${Math.round(adc)}<span class="unit"> ADC</span>`;
    }
    if (label) {
      ctx.font = `${12 * dpr}px JetBrains Mono, monospace`;
      const padX = 8 * dpr, padY = 5 * dpr;
      const tw = ctx.measureText(label).width;
      let bx = cx + 10 * dpr, by = cy + 10 * dpr;
      if (bx + tw + 2 * padX > cw) bx = cx - tw - 2 * padX - 10 * dpr;
      if (by + 22 * dpr > ch) by = cy - 22 * dpr - 10 * dpr;
      ctx.globalAlpha = 0.9;
      ctx.fillStyle = getCss('--bg-0') || '#0b0e11';
      ctx.fillRect(bx, by, tw + 2 * padX, 18 * dpr + 2 * padY);
      ctx.globalAlpha = 1;
      ctx.fillStyle = getCss('--accent') || '#2dd4bf';
      ctx.fillText(label, bx + padX, by + padY + 13 * dpr);
    }
    ctx.restore();
  }
}

// Compact inferno-ish colormap (5-stop interpolation)
function infernoColor(t) {
  t = Math.max(0, Math.min(1, t));
  const stops = [
    [0,   0,   4],
    [50,  10,  94],
    [150, 50,  100],
    [220, 95,  20],
    [252, 255, 164],
  ];
  const seg = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(seg));
  const f = seg - i;
  return [
    Math.round(stops[i][0] * (1 - f) + stops[i+1][0] * f),
    Math.round(stops[i][1] * (1 - f) + stops[i+1][1] * f),
    Math.round(stops[i][2] * (1 - f) + stops[i+1][2] * f),
  ];
}

// ── Color tab ────────────────────────────────────────────────
// Colour / CRI report + raw-data CSV. Posts the current spectrum to the shared
// server renderer (same engine as the desktop CIE tab) and downloads the result.
function saveColorReport(fmt) {
  const wl = State.scopeData[SI.X], ys = State.scopeData[SI.LIVE];
  if (!wl || !wl.length) { toast('No spectrum yet'); return; }
  fetch('/api/color/report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: fmt,
      wavelengths: Array.from(wl), intensities: Array.from(ys) }),
  })
    .then(r => r.ok ? r.blob() : Promise.reject(r.status))
    .then(blob => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `color_report_${new Date().toISOString().replace(/[:.]/g,'').slice(0,15)}.${fmt}`;
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch(err => toast(err === 503
      ? 'Report needs matplotlib on the server' : 'Report export failed'));
}

function updateColorTab(c) {
  const f = (k, fmt = (v) => v.toFixed(2)) =>
    (c[k] == null ? '—' : fmt(c[k]));
  $('#c-cct').innerHTML  = `${f('cct', v => v.toFixed(0))}<span class="unit"> K</span>`;
  $('#c-ra').textContent = f('Ra',  v => v.toFixed(1));
  $('#c-duv').textContent = f('duv', v => (v >= 0 ? '+' : '') + v.toFixed(4));
  $('#c-x').textContent  = f('x',   v => v.toFixed(4));
  $('#c-y').textContent  = f('y',   v => v.toFixed(4));
  $('#c-u76').textContent = f('u76', v => v.toFixed(4));
  $('#c-v76').textContent = f('v76', v => v.toFixed(4));
  $('#c-sdcm').textContent = f('sdcm', v => v.toFixed(2));
  $('#c-peak').innerHTML  = `${f('peak_nm', v => v.toFixed(1))}<span class="unit"> nm</span>`;
  $('#c-dom').innerHTML   = `${f('dominant_nm', v => v.toFixed(1))}<span class="unit"> nm</span>`;
  $('#c-pur').innerHTML   = `${f('purity_pct', v => v.toFixed(1))}<span class="unit"> %</span>`;
  $('#c-fwhm').innerHTML  = `${f('fwhm_nm', v => v.toFixed(1))}<span class="unit"> nm</span>`;
  $('#c-cent').innerHTML  = `${f('centroid_nm', v => v.toFixed(1))}<span class="unit"> nm</span>`;
  $('#c-sp').textContent  = f('sp_ratio', v => v.toFixed(3));
  $('#c-r').innerHTML = `${f('red_pct',   v => v.toFixed(1))}<span class="unit"> %</span>`;
  $('#c-g').innerHTML = `${f('green_pct', v => v.toFixed(1))}<span class="unit"> %</span>`;
  $('#c-b').innerHTML = `${f('blue_pct',  v => v.toFixed(1))}<span class="unit"> %</span>`;

  // CIE chromaticity diagram
  drawCIE(c.x, c.y, c.cct);
  // R1..R15 bar chart + radar / spider
  drawCRIBars(c.Ri || []);
  drawCRIRadar(c.Ri || []);
  // Reference vs. test colour-patch panel
  drawCRIPatches(c.tcs_swatches);
}

// CIE 1931 horseshoe (static layer cached in an offscreen canvas) -----
let _cieCache = null, _cieCacheKey = '';
function drawCIE(x, y, cct) {
  const canvas = $('#cie-canvas');
  const p = prepCanvas(canvas);
  if (!p) return;
  const { ctx, W, H, dpr } = p;
  const PAD = 28;
  const xMin = 0, xMax = 0.8, yMin = 0, yMax = 0.9;
  const xToPx = (vx) => PAD + (vx - xMin) / (xMax - xMin) * (W - 2 * PAD);
  const yToPx = (vy) => H - PAD - (vy - yMin) / (yMax - yMin) * (H - 2 * PAD);

  // Build / reuse the cached static layer at the current device resolution.
  const key = `${W}x${H}x${dpr}`;
  if (_cieCacheKey !== key) {
    const off = document.createElement('canvas');
    off.width = Math.round(W * dpr); off.height = Math.round(H * dpr);
    const octx = off.getContext('2d');
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawCIEStatic(octx, W, H, PAD, xToPx, yToPx);
    _cieCache = off; _cieCacheKey = key;
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.drawImage(_cieCache, 0, 0);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // Live measurement marker
  if (x != null && y != null && isFinite(x) && isFinite(y) && x >= 0 && y >= 0) {
    const px = xToPx(x), py = yToPx(y);
    ctx.beginPath(); ctx.arc(px, py, 8, 0, Math.PI * 2);
    ctx.fillStyle = getCss('--accent');
    ctx.strokeStyle = getCss('--bg-0');
    ctx.lineWidth = 3;
    ctx.fill(); ctx.stroke();
    // CCT/Duv label
    if (cct != null && isFinite(cct)) {
      ctx.fillStyle = getCss('--accent');
      ctx.font = 'bold 11px JetBrains Mono';
      ctx.fillText(`${cct.toFixed(0)} K`, px + 12, py - 8);
    }
  }
}

function drawCIEStatic(ctx, W, H, PAD, xToPx, yToPx) {
  ctx.fillStyle = getCss('--bg-0');
  ctx.fillRect(0, 0, W, H);

  // Spectrum locus (approximate horseshoe — 380..700nm)
  const locus = [];
  for (let wl = 380; wl <= 700; wl += 5) {
    const xy = sRGBSpectrumXY(wl);
    if (xy) locus.push(xy);
  }

  // Fill the inside with a coarse colour grid (using xy → sRGB approximation)
  const step = 4;
  for (let py = PAD; py < H - PAD; py += step) {
    for (let px = PAD; px < W - PAD; px += step) {
      const vx = (px - PAD) / (W - 2 * PAD) * 0.8;
      const vy = 0.9 - (py - PAD) / (H - 2 * PAD) * 0.9;
      if (!pointInPolygon([vx, vy], locus)) continue;
      const [r, g, b] = xyToSRGB(vx, vy);
      ctx.fillStyle = `rgba(${r},${g},${b},0.85)`;
      ctx.fillRect(px, py, step, step);
    }
  }

  // Locus outline
  ctx.strokeStyle = getCss('--fg-1');
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (let i = 0; i < locus.length; i++) {
    const [vx, vy] = locus[i];
    const px = xToPx(vx), py = yToPx(vy);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath(); ctx.stroke();

  // Wavelength labels along locus
  ctx.fillStyle = getCss('--fg-2');
  ctx.font = '9px JetBrains Mono';
  for (const wl of [400, 460, 480, 500, 520, 540, 560, 580, 600, 620, 650]) {
    const xy = sRGBSpectrumXY(wl);
    if (xy) {
      const px = xToPx(xy[0]) + 4;
      const py = yToPx(xy[1]) - 4;
      ctx.fillText(`${wl}`, px, py);
    }
  }

  // Planckian locus
  ctx.strokeStyle = getCss('--warn'); ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  for (let T = 1500; T <= 20000; T += 250) {
    const [x, y] = planckianXY(T);
    const px = xToPx(x), py = yToPx(y);
    if (T === 1500) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // Iso-CCT markers
  ctx.fillStyle = getCss('--warn');
  ctx.strokeStyle = getCss('--bg-0');
  ctx.lineWidth = 1.5;
  for (const T of [2700, 3000, 4000, 5000, 6500, 10000]) {
    const [x, y] = planckianXY(T);
    const px = xToPx(x), py = yToPx(y);
    ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = getCss('--warn');
    ctx.font = '9.5px JetBrains Mono';
    ctx.fillText(`${T/1000}k`, px + 6, py + 3);
    ctx.fillStyle = getCss('--warn');
  }

  // Axes labels
  ctx.fillStyle = getCss('--fg-3'); ctx.font = '10px JetBrains Mono';
  ctx.fillText('x', W - 14, H - 8);
  ctx.fillText('y', 6, 14);
}

// CIE 1931 2° observer — small lookup for spectrum locus (380..700nm, 10nm).
// Coarse but good enough for the diagram outline.
const CMF_X = [0.0014,0.0042,0.0143,0.0435,0.1344,0.2839,0.3483,0.3362,0.2908,0.1954,0.0956,0.0320,0.0049,0.0093,0.0633,0.1655,0.2904,0.4334,0.5945,0.7621,0.9163,1.0263,1.0622,1.0026,0.8544,0.6424,0.4479,0.2835,0.1649,0.0874,0.0468,0.0227,0.0114];
const CMF_Y = [0.0000,0.0001,0.0004,0.0012,0.0040,0.0116,0.0230,0.0380,0.0600,0.0910,0.1390,0.2080,0.3230,0.5030,0.7100,0.8620,0.9540,0.9950,0.9950,0.9520,0.8700,0.7570,0.6310,0.5030,0.3810,0.2650,0.1750,0.1070,0.0610,0.0320,0.0170,0.0082,0.0041];
const CMF_Z = [0.0065,0.0201,0.0679,0.2074,0.6456,1.3856,1.7471,1.7721,1.6692,1.2876,0.8130,0.4652,0.2720,0.1582,0.0782,0.0422,0.0203,0.0087,0.0039,0.0021,0.0017,0.0011,0.0008,0.0003,0.0002,0,0,0,0,0,0,0,0];
function sRGBSpectrumXY(wl) {
  if (wl < 380 || wl > 700) return null;
  const idx = (wl - 380) / 10;
  const lo = Math.floor(idx), hi = Math.min(CMF_X.length - 1, lo + 1);
  const t = idx - lo;
  const x = CMF_X[lo] * (1 - t) + CMF_X[hi] * t;
  const y = CMF_Y[lo] * (1 - t) + CMF_Y[hi] * t;
  const z = CMF_Z[lo] * (1 - t) + CMF_Z[hi] * t;
  const sum = x + y + z;
  if (sum < 1e-9) return null;
  return [x / sum, y / sum];
}

function xyToSRGB(x, y) {
  if (y <= 0) return [0, 0, 0];
  const Y = 1;
  const X = (x / y) * Y;
  const Z = ((1 - x - y) / y) * Y;
  let r =  3.2406 * X - 1.5372 * Y - 0.4986 * Z;
  let g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z;
  let b =  0.0557 * X - 0.2040 * Y + 1.0570 * Z;
  r = Math.max(0, r); g = Math.max(0, g); b = Math.max(0, b);
  const mx = Math.max(r, g, b);
  if (mx > 1) { r /= mx; g /= mx; b /= mx; }
  const gamma = (v) => v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1/2.4) - 0.055;
  return [
    Math.round(255 * gamma(r)),
    Math.round(255 * gamma(g)),
    Math.round(255 * gamma(b)),
  ];
}

function pointInPolygon(p, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1];
    const xj = poly[j][0], yj = poly[j][1];
    const hit = ((yi > p[1]) !== (yj > p[1])) &&
                (p[0] < (xj - xi) * (p[1] - yi) / (yj - yi + 1e-12) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}

function planckianXY(T) {
  // McCamy-inverse approximation via Planck SPD → CMFs.
  // Quick path: empirical (Krystek's polynomial fits).
  // x_D for daylight is not the same as Planckian; use direct integral.
  let X = 0, Y = 0, Z = 0;
  const h = 6.62607015e-34, c = 2.99792458e8, k = 1.380649e-23;
  for (let i = 0; i < CMF_X.length; i++) {
    const wl = (380 + i * 10) * 1e-9;
    const a = (2 * h * c * c) / Math.pow(wl, 5);
    const exponent = Math.min(700, (h * c) / (wl * k * T));
    const Lλ = a / (Math.exp(exponent) - 1);
    X += Lλ * CMF_X[i]; Y += Lλ * CMF_Y[i]; Z += Lλ * CMF_Z[i];
  }
  const s = X + Y + Z;
  return [X / s, Y / s];
}

// R1..R15 bar chart -------------------------------------------
function drawCRIBars(Ri) {
  const host = $('#cri-bars');
  if (host.childElementCount === 0) {
    // Lazy build the 15 rows
    for (let i = 0; i < 15; i++) {
      const row = document.createElement('div');
      row.className = 'bar-row';
      row.innerHTML = `<span>R${i+1}</span>
        <div class="bar"><div class="bar-fill" id="cri-fill-${i}"></div></div>
        <span class="bar-val" id="cri-val-${i}">—</span>`;
      host.appendChild(row);
    }
  }
  const colors = [
    '#E8B89B','#D9C982','#C7D068','#8FBE7C','#7BBFAE','#7AA3D4','#9F8DCB','#C77AB1',
    '#C13F3A','#D8C04A','#4FA864','#2F4FB8','#E2B89A','#7E8F50','#D9B8A0',
  ];
  for (let i = 0; i < 15; i++) {
    const r = Ri[i];
    const fillEl = $(`#cri-fill-${i}`);
    const valEl  = $(`#cri-val-${i}`);
    if (r == null || !isFinite(r)) {
      fillEl.style.width = '0%';
      valEl.textContent = '—';
    } else {
      fillEl.style.width = `${Math.max(0, Math.min(100, r))}%`;
      fillEl.style.background = colors[i];
      valEl.textContent = r.toFixed(1);
    }
  }
}

// Ref/Test colour-patch panel: top row reference appearance, bottom row test
// appearance (both Bradford-adapted to D65), with the per-sample ΔE*ab as a
// tooltip. Fed by the measure_all 'tcs_swatches' carried in the frame payload.
function drawCRIPatches(sw) {
  const host = $('#cri-patches');
  if (!host) return;
  if (!sw || !sw.ref || !sw.ref.length) { host.innerHTML = ''; return; }
  const labels = sw.labels || [], ref = sw.ref, test = sw.test, dE = sw.dE || [];
  const n = ref.length;
  // (re)build once for the right column count
  if (host.childElementCount !== n + 1) {
    host.innerHTML = '';
    const make = (i, isSrc) => {
      const col = document.createElement('div');
      col.className = 'patch-col';
      col.innerHTML =
        `<div class="patch patch-ref" id="pr-${i}"></div>` +
        `<div class="patch patch-test" id="pt-${i}"></div>` +
        `<span class="patch-lbl" id="pl-${i}"></span>`;
      host.appendChild(col);
    };
    for (let i = 0; i < n; i++) make(i, false);
    make(n, true);                       // source column
  }
  for (let i = 0; i < n; i++) {
    const name = (labels[i] || `TCS${i + 1}`);
    const d = dE[i];
    const tip = (d == null) ? name : `${name}  ΔE*ab = ${d.toFixed(1)}`;
    const pr = $(`#pr-${i}`), pt = $(`#pt-${i}`), pl = $(`#pl-${i}`);
    pr.style.background = ref[i]; pt.style.background = test[i];
    pr.title = tip; pt.title = tip;
    pl.textContent = name.replace('TCS', '');
  }
  // source column
  const pr = $(`#pr-${n}`), pt = $(`#pt-${n}`), pl = $(`#pl-${n}`);
  if (pr) {
    pr.style.background = sw.source_ref || '#222';
    pt.style.background = sw.source_test || '#222';
    pr.title = pt.title = 'Source white (Ref vs Test tint)';
    pl.textContent = 'Src';
  }
}

// R1..R15 radar / spider chart -------------------------------
const TCS_COLORS = [
  '#E8B89B','#D9C982','#C7D068','#8FBE7C','#7BBFAE','#7AA3D4','#9F8DCB','#C77AB1',
  '#C13F3A','#D8C04A','#4FA864','#2F4FB8','#E2B89A','#7E8F50','#D9B8A0',
];
function drawCRIRadar(Ri) {
  const canvas = $('#cri-radar');
  if (!canvas) return;
  const p = prepCanvas(canvas);
  if (!p) return;
  const { ctx, W, H } = p;
  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 26;
  const N = 15;
  ctx.clearRect(0, 0, W, H);
  const fg3 = getCss('--fg-3') || '#80848e';
  const grid = getCss('--border-1') || '#2a2e37';

  // concentric rings at 25/50/75/100 (= Ri scaled to R at 100)
  ctx.strokeStyle = grid; ctx.fillStyle = fg3;
  ctx.font = '9px JetBrains Mono'; ctx.textAlign = 'right';
  for (const lvl of [25, 50, 75, 100]) {
    const rr = R * lvl / 100;
    ctx.beginPath(); ctx.arc(cx, cy, rr, 0, 2 * Math.PI); ctx.stroke();
  }
  ctx.fillText('100', cx - 3, cy - R + 2);

  // spokes + labels
  const ang = i => -Math.PI / 2 + i * 2 * Math.PI / N;   // R1 at top, clockwise
  ctx.strokeStyle = grid;
  for (let i = 0; i < N; i++) {
    const a = ang(i);
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + R * Math.cos(a), cy + R * Math.sin(a)); ctx.stroke();
  }
  ctx.fillStyle = fg3; ctx.font = '9px JetBrains Mono'; ctx.textAlign = 'center';
  for (let i = 0; i < N; i++) {
    const a = ang(i), lr = R + 12;
    ctx.fillText(`R${i + 1}`, cx + lr * Math.cos(a), cy + lr * Math.sin(a) + 3);
  }

  // polygon
  const rv = i => {
    const v = Ri[i];
    return (v == null || !isFinite(v)) ? 0 : Math.max(0, Math.min(120, v));
  };
  ctx.beginPath();
  for (let i = 0; i <= N; i++) {
    const idx = i % N, a = ang(idx), rr = R * rv(idx) / 100;
    const px = cx + rr * Math.cos(a), py = cy + rr * Math.sin(a);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath();
  ctx.fillStyle = 'rgba(45,212,191,0.18)'; ctx.fill();
  ctx.strokeStyle = '#2dd4bf'; ctx.lineWidth = 1.5; ctx.stroke();

  // dots in TCS colours
  for (let i = 0; i < N; i++) {
    const a = ang(i), rr = R * rv(i) / 100;
    ctx.beginPath();
    ctx.arc(cx + rr * Math.cos(a), cy + rr * Math.sin(a), 3, 0, 2 * Math.PI);
    ctx.fillStyle = TCS_COLORS[i]; ctx.fill();
  }
}

// ── Config sync ──────────────────────────────────────────────
function applyConfigToUI(cfg) {
  for (const [key, value] of Object.entries(cfg)) {
    let v = value;
    // Defensive: exposure is in MILLISECONDS. A value far above the hardware
    // ceiling (~2500 ms) means a microsecond value leaked into the field;
    // normalize so the box never shows e.g. 769,395 for a 769 ms exposure.
    if (key === 'exposure_ms' && typeof v === 'number' && v > 5000) {
      v = v / 1000;
    }
    const el = document.getElementById(`cfg-${key}`)
            || document.querySelector(`input[data-key="${key}"], select[data-key="${key}"]`);
    if (el) {
      const scale = cfgScaleOf(el);
      let dv = (typeof v === 'number' && scale !== 1) ? killFloatDust(v / scale) : v;
      if (el.type === 'checkbox') el.checked = !!dv;
      else                        el.value = dv;
    }
    const tog = document.querySelector(`.toggle[data-key="${key}"]`);
    if (tog) tog.classList.toggle('on', !!v);
  }
  // Theme
  const isLight = cfg.theme === 'light';
  document.body.classList.toggle('light', isLight);
  $('#theme-dark').classList.toggle('on', !isLight);
  $('#theme-light').classList.toggle('on', isLight);
  // Peak / measure toolbar state
  State.peaksOn = !!cfg.show_peaks;
  State.measureMode = !!cfg.measure_mode;
  $('#btn-peaks').classList.toggle('on', State.peaksOn);
  $('#btn-measure').classList.toggle('on', State.measureMode);
}

// Reset integration time to 20 ms (mirrors the desktop GUI's "20 ms" button).
function resetExposure() {
  const el = $('#cfg-exposure_ms');
  if (el) el.value = 20;
  pushCfg({ exposure_ms: 20 });
}

// Snap Y max to the current frame peak and turn Auto-scale Y off (mirrors the
// desktop GUI's ▲ button). Reads the latest spectrum already held in scopeData.
function snapYToPeak() {
  const ys = State.scopeData[1] || [];
  let peak = 10;
  for (const v of ys) if (isFinite(v) && v > peak) peak = v;
  peak = Math.max(10, Math.round(peak));
  const yEl = $('#cfg-y_max_adc');
  if (yEl) yEl.value = peak;
  const autoTog = document.querySelector('.toggle[data-key="auto_y"]');
  if (autoTog) autoTog.classList.remove('on');
  // One push carries both changes; pushCfg re-feeds the plot so the new fixed
  // range applies immediately even while paused.
  pushCfg({ y_max_adc: peak, auto_y: false });
}

function pushCfg(partial) {
  Object.assign(State.cfg, partial);
  sendWS({ type: 'set_config', payload: partial });
  // Editing the display X range clears any active wheel/drag zoom so the new
  // limits actually take effect.
  if ('x_min_nm' in partial || 'x_max_nm' in partial) State.xView = null;
  // A display-range change is normally picked up on the next frame (the range
  // functions read State.cfg). If we're paused / between frames, force it now
  // by re-feeding the current data: setData(resetScales=true) re-runs
  // xRange()/autoYRange() and rebuilds the path WITHOUT locking the scale the
  // way an explicit setScale() would.
  if (State.scopePlot &&
      ('x_min_nm' in partial || 'x_max_nm' in partial ||
       'y_max_adc' in partial || 'auto_y' in partial)) {
    State.scopePlot.setData(State.scopeData);
  }
}

// All value inputs/selects bound to a config key — either by id="cfg-KEY"
// (most controls) or data-key="KEY" (inline controls like fast-preview and
// fusion). Toggles are spans handled separately. An optional data-scale lets
// a control display in different units than it stores (e.g. fusion smoothing
// shows 0–99 % but stores 0–0.99, so data-scale="0.01").
function cfgInputEls() {
  const byId = $$('[id^="cfg-"]');
  const byKey = $$('input[data-key], select[data-key]');  // excludes .toggle spans
  return [...byId, ...byKey];
}
function cfgKeyOf(el) {
  return (el.id && el.id.startsWith('cfg-')) ? el.id.slice(4) : el.dataset.key;
}
function cfgScaleOf(el) {
  const s = parseFloat(el.dataset.scale);
  return (isFinite(s) && s !== 0) ? s : 1;
}
function killFloatDust(n) { return Math.round(n * 1e6) / 1e6; }

function wireConfigInputs() {
  cfgInputEls().forEach((el) => {
    const key = cfgKeyOf(el);
    if (!key) return;
    const scale = cfgScaleOf(el);
    el.addEventListener('change', () => {
      let v = el.value;
      if (el.type === 'number') v = parseFloat(v);
      if (typeof v === 'number' && scale !== 1) v = killFloatDust(v * scale);
      pushCfg({ [key]: v });
    });
  });
  $$('.toggle').forEach((el) => {
    const key = el.dataset.key;
    el.addEventListener('click', () => {
      const on = !el.classList.contains('on');
      el.classList.toggle('on', on);
      pushCfg({ [key]: on });
    });
  });
}

function updateConnectButton() {
  const btn = $('#btn-connect');
  if (State.hardwareConnected) {
    btn.textContent = 'Disconnect';
    btn.classList.add('connected');
  } else {
    btn.textContent = 'Connect';
    btn.classList.remove('connected');
  }
  syncPauseButton();
}

// Keep the Pause/Start button in step with the connection. It only does
// anything while connected, and a fresh connect ALWAYS starts running (the
// backend's is_measurement_paused resets to false on connect), so the button
// must read "PAUSE" then — never a stale "START" left over from before.
function syncPauseButton() {
  const btn = $('#btn-pause');
  if (!btn) return;
  if (!State.hardwareConnected) {
    State.paused = false;          // nothing is running while disconnected
    btn.disabled = true;
  } else {
    btn.disabled = false;
  }
  btn.classList.toggle('paused', State.paused);
  btn.innerHTML = State.paused ? '▶ &nbsp; START' : '■ &nbsp; PAUSE';
}

// ── Toolbar handlers ─────────────────────────────────────────
async function scanDevices() {
  const backend = $('#backend-select').value || '';
  const r = await fetch('/api/devices?backend=' + encodeURIComponent(backend)).then(r => r.json());
  const sel = $('#device-select');
  sel.innerHTML = '';
  if ((r.devices || []).length === 0) {
    sel.appendChild(new Option('No ' + (backend || 'device') + ' found', ''));
    $('#btn-connect').disabled = true;
  } else {
    for (const path of r.devices) sel.appendChild(new Option(path, path));
    $('#btn-connect').disabled = false;
  }
}

async function toggleConnection() {
  if (State.hardwareConnected) {
    await fetch('/api/disconnect', { method: 'POST' });
    State.hardwareConnected = false;
  } else {
    const path = $('#device-select').value || null;
    try {
      const backend = $('#backend-select').value || null;
      const r = await fetch('/api/connect', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ device_path: path, backend: backend }),
      });
      if (!r.ok) throw new Error((await r.json()).detail);
      State.hardwareConnected = true;
      State.paused = false;          // a fresh connect starts running
      // Re-fetch the active reference: the server interpolates onto the
      // connected device's wavelength axis, which differs per device.
      applyReference($('#cfg-reference_library')?.value || State.referenceName);
    } catch (e) { toast('Connect failed: ' + e.message); }
  }
  updateConnectButton();
}

function togglePause() {
  if (!State.hardwareConnected) return;   // no-op while disconnected
  State.paused = !State.paused;
  syncPauseButton();
  sendWS({ type: 'pause', paused: State.paused });
}

function togglePeaks() {
  State.peaksOn = !State.peaksOn;
  $('#btn-peaks').classList.toggle('on', State.peaksOn);
  pushCfg({ show_peaks: State.peaksOn });
}

function toggleMeasure() {
  State.measureMode = !State.measureMode;
  $('#btn-measure').classList.toggle('on', State.measureMode);
  pushCfg({ measure_mode: State.measureMode });
}

function setTheme(mode) {
  document.body.classList.toggle('light', mode === 'light');
  $('#theme-dark').classList.toggle('on', mode === 'dark');
  $('#theme-light').classList.toggle('on', mode === 'light');
  pushCfg({ theme: mode });
  _cieCacheKey = '';   // force CIE diagram static layer rebuild with new palette
}

function switchTab(tab) {
  State.activeTab = tab;
  $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === tab));
  $$('.tab-pane').forEach(p => p.classList.toggle('on', p.dataset.pane === tab));
  // The newly shown pane only gets a real height after this class change, so
  // size/redraw on the next frame.
  requestAnimationFrame(() => {
    if (tab === 'scope' && State.scopePlot) {
      const c = $('#scope-plot');
      if (c.clientWidth > 0 && c.clientHeight > 0) {
        State.scopePlot.setSize({ width: c.clientWidth, height: c.clientHeight });
      }
    } else if (tab === 'heatmap') {
      redrawHeatmap();
    } else if (tab === 'replay') {
      Replay.redraw();
    } else if (tab === 'filter') {
      Filter.onShow();
    }
  });
}

// ── Export ──────────────────────────────────────────────────
function downloadCsv() {
  const tab = State.activeTab === 'heatmap' ? 'heatmap' : 'spectrum';
  window.location.href = `/api/export/csv?tab=${tab}`;
}

function downloadPng() {
  // Client-side PNG: grab the canvas (heatmap) or the uPlot main canvas.
  const t = State.activeTab;
  let canvas;
  if (t === 'heatmap')      canvas = $('#heatmap-canvas');
  else if (t === 'color')   canvas = $('#cie-canvas');
  else                       canvas = State.scopePlot?.ctx?.canvas;
  if (!canvas) { toast('Nothing to save'); return; }
  const url = canvas.toDataURL('image/png');
  const a = document.createElement('a');
  a.href = url;
  a.download = `${t}_${new Date().toISOString().replace(/[:.]/g,'').slice(0,15)}.png`;
  a.click();
}

// Save the current colorimetry results as a CSV report (mirrors the desktop
// CIE tab's missing export). Pulls from the most recent color metrics.
function exportColorReport() {
  const c = (State.lastFrame && State.lastFrame.color) || State.lastColor;
  if (!c) { toast('No colorimetry data yet'); return; }
  const ts = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const num = (v, d = 4) => (v == null || !isFinite(v)) ? '' : Number(v).toFixed(d);
  const rows = [
    ['# Colorimetry report'],
    ['# Generated', ts],
    ['# Backend', (State.lastFrame && State.lastFrame.backend) || $('#sb-backend').textContent || ''],
    [],
    ['Metric', 'Value', 'Unit'],
    ['CCT', num(c.cct, 0), 'K'],
    ['Duv', num(c.duv, 4), ''],
    ['CIE x', num(c.x, 4), ''],
    ['CIE y', num(c.y, 4), ''],
    ["u'", num(c.u76, 4), ''],
    ["v'", num(c.v76, 4), ''],
    ['SDCM', num(c.sdcm, 2), ''],
    ['CRI Ra', num(c.Ra, 1), ''],
    ['Peak wavelength', num(c.peak_nm, 1), 'nm'],
    ['Dominant wavelength', num(c.dominant_nm, 1), 'nm'],
    ['Purity', num(c.purity_pct, 1), '%'],
    ['FWHM', num(c.fwhm_nm, 1), 'nm'],
    ['Centroid wavelength', num(c.centroid_nm, 1), 'nm'],
    ['S/P ratio', num(c.sp_ratio, 3), ''],
    ['Red', num(c.red_pct, 1), '%'],
    ['Green', num(c.green_pct, 1), '%'],
    ['Blue', num(c.blue_pct, 1), '%'],
  ];
  const Ri = c.Ri || [];
  for (let i = 0; i < Ri.length; i++) {
    rows.push([`CRI R${i + 1}`, num(Ri[i], 1), '']);
  }
  // CSV with ';' delimiter to match the project's other CSVs (German locale).
  const csv = rows.map(r => r.join(';')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `colorimetry_${new Date().toISOString().replace(/[:.]/g, '').slice(0, 15)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('Colorimetry report saved');
}

function copyClipboard() {
  if (!State.lastFrame) { toast('No data yet'); return; }
  const lines = ['Wavelength_nm\tIntensity_ADC'];
  for (let i = 0; i < State.lastFrame.wavelengths.length; i++) {
    lines.push(`${State.lastFrame.wavelengths[i].toFixed(2)}\t${State.lastFrame.intensities[i].toFixed(2)}`);
  }
  navigator.clipboard.writeText(lines.join('\n'))
    .then(() => toast(`Copied ${lines.length - 1} samples`))
    .catch(() => toast('Copy failed'));
}

// ── Bootstrap ────────────────────────────────────────────────
async function init() {
  // Wire buttons + open the WebSocket FIRST so the connection controls and
  // dropdown population (via the hello frame) never depend on the plot
  // libraries initialising. A throw in makeScopePlot() must not leave the
  // page dead — that was the "Cannot read properties of null" bootstrap hang.
  try {
    $('#btn-scan').addEventListener('click', scanDevices);
    $('#btn-connect').addEventListener('click', toggleConnection);
    $('#btn-pause').addEventListener('click', togglePause);
    $('#btn-peaks').addEventListener('click', togglePeaks);
    $('#btn-measure').addEventListener('click', toggleMeasure);
    $('#theme-dark').addEventListener('click', () => setTheme('dark'));
    $('#theme-light').addEventListener('click', () => setTheme('light'));
    $('#btn-csv').addEventListener('click', downloadCsv);
    $('#btn-png').addEventListener('click', downloadPng);
    $('#btn-clip').addEventListener('click', copyClipboard);
    $('#btn-exp-reset').addEventListener('click', resetExposure);
    $('#btn-y-snap').addEventListener('click', snapYToPeak);
    $('#btn-cie-csv').addEventListener('click', exportColorReport);
    const refSel = $('#cfg-reference_library');
    if (refSel) refSel.addEventListener('change', () => applyReference(refSel.value));
    // Scope overlay toolbar
    $('#btn-freeze')     ?.addEventListener('click', toggleFreeze);
    $('#btn-diff')       ?.addEventListener('click', toggleDiff);
    $('#btn-peakhold')   ?.addEventListener('click', togglePeakHold);
    $('#btn-persist')    ?.addEventListener('click', togglePersist);
    $('#csvbg-file')     ?.addEventListener('change', onCsvBgFile);
    $('#btn-csvbg-clear')?.addEventListener('click', clearCsvBg);
    syncOverlayButtons();
    // Filter tab toolbar
    $('#btn-filter-baseline')?.addEventListener('click', () => Filter.setBaseline());
    $('#btn-filter-clear')   ?.addEventListener('click', () => Filter.clearBaseline());
    $('#btn-filter-pdf')     ?.addEventListener('click', () => Filter.saveReport('pdf'));
    $('#btn-filter-png')     ?.addEventListener('click', () => Filter.saveReport('png'));
    $('#btn-filter-csv')     ?.addEventListener('click', () => Filter.saveReport('csv'));
    $('#btn-color-pdf')      ?.addEventListener('click', () => saveColorReport('pdf'));
    $('#btn-color-png')      ?.addEventListener('click', () => saveColorReport('png'));
    $('#btn-color-csv')      ?.addEventListener('click', () => saveColorReport('csv'));
    $$('.color-subtab').forEach(b => b.addEventListener('click', () => {
      const sub = b.dataset.sub;
      State.colorSub = sub;
      $$('.color-subtab').forEach(x => x.classList.toggle('on', x === b));
      $$('.color-sub').forEach(p => { p.hidden = (p.dataset.sub !== sub); });
      if (sub === 'tm30') Tm30.onShow();
    }));
    $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
    $('#sidebar-toggle').addEventListener('click', () => {
      $('#sidebar').classList.toggle('open');
    });
    wireConfigInputs();
  } catch (e) {
    console.error('init: wiring controls failed', e);
  }

  // Status (best-effort)
  try {
    const s = await fetch('/api/status').then(r => r.json());
    $('#sb-backend').textContent = s.backend;
    State.hardwareConnected = !!s.connected;
    updateConnectButton();
  } catch (e) { console.warn('init: /api/status failed', e); }

  // WebSocket — populates backend/device dropdowns from the hello frame.
  try { connectWS(); } catch (e) { console.error('init: connectWS failed', e); }

  // Initial device scan (best-effort)
  try { await scanDevices(); } catch (e) { console.warn('init: scanDevices failed', e); }

  // Plot + heatmap LAST and isolated — a rendering error here is non-fatal.
  try { makeScopePlot(); } catch (e) { console.error('init: makeScopePlot failed', e); }
  try { initHeatmap(); } catch (e) { console.error('init: initHeatmap failed', e); }
  try { Replay.init(); } catch (e) { console.error('init: Replay.init failed', e); }
  try { Filter.init(); } catch (e) { console.error('init: Filter.init failed', e); }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// ════════════════════════════════════════════════════════════════════
//  Replay — waterfall player loaded from a CSV file
//  ----------------------------------------------------------------
//  Accepts two CSV shapes:
//   1. Heatmap export from this app:  "Frame;<wl1>;<wl2>;…" header, then one
//      row per frame "<idx>;<v1>;<v2>;…". Each row is one spectrum (a waterfall
//      line). This is the primary format.
//   2. Single-spectrum CSV: two columns (wavelength, intensity), with or
//      without headers / comment lines. Loaded as a single frame so the user
//      at least sees it; the player then has one frame.
//  Frames are normalized per-load (global max across all frames) into [0..1]
//  and rendered with the same inferno colormap + band layout as the live
//  heatmap. A play timer advances the frame; the scrub slider seeks. Newest
//  semantics match the live view: frame 0 at the TOP, later frames below, so a
//  loaded heatmap export reads the same way it was recorded.
// ════════════════════════════════════════════════════════════════════
const Replay = (() => {
  const RCOLS = 512;                 // resample resolution per frame
  let frames = [];                   // array of Float32Array(RCOLS), [0..1]
  let wavelengths = null;            // optional λ axis (for the info line)
  let idx = 0;                       // current frame index
  let playing = false;
  let timer = null;
  let baseInterval = 120;            // ms per frame at 1× (derived if possible)

  const $id = (s) => document.getElementById(s);

  function init() {
    const file = $id('replay-file');
    const play = $id('replay-play');
    const scrub = $id('replay-scrub');
    const speed = $id('replay-speed');
    if (!file) return;              // pane not present
    file.addEventListener('change', onFile);
    play.addEventListener('click', togglePlay);
    scrub.addEventListener('input', () => { seek(parseInt(scrub.value, 10)); });
    speed.addEventListener('change', () => { if (playing) { stopTimer(); startTimer(); } });
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => redraw());
      ro.observe($id('replay-canvas'));
    }
  }

  function onFile(ev) {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        parseCsv(String(reader.result));
        $id('replay-name').textContent = `${f.name} · ${frames.length} frames`;
        setupAfterLoad();
      } catch (e) {
        console.error('replay parse failed', e);
        toast('Could not parse CSV: ' + e.message);
      }
    };
    reader.onerror = () => toast('File read failed');
    reader.readAsText(f);
  }

  // Parse either the heatmap matrix CSV or a 2-column spectrum CSV.
  function parseCsv(text) {
    const rawLines = text.split(/\r?\n/);
    // Keep comment lines aside, drop blank lines.
    const lines = rawLines.filter(l => l.trim().length && !l.trim().startsWith('#'));
    if (lines.length === 0) throw new Error('empty file');

    // Detect delimiter: project CSVs use ';', but tolerate ',', tab, and
    // whitespace (the LR-2T raw dump is "wavelength<space>intensity").
    const sample = lines[0];
    let delim;
    if (sample.indexOf(';') >= 0)       delim = ';';
    else if (sample.indexOf('\t') >= 0) delim = '\t';
    else if (sample.indexOf(',') >= 0)  delim = ',';
    else                                delim = /\s+/;   // regex split on runs of whitespace
    const split = (l) => l.trim().split(delim);

    const first = split(lines[0]).map(s => s.trim());
    const firstIsHeader = first.some(s => /[a-zA-Z]/.test(s));

    // Two matrix shapes are possible with many columns:
    //  (a) Waterfall export from this app: header starts with "Frame", each row
    //      is one time-frame, columns are wavelengths. Rows = frames.
    //  (b) Reference-style matrix: first column is a wavelength axis and the
    //      other columns are different spectra (e.g. reference_spectra.csv:
    //      Wavelength;Hydrogen;Mercury;…). Here COLUMNS are the spectra, so we
    //      transpose — each column becomes a playable frame.
    const isFrameMatrix = /frame/i.test(first[0]);
    const looksMatrix = first.length > 3 && (isFrameMatrix || (firstIsHeader && first.length > 8));

    frames = [];
    wavelengths = null;

    if (looksMatrix && !isFrameMatrix) {
      // Reference-style: parse all numeric rows, split off column 0 as the
      // wavelength axis, transpose remaining columns into frames.
      const startRow = firstIsHeader ? 1 : 0;
      const wlAxis = [];
      const cols = first.length - 1;
      const colData = Array.from({ length: cols }, () => []);
      for (let li = startRow; li < lines.length; li++) {
        const parts = split(lines[li]);
        if (parts.length < 2) continue;
        const w = parseFloat(parts[0]);
        if (!isFinite(w)) continue;
        wlAxis.push(w);
        for (let c = 1; c < parts.length && c - 1 < cols; c++) {
          const v = parseFloat(parts[c]);
          colData[c - 1].push(isFinite(v) ? v : 0);
        }
      }
      if (wlAxis.length === 0) throw new Error('no numeric rows found');
      wavelengths = wlAxis;
      let gmax = 0;
      for (const col of colData) for (const v of col) if (v > gmax) gmax = v;
      if (gmax <= 0) gmax = 1;
      for (const col of colData) frames.push(resampleNorm(Float64Array.from(col), gmax));
    } else if (looksMatrix) {
      // Header columns 1..N are wavelengths; each later row is one frame.
      wavelengths = first.slice(1).map(parseFloat);
      const raw = [];
      let gmax = 0;
      for (let li = 1; li < lines.length; li++) {
        const parts = split(lines[li]);
        if (parts.length < 2) continue;
        const vals = new Float64Array(parts.length - 1);
        for (let c = 1; c < parts.length; c++) {
          const v = parseFloat(parts[c]);
          vals[c - 1] = isFinite(v) ? v : 0;
          if (vals[c - 1] > gmax) gmax = vals[c - 1];
        }
        raw.push(vals);
      }
      if (raw.length === 0) throw new Error('no data rows');
      if (gmax <= 0) gmax = 1;
      // Resample each row to RCOLS and normalize by global max.
      for (const vals of raw) frames.push(resampleNorm(vals, gmax));
    } else {
      // Two-column spectrum: wavelength, intensity → a single frame.
      const wl = [], it = [];
      const startRow = firstIsHeader ? 1 : 0;
      let gmax = 0;
      for (let li = startRow; li < lines.length; li++) {
        const parts = split(lines[li]);
        if (parts.length < 2) continue;
        // Some project spectrum CSVs are Pixel;Wavelength;ADC (3 cols).
        const w = parseFloat(parts[parts.length - 2]);
        const v = parseFloat(parts[parts.length - 1]);
        if (!isFinite(w) || !isFinite(v)) continue;
        wl.push(w); it.push(v); if (v > gmax) gmax = v;
      }
      if (it.length === 0) throw new Error('no numeric rows found');
      if (gmax <= 0) gmax = 1;
      wavelengths = wl;
      frames.push(resampleNorm(Float64Array.from(it), gmax));
    }
  }

  // Resample an arbitrary-length intensity vector to RCOLS and normalize.
  function resampleNorm(vals, gmax) {
    const out = new Float32Array(RCOLS);
    const n = vals.length;
    if (n === RCOLS) {
      for (let i = 0; i < RCOLS; i++) out[i] = Math.max(0, Math.min(1, vals[i] / gmax));
      return out;
    }
    for (let c = 0; c < RCOLS; c++) {
      const src = c / (RCOLS - 1) * (n - 1);
      const i0 = Math.floor(src), i1 = Math.min(n - 1, i0 + 1), f = src - i0;
      const v = vals[i0] * (1 - f) + vals[i1] * f;
      out[c] = Math.max(0, Math.min(1, v / gmax));
    }
    return out;
  }

  function setupAfterLoad() {
    idx = 0;
    stop();
    const scrub = $id('replay-scrub');
    scrub.min = 0;
    scrub.max = Math.max(0, frames.length - 1);
    scrub.value = 0;
    scrub.disabled = frames.length < 2;
    $id('replay-play').disabled = frames.length < 2;
    updateLabel();
    updateInfo();
    redraw();
  }

  function updateLabel() {
    $id('replay-framelbl').textContent =
      frames.length ? `${idx + 1} / ${frames.length}` : '— / —';
  }
  function updateInfo() {
    const info = $id('replay-info');
    if (!frames.length) return;
    let span = '';
    if (wavelengths && wavelengths.length > 1) {
      const lo = Math.min(wavelengths[0], wavelengths[wavelengths.length - 1]);
      const hi = Math.max(wavelengths[0], wavelengths[wavelengths.length - 1]);
      if (isFinite(lo) && isFinite(hi)) span = ` · ${lo.toFixed(0)}–${hi.toFixed(0)} nm`;
    }
    info.textContent = `${frames.length} frame(s)${span} · current frame at bottom, earlier frames above`;
  }

  function seek(i) {
    if (!frames.length) return;
    idx = Math.max(0, Math.min(frames.length - 1, i));
    $id('replay-scrub').value = idx;
    updateLabel();
    redraw();
  }

  function togglePlay() { playing ? stop() : start(); }

  function start() {
    if (frames.length < 2) return;
    playing = true;
    $id('replay-play').textContent = '⏸';
    // If at the end, restart from top.
    if (idx >= frames.length - 1) seek(0);
    startTimer();
  }
  function stop() {
    playing = false;
    $id('replay-play').textContent = '▶';
    stopTimer();
  }
  function startTimer() {
    const mult = parseFloat($id('replay-speed').value) || 1;
    stopTimer();
    timer = setInterval(tick, Math.max(16, baseInterval * mult));
  }
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }

  function tick() {
    if (idx >= frames.length - 1) {
      if ($id('replay-loopchk').checked) { seek(0); }
      else { stop(); return; }
    } else {
      seek(idx + 1);
    }
  }

  // Render a window of frames as a waterfall, with the CURRENT frame at the
  // BOTTOM and preceding frames stacked above it — matching the live heatmap and
  // the desktop app (history scrolls upward).
  function redraw() {
    const canvas = $id('replay-canvas');
    if (!canvas) return;
    const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
    if (cssW <= 0 || cssH <= 0) return;
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.round(cssW * dpr), ch = Math.round(cssH * dpr);
    if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = getCss('--bg-1') || '#101317';
    ctx.fillRect(0, 0, cw, ch);
    if (!frames.length) return;

    // Up to ROWS bands: current frame at the bottom, older ones (lower index)
    // above. Fixed band count keeps band height stable across files.
    const ROWS = Math.min(frames.length, 200);
    const bandH = ch / ROWS;
    for (let r = 0; r < ROWS; r++) {
      const fi = idx - r;                 // bottom band = current frame
      if (fi < 0) break;
      const row = frames[fi];
      const y0 = Math.floor(ch - (r + 1) * bandH);
      const y1 = Math.floor(ch - r * bandH);
      const bh = Math.max(1, y1 - y0);
      const band = ctx.createImageData(cw, bh);
      for (let px = 0; px < cw; px++) {
        const c = Math.min(RCOLS - 1, Math.floor(px / cw * RCOLS));
        const [rr, gg, bb] = infernoColor(row[c]);
        for (let dy = 0; dy < bh; dy++) {
          const o = (dy * cw + px) * 4;
          band.data[o] = rr; band.data[o+1] = gg; band.data[o+2] = bb; band.data[o+3] = 255;
        }
      }
      ctx.putImageData(band, 0, y0);
    }

    // Marker line at the very bottom edge = the current frame.
    ctx.fillStyle = getCss('--accent') || '#2dd4bf';
    ctx.fillRect(0, ch - Math.max(1, Math.round(2 * dpr)), cw,
                 Math.max(1, Math.round(2 * dpr)));
  }

  return { init, redraw };
})();

// ════════════════════════════════════════════════════════════════════
//  Filter — optical-filter characterization tab
//  --------------------------------------------------------------------
//  Capture a baseline (light source WITHOUT the filter) via "Set baseline",
//  then insert the filter: the tab plots live transmission T(λ)=sample/baseline
//  (%). Metrics (type, centre λ, FWHM, edges, steepness, OD) come from the
//  shared server engine (/api/filter → filter_analysis.analyze_filter) so web
//  and desktop report identical numbers; the live trace is computed client-side
//  for smoothness. PNG (graph) + CSV (metrics) report can be saved.
// ════════════════════════════════════════════════════════════════════
const Filter = (() => {
  const REF_FLOOR_FRAC = 0.02;        // mirror of filter_analysis.DEFAULT_REF_FLOOR_FRAC
  const METRIC_THROTTLE_MS = 700;
  let plot = null;
  let baseline = null;                // { wl:Float64Array, ys:Float64Array }
  let latest = null;                  // { wl, ys }
  let result = null;                  // last /api/filter result (has .rows)
  let capture = null;                 // in-progress baseline averaging | null
  let data = [[], [], []];            // x, T%, 50%-guide
  let lastFetch = 0, pending = false;

  // Display-only moving average (null-gap aware) for the live transmission curve.
  // Metrics come from the server on raw data, so this never affects the numbers.
  function smoothFinite(arr, window) {
    const w = (window | 0) < 3 ? 0 : ((window | 0) % 2 ? (window | 0) : (window | 0) + 1);
    if (!w) return arr;
    const n = arr.length, half = w >> 1, out = new Array(n);
    for (let i = 0; i < n; i++) {
      let s = 0, c = 0;
      for (let j = Math.max(0, i - half); j <= Math.min(n - 1, i + half); j++) {
        const v = arr[j];
        if (v != null && isFinite(v)) { s += v; c++; }
      }
      out[i] = (arr[i] == null) ? null : (c ? s / c : null);   // keep masked gaps
    }
    return out;
  }

  // Hover crosshair → top-bar readout (pixel / wavelength / sample intensity),
  // mirroring the scope tab so the readout isn't blank on the filter tab.
  function readoutPlugin() {
    return { hooks: { setCursor(u) {
      const idx = u.cursor.idx;
      if (idx == null || !latest) {
        $('#ro-pixel').textContent = '—';
        $('#ro-wave').innerHTML = '—<span class="unit"> nm</span>';
        $('#ro-int').innerHTML  = '—<span class="unit"> ADC</span>';
        return;
      }
      const wl = latest.wl?.[idx], it = latest.ys?.[idx];
      if (wl == null || it == null) return;
      $('#ro-pixel').textContent = idx;
      $('#ro-wave').innerHTML = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
      $('#ro-int').innerHTML  = `${it.toFixed(0)}<span class="unit"> ADC</span>`;
    }}};
  }

  function markersPlugin() {
    return { hooks: { draw: u => {
      if (!result) return;
      const ctx = u.ctx;
      const edges = [result.left_edge_nm, result.right_edge_nm,
                     result.cut_on_nm, result.cut_off_nm].filter(v => v != null);
      ctx.save();
      ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.strokeStyle = '#d8b46a';
      for (const e of edges) {
        const x = u.valToPos(e, 'x', true);
        ctx.beginPath();
        ctx.moveTo(x, u.bbox.top);
        ctx.lineTo(x, u.bbox.top + u.bbox.height);
        ctx.stroke();
      }
      const c = result.center_wavelength_nm;
      if (c != null) {
        ctx.setLineDash([2, 3]); ctx.strokeStyle = '#5ee0d6';
        const x = u.valToPos(c, 'x', true);
        ctx.beginPath();
        ctx.moveTo(x, u.bbox.top);
        ctx.lineTo(x, u.bbox.top + u.bbox.height);
        ctx.stroke();
      }
      ctx.restore();
    }}};
  }

  function filterYRange() {
    // Fixed transmission axis — a ratio in %, so it should not autorange and
    // jitter with the live signal. 0..110% gives headroom above 100%; rare
    // edge spikes from a near-zero baseline simply clip off-screen.
    return [0, 110];
  }

  function ensurePlot() {
    if (plot) return;
    const container = document.querySelector('#filter-plot');
    if (!container) return;
    const opts = {
      width:  container.clientWidth  || 800,
      height: container.clientHeight || 480,
      pxAlign: false,
      cursor: { drag: { x: true, y: false }, points: { size: 6 } },
      legend: { show: false },
      axes: [
        { stroke: getCss('--fg-3'), grid: { stroke: getCss('--border-1'), width: 1 },
          ticks: { stroke: getCss('--border-2') }, font: '11px JetBrains Mono',
          labelFont: '10px Inter', label: 'Wavelength (nm)',
          values: (u, sp) => sp.map(v => v.toFixed(0)) },
        { stroke: getCss('--fg-3'), grid: { stroke: getCss('--border-1'), width: 1 },
          ticks: { stroke: getCss('--border-2') }, font: '11px JetBrains Mono',
          labelFont: '10px Inter', label: 'Transmission (%)' },
      ],
      series: [
        {},
        { stroke: '#5ee0d6', width: 2, spanGaps: false, points: { show: false } },
        { stroke: getCss('--fg-3') || '#80848e', width: 1, dash: [4, 4],
          spanGaps: true, points: { show: false } },
      ],
      scales: {
        x: { time: false, range: () => xRange() },
        y: { range: () => filterYRange() },
      },
      plugins: [ markersPlugin(), wheelZoomPlugin(), readoutPlugin() ],
    };
    plot = new uPlot(opts, data, container);
    const fit = () => {
      if (!plot) return;
      const w = container.clientWidth, h = container.clientHeight;
      if (w > 0 && h > 0 && (w !== plot.width || h !== plot.height))
        plot.setSize({ width: w, height: h });
    };
    requestAnimationFrame(fit);
    if (window.ResizeObserver) new ResizeObserver(fit).observe(container);
    window.addEventListener('resize', fit);
  }

  function alignedBaseline(wl) {
    if (!baseline) return null;
    if (baseline.ys.length === wl.length) return baseline.ys;
    return interpToAxis(baseline.wl, baseline.ys, wl);   // device axis changed
  }

  function computeAndDraw() {
    if (!baseline || !latest) return;
    ensurePlot();
    const wl = latest.wl, smp = latest.ys;
    const ref = alignedBaseline(wl);
    const n = wl.length;
    let refMax = 0;
    for (let i = 0; i < n; i++) if (ref[i] != null && ref[i] > refMax) refMax = ref[i];
    const floor = REF_FLOOR_FRAC * refMax;
    const T = new Array(n), fifty = new Array(n);
    for (let i = 0; i < n; i++) {
      fifty[i] = 50;
      const r = ref[i];
      T[i] = (r != null && r > floor) ? Math.max(0, (smp[i] / r) * 100) : null;
    }
    data = [wl, T, fifty];
    if (plot && State.activeTab === 'filter') {
      // Display-only smoothing (filter_display_smoothing > 1); metrics stay raw.
      const sm = State.cfg.filter_display_smoothing | 0;
      const shown = sm > 1 ? [wl, smoothFinite(T, sm), fifty] : data;
      plot.setData(shown);
    }
  }

  function maybeFetchMetrics() {
    if (!baseline || !latest) return;
    const now = performance.now();
    if (pending || now - lastFetch < METRIC_THROTTLE_MS) return;
    pending = true; lastFetch = now;
    const wl = latest.wl;
    const ref = alignedBaseline(wl);
    fetch('/api/filter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        wavelengths: Array.from(wl),
        reference:   Array.from(ref),
        sample:      Array.from(latest.ys),
      }),
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(res => {
        result = res; renderMetrics(res);
      })
      .catch(() => {})
      .finally(() => { pending = false; });
  }

  function renderMetrics(res) {
    const host = document.querySelector('#filter-metrics');
    if (!host) return;
    const rows = res.rows || [];
    host.innerHTML = rows.map(([k, v]) =>
      `<div class="card"><div class="lbl">${k}</div><div class="val">${v}</div></div>`
    ).join('');
    const note = document.querySelector('#filter-note');
    if (note) note.textContent = res.notes || '';
  }

  function finishBaseline(averaged) {
    document.querySelector('#btn-filter-baseline').disabled = false;
    document.querySelector('#btn-filter-clear').disabled = false;
    document.querySelector('#btn-filter-pdf').disabled = false;
    document.querySelector('#btn-filter-png').disabled = false;
    document.querySelector('#btn-filter-csv').disabled = false;
    document.querySelector('#filter-state').textContent =
      averaged > 1 ? `Baseline set (avg ${averaged})` : 'Baseline set';
    const hint = document.querySelector('#filter-hint');
    if (hint) hint.style.display = 'none';
    computeAndDraw();
    lastFetch = 0; maybeFetchMetrics();
  }

  function setBaseline() {
    if (!latest || !latest.ys || latest.ys.length === 0) {
      toast('No live spectrum yet — connect a device first.');
      return;
    }
    const n = Math.max(1, State.cfg.filter_baseline_frames | 0 || 16);
    if (n <= 1) {
      baseline = { wl: Float64Array.from(latest.wl), ys: Float64Array.from(latest.ys) };
      finishBaseline(1);
      return;
    }
    // Average the next N live frames into the reference (noise ~/√N, no loss of
    // spectral resolution since the reference is static). Mirrors the desktop app.
    capture = { wl: null, sum: null, count: 0, target: n };
    document.querySelector('#btn-filter-baseline').disabled = true;
    document.querySelector('#filter-state').textContent = `Averaging… 0/${n}`;
  }

  function accumulateBaseline(wl, ys) {
    if (!capture.sum || capture.sum.length !== ys.length) {
      capture.wl = Float64Array.from(wl);
      capture.sum = Float64Array.from(ys);
      capture.count = 1;
    } else {
      for (let i = 0; i < ys.length; i++) capture.sum[i] += ys[i];
      capture.count++;
    }
    document.querySelector('#filter-state').textContent =
      `Averaging… ${capture.count}/${capture.target}`;
    if (capture.count >= capture.target) {
      const mean = new Float64Array(capture.sum.length);
      for (let i = 0; i < mean.length; i++) mean[i] = capture.sum[i] / capture.count;
      baseline = { wl: capture.wl, ys: mean };
      const avg = capture.target; capture = null;
      finishBaseline(avg);
    }
  }

  function clearBaseline() {
    capture = null;
    baseline = null; result = null;
    data = [[], [], []];
    if (plot) plot.setData(data);
    document.querySelector('#btn-filter-baseline').disabled = false;
    document.querySelector('#btn-filter-clear').disabled = true;
    document.querySelector('#btn-filter-pdf').disabled = true;
    document.querySelector('#btn-filter-png').disabled = true;
    document.querySelector('#btn-filter-csv').disabled = true;
    document.querySelector('#filter-state').textContent = 'No baseline';
    document.querySelector('#filter-metrics').innerHTML = '';
    const note = document.querySelector('#filter-note');
    if (note) note.textContent = '';
    const hint = document.querySelector('#filter-hint');
    if (hint) hint.style.display = '';
  }

  function onFrame(wl, ys) {
    latest = { wl, ys };
    if (capture) { accumulateBaseline(wl, ys); return; }
    if (State.activeTab !== 'filter' || !baseline) return;
    computeAndDraw();
    maybeFetchMetrics();
  }

  function onShow() {
    ensurePlot();
    requestAnimationFrame(() => {
      const c = document.querySelector('#filter-plot');
      if (plot && c && c.clientWidth > 0 && c.clientHeight > 0)
        plot.setSize({ width: c.clientWidth, height: c.clientHeight });
      if (baseline) computeAndDraw();
    });
  }

  function saveReport(fmt) {
    if (!baseline || !latest) { toast('Set a baseline first'); return; }
    const wl = latest.wl, ref = alignedBaseline(wl);
    fetch('/api/filter/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        format: fmt,
        wavelengths: Array.from(wl),
        reference:   Array.from(ref),
        sample:      Array.from(latest.ys),
      }),
    })
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(blob => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `filter_report_${new Date().toISOString().replace(/[:.]/g,'').slice(0,15)}.${fmt}`;
        a.click();
        URL.revokeObjectURL(a.href);
      })
      .catch(err => toast(err === 503
        ? 'Report needs matplotlib on the server' : 'Report export failed'));
  }

  function init() { /* plot is created lazily on first show */ }

  return { init, onFrame, onShow, setBaseline, clearBaseline, saveReport };
})();

// ════════════════════════════════════════════════════════════════════
//  Tm30 — TM-30-18 (Rf/Rg, hue bins, colour vector graphic) + CQS sub-tab
//  --------------------------------------------------------------------
//  Metrics come from the shared server engine (/api/tm30 → colour-science) so
//  web and desktop report identical numbers. Heavy (~0.1 s), so fetched on a
//  throttle only while the TM-30 sub-tab is visible.
// ════════════════════════════════════════════════════════════════════
const Tm30 = (() => {
  const THROTTLE_MS = 1200;
  let last = 0, pending = false, data = null, binColors = null;

  function onShow() { last = 0; render(); fetchNow(); }
  function onFrame() { fetchNow(); }

  function fetchNow() {
    const wl = State.scopeData[SI.X], ys = State.scopeData[SI.LIVE];
    if (!wl || !wl.length) return;
    const now = performance.now();
    if (pending || now - last < THROTTLE_MS) return;
    pending = true; last = now;
    fetch('/api/tm30', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wavelengths: Array.from(wl), intensities: Array.from(ys) }),
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(j => { data = j.tm30; data._cqs = j.cqs; binColors = j.bin_colors || null;
                   const h = $('#tm30-hint'); if (h) h.style.display = 'none'; render(); })
      .catch(err => {
        const h = $('#tm30-hint');
        if (h) { h.style.display = ''; h.textContent = (err === 503)
          ? 'TM-30 needs the optional colour-science package on the server.'
          : 'Waiting for a spectrum…'; }
      })
      .finally(() => { pending = false; });
  }

  function render() {
    renderCards();
    drawCVG();
    drawSamples();
    drawBins();
  }

  function card(label, val, title) {
    return `<div class="card" title="${title || ''}"><div class="lbl">${label}</div>` +
           `<div class="val">${val}</div></div>`;
  }
  function renderCards() {
    const host = $('#tm30-cards'); if (!host) return;
    if (!data) { host.innerHTML = ''; return; }
    const n = (v, d = 0) => (v == null || !isFinite(v)) ? '—' : v.toFixed(d);
    const cq = data._cqs || {};
    host.innerHTML =
      card('TM-30 Rf', n(data.Rf), 'Fidelity index — how accurately colours are rendered vs the reference (99 samples). 100 = identical.') +
      card('TM-30 Rg', n(data.Rg), 'Gamut index — overall saturation vs the reference. >100 = more saturated, <100 = duller.') +
      card('CQS Qa', n(cq.Qa), 'Color Quality Scale (NIST) — 15 saturated samples, blends fidelity & preference.') +
      card('CQS Qf', n(cq.Qf), 'CQS fidelity component.') +
      card('CQS Qg', n(cq.Qg), 'CQS gamut component.') +
      card('CCT', n(data.CCT) + ' K', 'Correlated colour temperature (from the TM-30 computation).') +
      card('Duv', (data.Duv == null || !isFinite(data.Duv)) ? '—' : (data.Duv >= 0 ? '+' : '') + data.Duv.toFixed(4), 'Distance from the Planckian locus.');
  }

  function binColor(j) { return (binColors && binColors[j % 16]) || TCS_COLORS[j % 15] || '#888'; }

  function drawCVG() {
    const cv = $('#tm30-cvg'); if (!cv) return;
    const p = prepCanvas(cv); if (!p) return;
    const { ctx, W, H } = p;
    ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 24;
    const grid = getCss('--border-1') || '#2a2e37';
    const fg3 = getCss('--fg-3') || '#80848e';
    // reference circle + guides
    ctx.strokeStyle = grid; ctx.lineWidth = 1;
    for (const r of [0.8, 1.0, 1.2]) {
      ctx.beginPath(); ctx.arc(cx, cy, R * r, 0, 2 * Math.PI);
      ctx.strokeStyle = r === 1.0 ? fg3 : grid; ctx.stroke();
    }
    ctx.strokeStyle = grid;
    ctx.beginPath(); ctx.moveTo(cx - R * 1.3, cy); ctx.lineTo(cx + R * 1.3, cy);
    ctx.moveTo(cx, cy - R * 1.3); ctx.lineTo(cx, cy + R * 1.3); ctx.stroke();
    if (!data || !data.avg_ref || !data.avg_test) return;
    const ref = data.avg_ref, test = data.avg_test, N = ref.length;
    const norm = j => Math.hypot(ref[j][0], ref[j][1]) || 1;
    const P = (vx, vy, j) => { const k = norm(j); return [cx + R * vx / k, cy - R * vy / k]; };
    // test polygon (red)
    ctx.beginPath();
    for (let j = 0; j <= N; j++) {
      const jj = j % N, [px, py] = P(test[jj][0], test[jj][1], jj);
      if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = '#e0314a'; ctx.lineWidth = 1.6; ctx.stroke();
    // arrows ref→test, coloured by bin
    for (let j = 0; j < N; j++) {
      const [rx, ry] = P(ref[j][0], ref[j][1], j);
      const [tx, ty] = P(test[j][0], test[j][1], j);
      ctx.strokeStyle = ctx.fillStyle = binColor(j); ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.moveTo(rx, ry); ctx.lineTo(tx, ty); ctx.stroke();
      const a = Math.atan2(ty - ry, tx - rx), hl = 5;
      ctx.beginPath(); ctx.moveTo(tx, ty);
      ctx.lineTo(tx - hl * Math.cos(a - 0.4), ty - hl * Math.sin(a - 0.4));
      ctx.lineTo(tx - hl * Math.cos(a + 0.4), ty - hl * Math.sin(a + 0.4));
      ctx.closePath(); ctx.fill();
    }
  }

  function drawSamples() {
    const cv = $('#tm30-samples'); if (!cv) return;
    const p = prepCanvas(cv); if (!p) return;
    const { ctx, W, H } = p;
    ctx.clearRect(0, 0, W, H);
    if (!data || !data.Rs) return;
    const Rs = data.Rs, bins = data.bins || [], n = Rs.length;
    const padB = 16, padT = 8, h = H - padB - padT;
    const yOf = v => padT + h * (1 - Math.max(0, Math.min(110, v)) / 110);
    const grid = getCss('--border-1') || '#2a2e37', fg3 = getCss('--fg-3') || '#80848e';
    ctx.strokeStyle = grid; ctx.fillStyle = fg3; ctx.font = '9px JetBrains Mono';
    ctx.textAlign = 'right';
    for (const lv of [0, 20, 40, 60, 80, 100]) {
      const y = yOf(lv); ctx.beginPath(); ctx.moveTo(28, y); ctx.lineTo(W, y); ctx.stroke();
      ctx.fillText(String(lv), 24, y + 3);
    }
    const bw = (W - 30) / n;
    const hex = (data.Rs_hex && data.Rs_hex.length === n) ? data.Rs_hex : null;
    for (let i = 0; i < n; i++) {
      const x = 30 + i * bw, y = yOf(Rs[i]);
      // Per-sample colour: the CES sample's true (reference) colour when the
      // server provides it, else a continuous red→magenta hue ramp.
      ctx.fillStyle = hex ? hex[i]
        : `hsl(${((i / Math.max(n - 1, 1)) * 300).toFixed(1)},70%,58%)`;
      ctx.fillRect(x, y, Math.max(1, bw - 0.5), yOf(0) - y);
    }
  }

  function drawBins() {
    const cv = $('#tm30-bins'); if (!cv) return;
    const p = prepCanvas(cv); if (!p) return;
    const { ctx, W, H } = p;
    ctx.clearRect(0, 0, W, H);
    if (!data || !data.Rcshj) return;
    const v = data.Rcshj, N = v.length;          // chroma shift in %
    const mid = H / 2, scale = (H / 2 - 14) / 20; // ±20% full scale
    const grid = getCss('--border-1') || '#2a2e37', fg3 = getCss('--fg-3') || '#80848e';
    ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(28, mid); ctx.lineTo(W, mid); ctx.stroke();
    ctx.fillStyle = fg3; ctx.font = '9px JetBrains Mono'; ctx.textAlign = 'right';
    ctx.fillText('+20%', 24, 12); ctx.fillText('0', 24, mid + 3); ctx.fillText('−20%', 24, H - 4);
    const bw = (W - 34) / N;
    for (let j = 0; j < N; j++) {
      const x = 32 + j * bw;
      const val = Math.max(-20, Math.min(20, v[j] || 0));
      const y = mid - val * scale;
      ctx.fillStyle = binColor(j);
      ctx.fillRect(x, Math.min(mid, y), Math.max(1, bw - 2), Math.abs(mid - y));
    }
  }

  return { onShow, onFrame };
})();
