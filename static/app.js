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
  SRC:    7 + PERSIST_N,        // reference-source overlay (server-scaled, violet dashed)
  RAW:    8 + PERSIST_N,        // averaged raw ADC curve (faint, own right-hand axis)
};
const SERIES_COUNT = SI.RAW + 1;   // 15 total (1 x + 14 y)
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
  specRange: { min: null, max: null },  // manufacturer-guaranteed range (nm);
                             // data outside it is shaded on the scope.
  validRange: null,          // [lo, hi] nm of the active calibration profile
                             // (null = none); outside it is shaded grey.
  unit: 'counts',            // unit of msg.intensities (from the frame)
  unitLabel: 'counts',
  quantity: 'irradiance',    // measurement quantity of the data (lx vs. cd/m²)
  dispUnit: 'counts',        // unit of the plotted `display` values
  dispUnitLabel: 'counts',
  displayModeEff: null,      // effective display mode of the last frame
  displayModes: [],          // [{value,label,available}] from the frame
  rawData: null,             // averaged raw ADC per pixel (frame.raw)
  exposure: null,            // frame.exposure {peak_pct_fs, saturated, ...}
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
  const tk = Access.token();
  const q = tk ? `?token=${encodeURIComponent(tk)}` : '';
  const ws = new WebSocket(`${proto}://${location.host}/ws/stream${q}`);
  State.ws = ws;
  ws.onopen = () => {
    State.wsConnected = true;
    $('#sb-ws').textContent = 'connected';
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'hello') handleHello(msg);
      else if (msg.type === 'frame') handleFrame(msg);
      else if (msg.type === 'config' && msg.config) {
        // Live server-driven config update (e.g. auto-exposure). Merge and
        // reflect in the UI without echoing back to the server.
        Object.assign(State.cfg, msg.config);
        applyConfigToUI(State.cfg);
        Hm.onConfig(msg.config);
      }
      else if (msg.type === 'job') Jobs.onWsJob(msg.data);
      else if (msg.type === 'control') Access.setControl(msg.data);
      else if (msg.type === 'calibration') Cal.onFrameStatus(msg.data, true);
      else if (msg.type === 'autosave') Hm.onAutosave(msg.data);
      else if (msg.type === 'status') refreshStatus();
      else if (msg.type === 'auth') Access.setAuthorized(!!msg.authorized);
      else if (msg.type === 'error') {
        if (msg.code === 'unauthorized') Access.setAuthorized(false, 'Token required.');
        else toast(friendlyError(msg.code, msg.message), 3500);
      }
    } catch (e) {
      console.warn('Bad WS message', e);
    }
  };
  ws.onclose = () => {
    if (State.ws !== ws) return;           // superseded by a reconnect with a new token
    State.wsConnected = false;
    $('#sb-ws').textContent = 'reconnecting…';
    setTimeout(connectWS, 1500);
  };
  ws.onerror = () => ws.close();
}

// Re-open the stream (new token).
function reconnectWS() {
  const old = State.ws;
  State.ws = null;
  if (old) { try { old.close(); } catch (e) { /* ignore */ } }
  connectWS();
}

function sendWS(obj) {
  if (State.ws && State.ws.readyState === 1) {
    State.ws.send(JSON.stringify(obj));
  }
}

function handleHello(msg) {
  State.cfg = msg.config || {};
  State.hardwareConnected = !!msg.connected;
  Access.onHello(msg);
  if (Array.isArray(msg.int_bounds_us)) applyIntBounds(msg.int_bounds_us);
  applyConfigToUI(State.cfg);
  Hm.onConfig(State.cfg);
  updateConnectButton();
  loadReferenceNames();
  Hm.sync();

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
  // The scope plots the DISPLAY values (mode dependent); evaluations (colour,
  // filter, CSV) keep using msg.intensities.
  const disp = Array.isArray(msg.display) ? msg.display : msg.intensities;
  State.scopeData[1] = disp;
  DisplayMode.onFrame(msg);
  // Manufacturer-guaranteed range for out-of-spec shading (may be null).
  if (msg.spec_min !== undefined) State.specRange.min = msg.spec_min;
  if (msg.spec_max !== undefined) State.specRange.max = msg.spec_max;
  // Calibration: unit of the data + valid range of the active v2 profile.
  State.validRange = Array.isArray(msg.valid_range_nm) ? msg.valid_range_nm : null;
  State.validSegments = Array.isArray(msg.valid_segments_nm) ? msg.valid_segments_nm : null;
  State.luxCal = msg.lux_calibration || null;
  State.quantity = msg.quantity || 'irradiance';
  if (msg.unit !== undefined && (msg.unit !== State.unit || msg.unit_label !== State.unitLabel)) {
    State.unit = msg.unit;
    State.unitLabel = msg.unit_label || msg.unit;
    updateYAxisLabel();
  }
  Cal.onFrameStatus(msg.calibration);
  // Reference overlay: scale the normalised (0..1) library spectrum to 90% of
  // the current Y maximum so it always fits the displayed signal. Aligned to
  // the device axis at fetch time; null-filled (drawn as a gap) when off or on
  // a length mismatch (e.g. mid device-switch, before re-fetch).
  State.scopeData[SI.REF] = buildReferenceSeries(disp);
  // CSV background, freeze, difference, peak-hold envelope and the persistence
  // trail — all computed here so a single setData() draws everything in step.
  updateOverlays(disp);
  try { RefOverlay.request(false); } catch (e) { /* overlay is optional */ }

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
  rollHeatmap(disp);

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
    spanGaps: false,            // null = masked pixel (outside profile range) -> gap
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
  s[SI.SRC] = {                 // reference-source overlay (lamp file), already in display unit
    stroke: '#c084fc', width: 1.5, dash: [8, 4], spanGaps: true, points: { show: false },
  };
  s[SI.RAW] = {                 // raw ADC (averaged), faint, on the right-hand 'raw' axis
    stroke: 'rgba(154,160,166,0.5)', width: 1, spanGaps: true, scale: 'raw', points: { show: false },
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
      {
        scale: 'raw', side: 1, show: false,
        stroke: getCss('--fg-4'),
        grid: { show: false },
        ticks: { stroke: getCss('--border-2') },
        font: '11px JetBrains Mono',
        labelFont: '10px Inter',
        label: 'Raw (ADC)',
      },
    ],
    series: buildScopeSeries(),
    scales: {
      // x is WAVELENGTH (nm), not time. Without time:false uPlot renders the
      // x-axis as Unix timestamps (the "1:07am 1/1/70" axis) and mis-places
      // every point.
      x: { time: false, range: () => xRange() },
      y: { range: () => autoYRange() },
      raw: { range: () => rawRange() },
    },
    plugins: [
      outOfSpecPlugin(),
      validRangePlugin(),
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

// Y-axis title follows the display unit and mode of the frame.
function yAxisTitle() { return DisplayMode.yTitle(); }
function updateYAxisLabel() {
  const u = State.scopePlot;
  if (!u || !u.axes[1]) return;
  u.axes[1].label = yAxisTitle();
  u.redraw(true, true);
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
  // Non-count units (irradiance, relative, ...) can be << 1: no ADC-sized floor.
  const isCount = DisplayMode.isCountUnit(State.dispUnit);
  const fs = (State.exposure && Number(State.exposure.full_scale)) || 4000;
  const manualMax = Number(State.cfg.y_max_adc);
  if (State.cfg.auto_y === false && manualMax > 0) {
    return [isCount ? Math.min(-20, mn * 1.1) : Math.min(0, mn * 1.1), manualMax];
  }
  if (!isCount) return [Math.min(0, mn * 1.1), (mx > 0 ? mx : 1) * 1.1];
  const lo = Math.min(-20, mn * 1.1);
  const live = State.scopeData[SI.LIVE];
  if ((!live || live.length === 0) && !State.freeze) return [lo, fs];
  return [lo, Math.max(50, mx * 1.1)];
}

// Range of the secondary raw-ADC axis (follows the raw curve itself).
function rawRange() {
  const arr = State.scopeData[SI.RAW];
  let mx = 0;
  if (arr) for (let i = 0; i < arr.length; i++) { const v = arr[i]; if (v != null && isFinite(v) && v > mx) mx = v; }
  return [-20, Math.max(50, mx * 1.1)];
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

// Shade the wavelength ranges the manufacturer doesn't guarantee (data still
// arrives from the sensor, but accuracy isn't specified there). Drawn in the
// drawClear hook so the translucent-red bands sit BEHIND the spectrum; a
// brighter line marks the exact spec edge. Mirrors the desktop app's overlay.
// Reads State.specRange, populated per frame from the server payload.
function outOfSpecPlugin() {
  return { hooks: { drawClear: u => {
    const sr = State.specRange || {};
    const lo = sr.min, hi = sr.max;
    if (lo == null && hi == null) return;
    const ctx = u.ctx;
    const { left, top, width, height } = u.bbox;
    const xmin = u.scales.x.min, xmax = u.scales.x.max;
    if (xmin == null || xmax == null) return;
    ctx.save();
    ctx.fillStyle = 'rgba(230,60,60,0.04)';
    // left band: from the plot's left edge up to spec-min
    if (lo != null && lo > xmin) {
      const x1 = Math.round(u.valToPos(Math.min(lo, xmax), 'x', true));
      if (x1 > left) ctx.fillRect(left, top, x1 - left, height);
    }
    // right band: from spec-max to the plot's right edge
    if (hi != null && hi < xmax) {
      const x0 = Math.round(u.valToPos(Math.max(hi, xmin), 'x', true));
      if (left + width > x0) ctx.fillRect(x0, top, left + width - x0, height);
    }
    // exact spec-edge boundary lines
    ctx.strokeStyle = 'rgba(230,60,60,0.31)';
    ctx.lineWidth = 1;
    for (const val of [lo, hi]) {
      if (val == null || val <= xmin || val >= xmax) continue;
      const x = Math.round(u.valToPos(val, 'x', true)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
    }
    ctx.restore();
  } } };
}

// Grey shading outside the valid range of the active calibration profile
// (no calibrated values there: the trace shows a gap). Same drawClear approach
// as the out-of-spec bands; reads State.validRange from the frame payload.
function validRangePlugin() {
  return { hooks: { drawClear: u => {
    const vr = State.validRange;
    if (!vr || vr.length < 2) return;
    const lo = vr[0], hi = vr[1];
    const ctx = u.ctx;
    const { left, top, width, height } = u.bbox;
    const xmin = u.scales.x.min, xmax = u.scales.x.max;
    if (xmin == null || xmax == null) return;
    ctx.save();
    ctx.fillStyle = 'rgba(128,128,128,0.16)';
    // Gaps BETWEEN valid segments (valid_segments_nm) are shaded the same way.
    const segs = State.validSegments;
    if (Array.isArray(segs) && segs.length > 1) {
      for (let i = 0; i + 1 < segs.length; i++) {
        const g0 = Number(segs[i][1]), g1 = Number(segs[i + 1][0]);
        if (!isFinite(g0) || !isFinite(g1) || g1 <= g0) continue;
        if (g1 <= xmin || g0 >= xmax) continue;
        const xa = Math.round(u.valToPos(Math.max(g0, xmin), 'x', true));
        const xb = Math.round(u.valToPos(Math.min(g1, xmax), 'x', true));
        if (xb > xa) ctx.fillRect(xa, top, xb - xa, height);
      }
    }
    if (lo != null && lo > xmin) {
      const x1 = Math.round(u.valToPos(Math.min(lo, xmax), 'x', true));
      if (x1 > left) ctx.fillRect(left, top, x1 - left, height);
    }
    if (hi != null && hi < xmax) {
      const x0 = Math.round(u.valToPos(Math.max(hi, xmin), 'x', true));
      if (left + width > x0) ctx.fillRect(x0, top, left + width - x0, height);
    }
    ctx.restore();
  } } };
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
    const r = await apiFetch('/api/references').then(r => r.json());
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
    const r = await apiFetch('/api/reference?name=' + encodeURIComponent(name))
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
  // Reference-source overlay (values scaled by the server to the display unit).
  State.scopeData[SI.SRC] = RefOverlay.series(live);

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
          $('#ro-int').innerHTML   = `—<span class="unit"> ${esc(DisplayMode.unitText())}</span>`;
          return;
        }
        const idx = u.cursor.idx;
        const wl = State.scopeData[0]?.[idx];
        const it = State.scopeData[1]?.[idx];
        if (wl == null) return;
        $('#ro-pixel').textContent = idx;
        $('#ro-wave').innerHTML  = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
        $('#ro-int').innerHTML   = DisplayMode.fmtUnit(it);   // "–" for masked pixels
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
    // Markers sit on the plotted (display) curve, whatever the display mode.
    const dxs = State.scopeData[0], dys = State.scopeData[1];
    let pv = p.intensity_adc;
    if (dxs && dys && dxs.length === dys.length && dxs.length) {
      let bi = 0, bd = Infinity;
      for (let i = 0; i < dxs.length; i++) { const d = Math.abs(dxs[i] - p.wavelength_nm); if (d < bd) { bd = d; bi = i; } }
      if (dys[bi] != null && isFinite(dys[bi])) pv = dys[bi]; else continue;
    }
    const y = u.valToPos(pv, 'y');
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
    let yA = NaN, yB = NaN;
    if (ws && vs) {
      const findY = (wl) => {
        if (!ws.length) return NaN;
        let bestI = 0, bestD = Math.abs(ws[0] - wl);
        for (let i = 1; i < ws.length; i++) {
          const d = Math.abs(ws[i] - wl);
          if (d < bestD) { bestD = d; bestI = i; }
        }
        const v = vs[bestI];
        return (v == null || !isFinite(v)) ? NaN : v;
      };
      yA = findY(State.measureA);
      yB = findY(State.measureB);
    }
    const dy = Math.abs(yB - yA);
    measureBox.innerHTML =
      `A ${State.measureA.toFixed(1)} · B ${State.measureB.toFixed(1)} · ` +
      `Δλ ${dx.toFixed(2)} nm · Δy ${DisplayMode.fmt(dy)} ${DisplayMode.unitText()}`;
  }
}

// ── Heatmap (Canvas2D rolling buffer) ────────────────────────
// We keep a logical history of the last N frames, each stored at the FULL
// incoming spectrum resolution (no 512-column down-mix) together with the
// frame's wavelength axis, so the waterfall shows the device's native line
// resolution. The canvas is fully redrawn from this history every frame, so
// resizing never wipes data and the buffer always scrolls.
// Rows kept = server buffer size (State.hmSize, Hm module); default until known.
const HM_ROWS_DEFAULT = 100;
const hmRows = () => Math.max(10, State.hmSize || HM_ROWS_DEFAULT);

function initHeatmap() {
  if (State.hm && State.hm.inited) return;      // may already exist (Hm.sync / first frame)
  State.hm = {
    inited: true,
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
    $('#ro-int').innerHTML  = `—<span class="unit"> ${esc(DisplayMode.unitText())}</span>`;
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
  // Store the full-resolution normalized row plus its display values + axis so
  // the crosshair can report the true value and wavelength. The upper limit is
  // per display mode (auto per frame, or manual).
  const norm = HmLimits.norm(ys);

  State.hm.xMin = xMin; State.hm.xMax = xMax;
  // Same logging rule as the server buffer: one column per measurement
  // interval (0 = every frame).
  const iv = Number(State.cfg.heatmap_measure_interval_ms) || 0;
  const nowMs = performance.now();
  if (iv > 0 && State.hm.rows.length && nowMs - (State.hm.lastPush || 0) < iv) return;
  State.hm.lastPush = nowMs;
  State.hm.rows.unshift({ norm, raw: ys, xs, t: nowMs });   // index 0 = newest (drawn at bottom)
  if (State.hm.rows.length > hmRows()) State.hm.rows.length = hmRows();
  Hm.onRows();

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
  const bandH = ch / hmRows();
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

  // Rewind marker: the frame picked with the slider (index 0 = newest).
  if (State.hm.pin != null && State.hm.pin < hmRows()) {
    const py = Math.round(ch - (State.hm.pin + 0.5) * bandH);
    ctx.save();
    ctx.strokeStyle = getCss('--warn') || '#d8b46a';
    ctx.lineWidth = Math.max(1, dpr);
    ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(cw, py); ctx.stroke();
    ctx.restore();
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
                          Math.max(0, Math.floor((1 - cur.y / cssH) * hmRows())));
    const row = rows[rIdx];
    let label = '';
    if (row) {
      const idx = _hmIndexForX(cx, cw, row.xs, xMin, xMax);
      const wl = row.xs[idx], adc = row.raw[idx];
      const age = rIdx;   // 0 = most recent (bottom); older rows increase upward
      label = `${wl.toFixed(1)} nm · ${DisplayMode.fmt(adc)} ${DisplayMode.unitText()} · −${age} frame${age === 1 ? '' : 's'}`;
      // Also drive the top-bar readout, like the scope tab.
      $('#ro-pixel').textContent = idx;
      $('#ro-wave').innerHTML = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
      $('#ro-int').innerHTML  = DisplayMode.fmtUnit(adc);
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
  const wl = State.scopeData[SI.X];
  const ys = (State.lastFrame && State.lastFrame.intensities) || State.scopeData[SI.LIVE];
  if (!wl || !wl.length) { toast('No spectrum yet'); return; }
  apiFetch('/api/color/report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format: fmt,
      wavelengths: Array.from(wl), intensities: Array.from(ys),
      lux_calibration: State.luxCal || 0, quantity: State.quantity || 'irradiance',
      unit: (State.lastFrame && State.lastFrame.unit) || null }),
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

function fmtLux(v) {
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : (a >= 1 ? v.toFixed(1) : v.toPrecision(3));
}

function updateColorTab(c) {
  const f = (k, fmt = (v) => v.toFixed(2)) =>
    (c[k] == null || !isFinite(c[k]) ? '—' : fmt(c[k]));
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
  // radiance (unit .../sr/nm): luminance in cd/m² instead of illuminance; no foot-candles
  const rad = State.quantity === 'radiance';
  $('#c-lux-lbl').textContent = rad ? 'Luminance (cd/m²)' : 'Illuminance (lx)';
  $('#c-lux').parentElement.title = rad
    ? 'Photopic luminance of the measured radiance. “—” = not available.'
    : 'Photopic illuminance. Calibrated only with an absolute profile or a Radiometric factor; otherwise an uncalibrated relative value. “—” = not available.';
  $('#c-lux').innerHTML   = rad
    ? `${f('luminance', v => fmtLux(v))}<span class="unit"> cd/m²</span>`
    : `${f('lux', v => fmtLux(v))}<span class="unit"> lx</span>`;
  $('#c-fc').innerHTML    = rad ? '—<span class="unit"> fc</span>'
    : `${f('fc', v => fmtLux(v))}<span class="unit"> fc</span>`;
  $('#c-central').innerHTML = `${f('central_nm', v => v.toFixed(1))}<span class="unit"> nm</span>`;
  $('#c-u60').textContent = f('u60', v => v.toFixed(4));
  $('#c-v60').textContent = f('v60', v => v.toFixed(4));
  $('#c-r9').textContent  = (Array.isArray(c.Ri) && c.Ri[8] != null && isFinite(c.Ri[8]))
                            ? Number(c.Ri[8]).toFixed(1) : '—';
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
      else if (key === 'exposure_ms' && typeof dv === 'number') el.value = dv.toFixed(2);
      else if (dv === null || dv === undefined) el.value = '';
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
  if ('display_mode' in cfg) { try { DisplayMode.syncSelect(); } catch (e) { /* not ready */ } }
  if ('heatmap_limits_by_mode' in cfg) { try { HmLimits.syncUi(); HmLimits.renorm(); } catch (e) { /* not ready */ } }
  if ('scope_reference_overlay' in cfg || 'scope_reference_id' in cfg) {
    try { RefOverlay.sync(); RefOverlay.request(true); } catch (e) { /* overlay is optional */ }
  }
}

// Reset integration time to 20 ms (mirrors the desktop GUI's "20 ms" button).
function resetExposure() {
  const el = $('#cfg-exposure_ms');
  let ms = 20;
  if (State.intBounds) ms = Math.max(State.intBounds[0] / 1000, Math.min(State.intBounds[1] / 1000, ms));
  if (el) el.value = ms.toFixed(2);
  pushCfg({ exposure_ms: ms });
}

// Snap Y max to the current frame peak and turn Auto-scale Y off (mirrors the
// desktop GUI's ▲ button). Reads the latest spectrum already held in scopeData.
function snapYToPeak() {
  const ys = State.scopeData[1] || [];
  const isCount = DisplayMode.isCountUnit(State.dispUnit);
  let peak = 0;
  for (const v of ys) if (v != null && isFinite(v) && v > peak) peak = v;
  peak = isCount ? Math.max(10, Math.round(peak)) : Number((peak > 0 ? peak : 1).toPrecision(4));
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
  // Manual Y limits are remembered per display mode.
  if (!State._applyingMode && State.displayModeEff && ('y_max_adc' in partial || 'auto_y' in partial)) {
    const by = Object.assign({}, State.cfg.y_limits_by_mode || {});
    const mx = Number(State.cfg.y_max_adc);
    by[State.displayModeEff] = { auto: State.cfg.auto_y !== false, min: null, max: mx > 0 ? mx : null };
    partial.y_limits_by_mode = by;
    State.cfg.y_limits_by_mode = by;
  }
  // Without an accepted token the server refuses set_config: display-only
  // settings still work locally (they live in State.cfg).
  if (Access.canWrite()) sendWS({ type: 'set_config', payload: partial });
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
  const r = await apiFetch('/api/devices?backend=' + encodeURIComponent(backend)).then(r => r.json());
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
    const r = await apiFetch('/api/disconnect', { method: 'POST' });
    if (!r.ok) { toast(await responseError(r), 3500); return; }
    State.hardwareConnected = false;
  } else {
    const path = $('#device-select').value || null;
    try {
      const backend = $('#backend-select').value || null;
      const r = await apiFetch('/api/connect', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ device_path: path, backend: backend }),
      });
      if (!r.ok) throw new Error(await responseError(r));
      State.hardwareConnected = true;
      State.paused = false;          // a fresh connect starts running
      // Re-fetch the active reference: the server interpolates onto the
      // connected device's wavelength axis, which differs per device.
      applyReference($('#cfg-reference_library')?.value || State.referenceName);
      refreshStatus();
    } catch (e) { toast('Connect failed: ' + e.message, 3500); }
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
  if (State.activeTab === 'heatmap') { Hm.exportCsv('full'); return; }
  apiDownload('/api/export/csv?tab=spectrum', 'spectrum.csv');
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
  // Masked pixels (outside the calibration's valid range) arrive as null.
  const fin = (v, d) => (v == null || !isFinite(v)) ? '' : Number(v).toFixed(d);
  const isCount = State.unit === 'counts' || State.unit === 'counts/s';
  const unitHdr = State.unit === 'counts' ? 'Intensity_ADC' : `Value_${State.unit}`;
  const TAB = String.fromCharCode(9);
  const lines = [`Wavelength_nm${TAB}${unitHdr}`];
  const wl = State.lastFrame.wavelengths || [];
  const iv = State.lastFrame.intensities || [];
  for (let i = 0; i < wl.length; i++) {
    const v = iv[i];
    const txt = isCount ? fin(v, 2) : ((v == null || !isFinite(v)) ? '' : Number(v).toPrecision(6));
    lines.push(`${fin(wl[i], 2)}${TAB}${txt}`);
  }
  navigator.clipboard.writeText(lines.join('\n'))
    .then(() => toast(`Copied ${lines.length - 1} samples`))
    .catch(() => toast('Copy failed'));
}

// ── Bootstrap ────────────────────────────────────────────────
async function init() {
  Access.loadToken();       // ?token= → localStorage; before any request / the stream
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
    tagLocks();
    Access.init();
    Jobs.init();
    Hm.init();
    Dark.init();
    CalMgr.init();
    CalRun.init();
    DataSec.init();
    WlCal.init();
    Cal.init();
    Lin.init();
    DisplayMode.init();
    HmLimits.init();
    RefOverlay.init();
  } catch (e) {
    console.error('init: wiring controls failed', e);
  }

  // Status (best-effort)
  try { await refreshStatus(true); } catch (e) { console.warn('init: /api/status failed', e); }

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

// ── Spectral calibration (schema v2) ─────────────────────────
// Profile dropdown (value = profile id), status line, delete / export /
// import / copy-to-this-device. Capture/wizard is desktop-only.
const Cal = (() => {
  let lastKey = '';
  let items = [];

  const api = async (url, opts) => {
    const r = await apiFetch(url, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { /* non-JSON body */ }
    return { r, j };
  };
  const post = (url, body) => api(url, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const err = (j, r) => errText(j, r);

  function showStatus(st) {
    const el = $('#cal-status');
    if (!el || !st) return;
    const s = st.state || 'none';
    el.className = 'cal-status ' + s;
    let txt = st.message || '';
    // wl_changed / nl_changed: the server message already names the cause(s)
    if ((s === 'wl_changed' || s === 'nl_changed') && !txt) txt = 'Calibration conditions changed – recalibrate.';
    else if (!txt && s === 'none') txt = 'No calibration active (counts).';
    else if (!txt && s === 'ok') txt = 'OK';
    if (Array.isArray(st.valid_segments_nm) && st.valid_segments_nm.length > 1) {
      txt += ' Valid ' + st.valid_segments_nm.map(
        (g) => `${Number(g[0]).toFixed(0)}–${Number(g[1]).toFixed(0)}`).join(', ') + ' nm.';
    } else if (Array.isArray(st.valid_range_nm) && st.valid_range_nm.length >= 2) {
      txt += ` Valid ${Number(st.valid_range_nm[0]).toFixed(0)}–${Number(st.valid_range_nm[1]).toFixed(0)} nm.`;
    }
    if (st.implausible) {
      txt += ' ⚠ Saved although the plausibility check failed.';
      if (el.className.indexOf('wl_changed') < 0) el.className += ' wl_changed';
    }
    if (st.legacy_note) {
      // the note is already part of st.message; only the warning colour is added
      if (txt.indexOf(st.legacy_note) < 0) txt += ' ' + st.legacy_note;
      if (el.className.indexOf('wl_changed') < 0) el.className += ' wl_changed';
    }
    if (st.dark_source === 'stored') txt += ' Dark: stored (this t_int).';
    else if (st.dark_source === 'stored_model') txt += ' Dark: per-pixel model b+r·t.';
    if (st.dark_warning) {
      txt += ' ⚠ ' + st.dark_warning;
      el.className += ' wl_changed';      // same warning colour
    }
    el.textContent = `${st.label || ''}: ${txt}`.trim();
  }

  // Frames carry the status; only re-render when it changes.
  function onFrameStatus(st, fromEvent) {
    if (!st) return;
    const key = JSON.stringify([st.state, st.profile_id, st.label, st.unit,
                                st.dark_source, st.dark_warning, st.implausible,
                                st.valid_range_nm, st.valid_segments_nm]);
    if (key === lastKey) return;
    lastKey = key;
    State.calStatus = st;
    showStatus(st);
    updateRcState();
    if (fromEvent) refresh();       // profile list may have changed (new / activated)
  }

  async function refresh() {
    const sel = $('#cal-select');
    if (!sel) return;
    const old = $('#cal-include-old').checked ? 1 : 0;
    let j;
    try { ({ j } = await api('/api/calibration/profiles?include_old=' + old)); }
    catch (e) { return; }
    if (!j || !Array.isArray(j.items)) return;
    items = j.items;
    State.calSelection = j.selection;
    sel.innerHTML = '';
    for (const it of items) {
      const warn = it.state === 'wl_changed' || it.state === 'nl_changed';
      const o = new Option(it.label + (warn ? ' ⚠' : ''), it.value);
      o.title = it.message || '';
      sel.appendChild(o);
    }
    sel.value = j.selection;
    const oth = $('#cal-other');
    oth.innerHTML = '';
    for (const o of (j.other_units || [])) oth.appendChild(new Option(`${o.serial} · ${o.label}`, o.id));
    $('#cal-copy').disabled = !(j.other_units || []).length;
    showStatus(j.status);
    if (j.status) State.calStatus = j.status;
    updateRcState();
    CalMgr.setProfiles(j);
  }

  async function select(value, confirm) {
    const { r, j } = await post('/api/calibration/select', { selection: value, confirm: !!confirm });
    if (r.status === 409 && j && j.needs_confirmation) {
      if (window.confirm((j.message || 'Wavelength calibration changed.') +
                         '\n\nApply this profile anyway?')) return select(value, true);
      return refresh();
    }
    if (!r.ok) { toast(err(j, r)); return refresh(); }
    lastKey = '';
    showStatus(j.status);
    refresh();
  }

  async function del() {
    const id = $('#cal-select').value;
    const it = items.find(x => x.value === id);
    if (!it || it.state === 'builtin') { toast('Select a saved profile first'); return; }
    if (!window.confirm(`Delete profile "${it.label}"?`)) return;
    const { r, j } = await post('/api/calibration/profile/delete', { id });
    if (!r.ok) toast(err(j, r)); else toast('Profile deleted');
    lastKey = '';
    refresh();
  }

  function exportProfile() {
    const id = $('#cal-select').value;
    const it = items.find(x => x.value === id);
    if (!it || it.state === 'builtin') { toast('Select a saved profile first'); return; }
    const a = document.createElement('a');
    a.href = '/api/calibration/profile/export?id=' + encodeURIComponent(id);
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
  }

  async function importFile(ev) {
    const f = ev.target.files && ev.target.files[0];
    ev.target.value = '';
    if (!f) return;
    let body;
    try { body = JSON.parse(await f.text()); }
    catch (e) { toast('Not a valid JSON file'); return; }
    const { r, j } = await post('/api/calibration/profile/import', body);
    toast(r.ok ? `Imported: ${j.label}` : err(j, r), 3000);
    refresh();
  }

  async function copyToDevice() {
    const id = $('#cal-other').value;
    if (!id) return;
    const { r, j } = await post('/api/calibration/profile/copy', { id });
    toast(r.ok ? `Copied: ${j.label}` : err(j, r), 3000);
    refresh();
  }

  function init() {
    const sel = $('#cal-select');
    if (!sel) return;
    sel.addEventListener('change', () => select(sel.value));
    $('#cal-include-old').addEventListener('change', refresh);
    $('#cal-delete').addEventListener('click', del);
    $('#cal-export').addEventListener('click', exportProfile);
    $('#cal-import').addEventListener('click', () => $('#cal-import-file').click());
    $('#cal-import-file').addEventListener('change', importFile);
    $('#cal-copy').addEventListener('click', copyToDevice);
    $('#sec-calibration')?.addEventListener('toggle', e => { if (e.target.open) refresh(); });
    refresh();
  }

  return { init, refresh, onFrameStatus, select, getItems: () => items };
})();

// ════════════════════════════════════════════════════════════════════
//  Web parity (docs/PARITAET_Desktop_Web.md §13): access token, control
//  owner + jobs, heatmap controls, calibration manager / runs, data settings.
//  Everything below only talks to the server; the browser computes nothing
//  the engine already delivers.
// ════════════════════════════════════════════════════════════════════
const WEB_OWNER = 'remote:web';

// Keys the engine accepts only from the control owner (engine.CONTROL_KEYS).
const CONTROL_KEYS = new Set([
  'exposure_ms', 'auto_exposure', 'frames_to_average', 'pause',
  'fast_preview_enabled', 'fast_preview_short_pct',
  'dark_correction', 'dark_mode', 'dark_bias_adc', 'dark_rate_adc_per_s',
  'hot_pixel_filter', 'smoothing_width', 'spatial_smoothing',
  'spatial_smoothing_width', 'display_unit', 'irradiance_unit',
  'radiometric_calibration_factor', 'response_correction_active',
  'fusion_enabled', 'fusion_smoothing', 'fusion_change_sigma', 'fusion_noise_floor',
  'fusion_noise_gain', 'fusion_emit_hz', 'fusion_emit_max_hz', 'fusion_emit_min_hz',
]);
// Display-only settings of this page: they keep working without a token.
const DISPLAY_KEYS = new Set([
  'x_min_nm', 'x_max_nm', 'y_max_adc', 'auto_y', 'theme', 'show_peaks',
  'y_limits_by_mode', 'heatmap_limits_by_mode',
  'measure_mode', 'measure_a_nm', 'measure_b_nm', 'reference_library',
]);

const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function showModal(id, on) {
  const m = document.getElementById(id);
  if (m) m.hidden = !on;
}

// ── fetch wrapper: bearer token + client id on every call ────
function apiFetch(url, opts) {
  const o = Object.assign({}, opts || {});
  const h = new Headers(o.headers || {});
  const tok = Access.token();
  if (tok && !h.has('Authorization')) h.set('Authorization', 'Bearer ' + tok);
  if (!h.has('X-Client-Id')) h.set('X-Client-Id', 'web');
  o.headers = h;
  return window.fetch(url, o).then((r) => {
    if (r.status === 401) Access.onUnauthorized();
    return r;
  });
}

async function apiJson(url, opts) {
  const r = await apiFetch(url, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { /* empty / non-JSON body */ }
  return { r, j };
}
const apiSend = (method, url, body) => apiJson(url, {
  method, headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});
const apiPost = (url, body) => apiSend('POST', url, body === undefined ? {} : body);
const apiPut = (url, body) => apiSend('PUT', url, body);
const apiDelete = (url) => apiJson(url, { method: 'DELETE' });

// Readable message for an error response ({error:{code,message}} or {detail}).
function errText(j, r) {
  let code = null, msg = null;
  if (j && j.error && typeof j.error === 'object') { code = j.error.code; msg = j.error.message; }
  else if (j && typeof j.detail === 'string') msg = j.detail;
  else if (j && Array.isArray(j.detail)) msg = j.detail.map((d) => d.msg).join('; ');
  else if (j && typeof j.message === 'string') msg = j.message;
  const status = r ? r.status : 0;
  return friendlyError(code, msg || (status ? `HTTP ${status}` : 'Request failed'), status);
}
async function responseError(r) {
  let j = null;
  try { j = await r.clone().json(); } catch (e) { /* ignore */ }
  return errText(j, r);
}
function friendlyError(code, msg, status) {
  msg = msg || 'Request failed';
  if (code === 'unauthorized' || status === 401) {
    return 'Not authorized: the API token is missing or invalid.';
  }
  if (code === 'control_busy' || (status === 409 && /control is held/i.test(msg))) {
    return `Busy: ${msg} Wait until it is finished, or cancel that job.`;
  }
  return msg;
}

// Download a (possibly authenticated) URL as a file.
async function apiDownload(url, fallbackName) {
  try {
    const r = await apiFetch(url);
    if (!r.ok) { toast(await responseError(r), 3500); return false; }
    const blob = await r.blob();
    let name = fallbackName || 'download';
    const cd = r.headers.get('Content-Disposition') || '';
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
    if (m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    return true;
  } catch (e) {
    toast('Download failed');
    return false;
  }
}

// ── Access: token, read-only state, control owner ────────────
const Access = (() => {
  const KEY = 'ps2600_token';
  let token = null, required = false, authorized = true;
  let control = { owner: null, reason: '' };
  let dialogOpen = false;

  function loadToken() {
    try {
      const u = new URL(location.href);
      const t = u.searchParams.get('token');
      if (t) {
        token = t;
        try { localStorage.setItem(KEY, t); } catch (e) { /* private mode */ }
        u.searchParams.delete('token');           // keep the secret out of history / copies
        history.replaceState(null, '', u.pathname + u.search + u.hash);
      }
    } catch (e) { /* ignore */ }
    if (!token) {
      try { token = localStorage.getItem(KEY) || null; } catch (e) { token = null; }
    }
  }
  function clearToken() {
    token = null;
    try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ }
  }
  const canWrite = () => !required || authorized;
  const foreign = () => !!(control && control.owner && control.owner !== WEB_OWNER);

  function controlText(c) {
    if (!c || !c.owner) return 'free';
    const why = c.reason ? ` (${c.reason})` : '';
    return `${c.owner}${why}`;
  }

  function lockedFor(kind) {
    if (!canWrite()) return true;
    return kind === 'control' && foreign();
  }

  function applyLocks() {
    const why = !canWrite() ? 'Read-only: an API token is required.'
      : `Control is held by ${controlText(control)}.`;
    document.querySelectorAll('[data-lock]').forEach((el) => {
      const l = lockedFor(el.dataset.lock);
      el.classList.toggle('locked', l);
      if (l) {
        el.setAttribute('aria-disabled', 'true');
        el.tabIndex = -1;
        if (!('title0' in el.dataset)) el.dataset.title0 = el.title || '';
        el.title = why;
      } else {
        el.removeAttribute('aria-disabled');
        el.removeAttribute('tabindex');
        if ('title0' in el.dataset) { el.title = el.dataset.title0; delete el.dataset.title0; }
      }
    });
  }

  function refresh() {
    const ro = required && !authorized;
    document.body.classList.toggle('readonly', ro);
    const chip = $('#sb-access');
    if (chip) {
      chip.hidden = !required;
      chip.className = 'chip ' + (ro ? 'warn' : 'ok');
      chip.textContent = ro ? 'read-only' : 'token ok';
      chip.style.cursor = ro ? 'pointer' : '';
      chip.title = ro ? 'No valid API token: click to enter one.' : 'API token accepted.';
    }
    const el = $('#sb-control');
    if (el) {
      el.textContent = controlText(control);
      el.style.color = foreign() ? 'var(--warn)' : '';
    }
    applyLocks();
  }

  function promptToken(msg) {
    dialogOpen = true;
    showModal('token-dialog', true);
    const e = $('#token-error'); if (e) e.textContent = msg || '';
    setTimeout(() => { const i = $('#token-input'); if (i) i.focus(); }, 30);
  }
  function closeDialog() { dialogOpen = false; showModal('token-dialog', false); }

  function onHello(msg) {
    required = !!msg.token_required;
    authorized = msg.authorized !== false;
    if (msg.control) control = msg.control;
    if (required && !authorized) {
      const had = !!token;
      if (had) clearToken();
      promptToken(had ? 'The token was rejected. Enter a valid token.' : '');
    } else {
      closeDialog();
    }
    refresh();
  }
  function onStatus(s) {
    if (!s) return;
    if ('token_required' in s) {
      required = !!s.token_required;
      authorized = !required || !!token;       // confirmed by the stream's hello
      if (required && !token && !dialogOpen) promptToken('');
    }
    if (s.control) control = s.control;
    refresh();
  }
  function setAuthorized(a, msg) {
    authorized = !!a;
    if (!authorized) promptToken(msg || 'The token was rejected.');
    refresh();
  }
  function onUnauthorized() {
    required = true; authorized = false;
    const had = !!token;
    if (had) clearToken();
    promptToken(had ? 'The token was rejected. Enter a valid token.' : '');
    refresh();
  }
  function setControl(c) { control = c || { owner: null, reason: '' }; refresh(); }

  function submit() {
    const inp = $('#token-input');
    const v = ((inp && inp.value) || '').trim();
    if (!v) { $('#token-error').textContent = 'Enter the token.'; return; }
    token = v;
    try { localStorage.setItem(KEY, v); } catch (e) { /* ignore */ }
    inp.value = '';
    closeDialog();
    reconnectWS();               // the stream authorizes via ?token=
    refreshStatus();
  }

  function init() {
    $('#token-ok').addEventListener('click', submit);
    $('#token-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    $('#token-skip').addEventListener('click', closeDialog);
    $('#sb-access').addEventListener('click', () => { if (required && !authorized) promptToken(''); });
    refresh();
  }

  return { init, loadToken, onHello, onStatus, setControl, setAuthorized, onUnauthorized,
           canWrite, foreign, applyLocks, controlText, token: () => token,
           control: () => control };
})();

// Mark config-bound controls with the kind of permission they need.
function tagLocks() {
  const els = [...$$('[id^="cfg-"]'), ...$$('input[data-key], select[data-key]'), ...$$('.toggle[data-key]')];
  for (const el of els) {
    const key = cfgKeyOf(el);
    if (!key || DISPLAY_KEYS.has(key) || el.dataset.lock) continue;
    el.dataset.lock = CONTROL_KEYS.has(key) ? 'control' : 'auth';
  }
  Access.applyLocks();
}

// ── Status / bounds / response-compensation indicator ────────
function applyIntBounds(b) {
  const lo = Number(b[0]), hi = Number(b[1]);
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
  State.intBounds = [lo, hi];
  const el = $('#cfg-exposure_ms');
  if (el) {
    el.min = (lo / 1000).toFixed(2);
    el.max = (hi / 1000).toFixed(2);
    el.step = '0.01';
    el.title = `Integration time ${(lo / 1000).toFixed(2)} – ${(hi / 1000).toFixed(2)} ms (device limits)`;
    const v = parseFloat(el.value);
    if (isFinite(v)) el.value = Math.max(lo / 1000, Math.min(hi / 1000, v)).toFixed(2);
  }
}

async function refreshStatus() {
  try {
    const { r, j: s } = await apiJson('/api/status');
    if (!r.ok || !s) return;
    $('#sb-backend').textContent = s.backend;
    State.hardwareConnected = !!s.connected;
    updateConnectButton();
    const v = $('#sb-version');
    if (v) v.textContent = s.version && s.version !== 'unknown' ? `v${String(s.version).replace(/^v/i, '')}` : '';
    if (Array.isArray(s.int_bounds_us)) applyIntBounds(s.int_bounds_us);
    if (s.heatmap_size) Hm.setSize(s.heatmap_size);
    Access.onStatus(s);
    if (s.autosave) Hm.onAutosave(s.autosave);
  } catch (e) { console.warn('status refresh failed', e); }
}

// "Response compensation" is no switch: it follows the profile selection.
function updateRcState() {
  const el = $('#rc-state');
  if (!el) return;
  const st = State.calStatus || {};
  const on = st.state === 'ok' && st.label && st.label !== 'None';
  const bad = st.state === 'wl_changed' || st.state === 'nl_changed';
  el.className = 'chip ' + (bad ? 'warn' : (on ? 'ok' : 'neutral'));
  el.textContent = bad ? 'conditions changed' : (on ? `active · ${st.label}` : 'off');
}

// ── Jobs: status bar, user-step prompt, tracking ─────────────
const Jobs = (() => {
  const FINAL = new Set(['done', 'failed', 'cancelled']);
  const active = new Map();
  const trackers = new Map();             // id → { cb, timer }
  let promptId = null, promptMsg = '';

  const TITLES = {
    cover: 'Cover the light path', uncover: 'Uncover the light path',
    mount_known: 'Mount the known optics', swap_optics: 'Mount the new optics',
    swap_back: 'Mount the known optics again',
    mount: 'Mount the cosine corrector on the sphere port',
    position: 'Set the next distance', repeat: 'Repeat the first distance',
  };

  function renderBar() {
    const wrap = $('#sb-job-wrap'), el = $('#sb-job');
    if (!wrap || !el) return;
    const list = [...active.values()];
    if (!list.length) { wrap.hidden = true; return; }
    const j = list[list.length - 1];
    const pct = Math.round(100 * (Number(j.progress) || 0));
    let txt = `${j.kind} ${pct}%`;
    if (j.step) txt += ` · ${String(j.step).replace(/_/g, ' ')}`;
    if (j.status === 'waiting_for_user') txt += ` · waiting: ${(j.prompt && j.prompt.message) || 'user action'}`;
    else if (j.message) txt += ` · ${j.message}`;
    if (j.owner && j.owner !== WEB_OWNER) txt += ` [${j.owner}]`;
    if (list.length > 1) txt += ` (+${list.length - 1})`;
    el.textContent = txt;
    el.title = txt;
    wrap.hidden = false;
  }

  function showPrompt(j) {
    promptId = j.id;
    promptMsg = (j.prompt && j.prompt.message) || '';
    $('#job-dialog-title').textContent = TITLES[j.prompt && j.prompt.action] || 'Action required';
    $('#job-dialog-msg').textContent = promptMsg;
    $('#job-dialog-job').textContent = `${j.kind}${j.step ? ' · ' + String(j.step).replace(/_/g, ' ') : ''}`;
    showModal('job-dialog', true);
    const b = $('#job-continue'); if (b) b.focus();
  }
  function hidePrompt() { promptId = null; promptMsg = ''; showModal('job-dialog', false); }

  function updatePrompt(j) {
    if (j.status === 'waiting_for_user' && j.owner === WEB_OWNER && j.prompt) {
      if (promptId !== j.id || promptMsg !== j.prompt.message) showPrompt(j);
    } else if (promptId === j.id) {
      hidePrompt();
    }
  }

  function ingest(j) {
    if (!j || !j.id) return;
    if (FINAL.has(j.status)) active.delete(j.id); else active.set(j.id, j);
    renderBar();
    updatePrompt(j);
  }

  // A job of this page: poll the full job (log, result) until it is final.
  function track(id, cb) {
    const old = trackers.get(id);
    if (old) clearTimeout(old.timer);
    const t = { cb, timer: null };
    trackers.set(id, t);
    const poll = async () => {
      let full = null;
      try {
        const { r, j } = await apiJson(`/api/v1/jobs/${encodeURIComponent(id)}`);
        if (r.ok) full = j;
      } catch (e) { /* retry */ }
      if (trackers.get(id) !== t) return;          // superseded by a newer track()
      if (full) {
        ingest(full);
        const fin = FINAL.has(full.status);
        try { cb(full, fin); } catch (e) { console.error('job callback failed', e); }
        if (fin) { trackers.delete(id); return; }
      }
      t.timer = setTimeout(poll, 800);
    };
    poll();
  }

  function onWsJob(j) {
    ingest(j);
    const t = j && trackers.get(j.id);
    if (t) { clearTimeout(t.timer); t.timer = setTimeout(() => track(j.id, t.cb), 0); }
  }

  async function answer(action) {
    if (!promptId) return;
    const id = promptId;
    const { r, j } = await apiPost(`/api/v1/jobs/${encodeURIComponent(id)}/${action}`);
    if (!r.ok) { toast(errText(j, r), 3500); return; }
    if (j) ingest(j);
    hidePrompt();
  }

  async function cancel(id) {
    const { r, j } = await apiPost(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`);
    if (!r.ok) toast(errText(j, r), 3500);
    return r.ok;
  }

  async function init() {
    $('#job-continue').addEventListener('click', () => answer('continue'));
    $('#job-cancel').addEventListener('click', () => answer('cancel'));
    try {
      const { r, j } = await apiJson('/api/v1/jobs?active=true');
      if (r.ok && Array.isArray(j)) j.slice().reverse().forEach(ingest);
    } catch (e) { /* ignore */ }
  }

  return { init, onWsJob, track, ingest, cancel };
})();

// ── Heatmap controls (buffer, interval, clear, autosave, export, rewind) ──
const Hm = (() => {
  const el = (id) => document.getElementById(id);
  let interval = 0, lastSpanAt = 0, fetchTimer = null, lastFetchAt = 0;

  function fmtDur(s) {
    if (!isFinite(s)) return '—';
    if (s < 90) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
    if (s < 5400) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
    return `${(s / 3600).toFixed(1)} h`;
  }
  function updateSpan() {
    const e = el('hm-span');
    if (!e) return;
    const fps = State.lastFrame && State.lastFrame.fps;
    const per = interval > 0 ? interval / 1000 : (fps > 0 ? 1 / fps : null);
    e.textContent = per ? `Span ≈ ${fmtDur(hmRows() * per)}` : 'Span —';
  }

  function clearRows() {
    if (State.hm) { State.hm.rows.length = 0; State.hm.pin = null; State.hm.lastPush = 0; }
    const sl = el('hm-rewind');
    if (sl) { sl.value = 0; sl.max = 0; sl.disabled = true; }
    setRewindLabel('Live');
    if (State.hm) redrawHeatmap();
  }

  function setSize(n) {
    n = Number(n);
    if (!isFinite(n) || n < 10) return;
    const changed = State.hmSize != null && State.hmSize !== n;
    State.hmSize = n;
    const b = el('hm-buffer');
    if (b && document.activeElement !== b) b.value = n;
    if (changed) clearRows();               // the server buffer is cleared by a size change
    updateSpan();
  }

  function onConfig(cfg) {
    if (!cfg) return;
    if (cfg.heatmap_buffer_size != null) setSize(cfg.heatmap_buffer_size);
    if (cfg.heatmap_measure_interval_ms != null) {
      interval = Math.max(0, Number(cfg.heatmap_measure_interval_ms) || 0);
      const i = el('hm-interval');
      if (i && document.activeElement !== i) i.value = interval;
      updateSpan();
    }
  }

  function setRewindLabel(t) { const l = el('hm-rewind-lbl'); if (l) l.textContent = t; }

  async function fetchFrame() {
    lastFetchAt = performance.now();
    const pin = State.hm && State.hm.pin;
    if (pin == null) { setRewindLabel('Live'); return; }
    const { r, j } = await apiJson(`/api/v1/heatmap/frame?index=${pin}`);
    if (State.hm.pin !== pin && State.hm.pin != null) return;   // moved on meanwhile
    if (!r.ok || !j) { setRewindLabel(`Frame ${pin}`); return; }
    let peak = '';
    const v = j.values || [], w = j.wavelengths || [];
    let bi = -1, bv = -Infinity;
    for (let i = 0; i < v.length; i++) if (v[i] != null && isFinite(v[i]) && v[i] > bv) { bv = v[i]; bi = i; }
    if (bi >= 0 && w[bi] != null) peak = ` · peak ${Number(w[bi]).toFixed(1)} nm`;
    const age = (j.age_s == null || !isFinite(j.age_s)) ? '' : `T-${Number(j.age_s).toFixed(1)} s ago · `;
    setRewindLabel(`${age}frame ${j.index}${peak}`);
  }
  function scheduleFetch() {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(fetchFrame, 120);
  }

  function onRows() {
    const hm = State.hm;
    if (!hm) return;
    const n = hm.rows.length;
    const sl = el('hm-rewind');
    if (hm.pin != null) {                    // DVR: keep following the pinned frame
      hm.pin = Math.min(hm.pin + 1, Math.max(0, n - 1));
      if (performance.now() - lastFetchAt > 1000) scheduleFetch();
    }
    if (sl) {
      sl.max = Math.max(0, n - 1);
      sl.disabled = n < 2;
      if (hm.pin != null) sl.value = hm.pin;
    }
    const now = performance.now();
    if (now - lastSpanAt > 1000) { lastSpanAt = now; updateSpan(); }
  }

  function onRewind(v) {
    if (!State.hm) return;
    State.hm.pin = v > 0 ? v : null;
    redrawHeatmap();
    if (v > 0) scheduleFetch(); else setRewindLabel('Live');
  }

  function live() {
    const sl = el('hm-rewind');
    if (sl) sl.value = 0;
    onRewind(0);
  }

  async function putSettings(body, what) {
    const { r, j } = await apiPut('/api/v1/heatmap', body);
    if (!r.ok) { toast(errText(j, r), 3500); sync(); return false; }
    if (j) {
      setSize(j.size);
      interval = Number(j.measure_interval_ms) || 0;
      updateSpan();
    }
    if (what === 'size') { clearRows(); toast('Buffer resized (buffer cleared)'); }
    return true;
  }

  async function clearBuffer() {
    const { r, j } = await apiPost('/api/v1/heatmap/clear');
    if (!r.ok) { toast(errText(j, r), 3500); return; }
    clearRows();
    toast('Heatmap buffer cleared');
  }

  function onAutosave(info) {
    if (!info) return;
    const c = el('hm-autosave'), p = el('hm-autosave-path');
    if (c) c.checked = !!info.active;
    if (p) {
      p.textContent = info.active && info.path ? `server: ${info.path}` : '';
      p.title = info.active ? `File on the server: ${info.path || ''}${info.warning ? ' — ' + info.warning : ''}`
                            : 'File on the server';
    }
  }

  async function setAutosave(on) {
    const { r, j } = await apiPost('/api/v1/heatmap/autosave', { active: !!on });
    if (!r.ok) { toast(errText(j, r), 3500); sync(); return; }
    onAutosave(j);
    toast(on ? 'Autosave started (file on the server)' : 'Autosave stopped');
  }

  async function exportCsv(scope) {
    scope = scope || el('hm-export-scope').value;
    let q = `scope=${encodeURIComponent(scope)}`;
    if (scope === 'range') {
      const a = el('hm-export-start').value, b = el('hm-export-end').value;
      if (a !== '') q += `&start=${encodeURIComponent(a)}`;
      if (b !== '') q += `&end=${encodeURIComponent(b)}`;
    } else if (scope === 'frame') {
      q += `&index=${(State.hm && State.hm.pin) || 0}`;
    }
    await apiDownload(`/api/v1/heatmap/export?${q}`, 'heatmap.csv');
  }

  // Rebuild the local view from the server buffer after a page load.
  async function prefill(filled) {
    if (!State.hm || State.hm.rows.length || !(filled > 0)) return;
    const { r, j } = await apiJson('/api/heatmap');
    if (!r.ok || !j || !Array.isArray(j.frames) || !Array.isArray(j.wavelengths)) return;
    if (State.hm.rows.length) return;                   // live frames arrived meanwhile
    const xs = Float64Array.from(j.wavelengths);
    const n = Math.min(filled, j.frames.length, hmRows());
    for (let i = 0; i < n; i++) {
      const ys = j.frames[i].map((v) => (v == null || !isFinite(v) ? null : v));
      const norm = HmLimits.norm(ys);
      State.hm.rows.push({ norm, raw: ys, xs, t: 0 });
    }
    State.hm.xMin = isFinite(State.cfg.x_min_nm) ? State.cfg.x_min_nm : xs[0];
    State.hm.xMax = isFinite(State.cfg.x_max_nm) ? State.cfg.x_max_nm : xs[xs.length - 1];
    onRows();
    redrawHeatmap();
  }

  async function sync() {
    const { r, j } = await apiJson('/api/v1/heatmap');
    if (!r.ok || !j) return;
    setSize(j.size);
    interval = Number(j.measure_interval_ms) || 0;
    const i = el('hm-interval'); if (i && document.activeElement !== i) i.value = interval;
    updateSpan();
    onAutosave(j.autosave);
    if (!State.hm) { try { initHeatmap(); } catch (e) { return; } }
    prefill(j.filled);
  }

  function init() {
    el('hm-buffer').addEventListener('change', () => {
      let n = Math.round(parseFloat(el('hm-buffer').value) / 10) * 10;
      if (!isFinite(n) || n < 10) n = 10;
      el('hm-buffer').value = n;
      putSettings({ buffer_size: n }, 'size');
    });
    el('hm-interval').addEventListener('change', () => {
      let n = Math.max(0, Math.round(parseFloat(el('hm-interval').value) || 0));
      el('hm-interval').value = n;
      putSettings({ measure_interval_ms: n }, 'interval');
    });
    el('hm-clear').addEventListener('click', clearBuffer);
    el('hm-autosave').addEventListener('change', (e) => setAutosave(e.target.checked));
    el('hm-export-scope').addEventListener('change', () => {
      el('hm-export-range').hidden = el('hm-export-scope').value !== 'range';
    });
    el('hm-export').addEventListener('click', () => exportCsv());
    el('hm-rewind').addEventListener('input', (e) => onRewind(parseInt(e.target.value, 10) || 0));
    el('hm-live').addEventListener('click', live);
  }

  return { init, sync, setSize, onConfig, onRows, onAutosave, exportCsv };
})();

// ── Dark capture (P-09) ──────────────────────────────────────
const Dark = (() => {
  const setStatus = (t) => { const e = $('#dark-status'); if (e) e.textContent = t; };
  async function start() {
    const { r, j } = await apiPost('/api/v1/dark', {});
    if (!r.ok) { toast(errText(j, r), 3500); return; }
    setStatus('Capturing dark spectrum…');
    Jobs.track(j.job.id, (job, fin) => {
      if (!fin) {
        const pct = Math.round(100 * (Number(job.progress) || 0));
        setStatus(job.status === 'waiting_for_user' ? 'Waiting for you (see dialog)…'
                  : `Capturing dark spectrum… ${pct}%`);
        return;
      }
      if (job.status === 'done') {
        const res = job.result || {};
        const ms = res.integration_us != null ? (res.integration_us / 1000).toFixed(2) : '?';
        const mean = (res.mean_counts != null && isFinite(res.mean_counts))
          ? `, mean ${Number(res.mean_counts).toFixed(1)} counts` : '';
        setStatus(`Dark stored: ${ms} ms, ${res.n_frames != null ? res.n_frames : '?'} frames${mean}.`);
        toast('Dark spectrum stored');
        CalMgr.loadDarks();
        Cal.refresh();
      } else if (job.status === 'cancelled') {
        setStatus('Dark capture cancelled.');
      } else {
        const m = (job.error && job.error.message) || 'failed';
        setStatus(`Dark capture failed: ${m}`);
        toast(`Dark capture failed: ${m}`, 3500);
      }
    });
  }
  function init() { $('#btn-dark').addEventListener('click', start); }
  return { init };
})();

// ── Calibration manager: profile / reference / dark tables ───
const CalMgr = (() => {
  let profiles = null;
  let refs = [];
  const isOpen = () => !$('#calmgr-dialog').hidden;

  const fmtNm = (a) => (a == null || !isFinite(a)) ? '—' : Number(a).toFixed(0);
  function rangeText(vr, segs) {
    if (Array.isArray(segs) && segs.length > 1) {
      return segs.map((g) => `${fmtNm(g[0])}–${fmtNm(g[1])}`).join(', ') + ' nm';
    }
    if (Array.isArray(vr) && vr.length >= 2) return `${fmtNm(vr[0])}–${fmtNm(vr[1])} nm`;
    return '—';
  }
  const fmtDate = (s) => {
    if (!s) return '—';
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleString();
  };
  const fill = (id, head, rows, empty) => {
    const t = document.getElementById(id);
    t.tHead.innerHTML = `<tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>`;
    t.tBodies[0].innerHTML = rows.length ? rows.join('')
      : `<tr><td class="empty" colspan="${head.length}">${esc(empty)}</td></tr>`;
  };

  function renderProfiles() {
    const items = (profiles && profiles.items) || [];
    const sel = profiles && profiles.selection;
    const note = $('#cal-table-note');
    if (note) note.textContent = profiles && profiles.device
      ? `${profiles.device}${profiles.serial ? ' · ' + profiles.serial : ''}` : 'no device';
    fill('cal-table',
      ['', 'Profile', 'State', 'Type', 'Quantity', 'Unit', 'Valid range', 'Created', 'Plausible'],
      items.map((it) => {
        const warn = it.state === 'wl_changed' || it.state === 'nl_changed' || it.state === 'error';
        const lr = it.legacy_valid_range_nm;
        const state = (it.state || '') + (it.legacy && lr && lr.length === 2
          ? ` · legacy, masked outside ${Math.round(lr[0])}–${Math.round(lr[1])} nm` : '');
        const pl = it.type == null ? '—' : (it.plausible === false ? '<span class="warn">⚠ no</span>' : 'yes');
        return `<tr class="${it.value === sel ? 'active' : ''}">` +
          `<td>${it.value === sel ? '●' : ''}</td>` +
          `<td title="${esc(it.message || '')}">${esc(it.label)}</td>` +
          `<td class="${warn || it.legacy ? 'warn' : ''}">${esc(state)}</td>` +
          `<td>${esc(it.type || '—')}</td>` +
          `<td>${esc(it.type ? (it.quantity || '—') : '—')}</td><td>${esc(it.unit || '—')}</td>` +
          `<td>${esc(rangeText(it.valid_range_nm, it.valid_segments_nm))}</td>` +
          `<td>${esc(fmtDate(it.created))}</td><td>${pl}</td></tr>`;
      }), 'No profiles (connect a device).');
  }

  function setProfiles(j) {
    profiles = j;
    if (isOpen()) renderProfiles();
    CalRun.setProfiles(j);
  }

  function renderRefs() {
    fill('ref-table', ['Name', 'Serial', 'Quantity', 'Unit', 'Range', 'Input', 'Built-in', ''],
      refs.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.serial || '—')}</td>` +
        `<td>${esc(s.quantity || 'irradiance')}</td>` +
        `<td>${esc(s.unit_label || s.unit || '—')}</td>` +
        `<td>${fmtNm(s.valid_min_nm)}–${fmtNm(s.valid_max_nm)} nm</td>` +
        `<td>${esc(s.input_optic || '—')}</td><td>${s.builtin ? 'yes' : 'no'}</td>` +
        `<td>${s.builtin ? '' : `<button class="row-btn" data-ref-del="${esc(s.id)}" ` +
          `data-name="${esc(s.name)}" data-lock="control">Remove</button>`}</td></tr>`),
      'No reference sources.');
    Access.applyLocks();
  }

  async function loadRefs() {
    const { r, j } = await apiJson('/api/v1/calibration/references');
    refs = (r.ok && Array.isArray(j)) ? j : [];
    if (isOpen()) renderRefs();
    CalRun.setRefs(refs);
  }

  async function loadDarks() {
    if (!isOpen()) return;
    const { r, j } = await apiJson('/api/v1/calibration/darks');
    const rows = (r.ok && Array.isArray(j)) ? j : [];
    fill('dark-table', ['Integration time', 'Frames', 'Mean', 'Date'],
      rows.map((d) => `<tr><td class="num">${d.integration_us != null ? (d.integration_us / 1000).toFixed(2) + ' ms' : '—'}</td>` +
        `<td class="num">${d.n_frames != null ? d.n_frames : '—'}</td>` +
        `<td class="num">${(d.mean_counts != null && isFinite(d.mean_counts)) ? Number(d.mean_counts).toFixed(1) + ' counts' : '—'}</td>` +
        `<td>${esc(fmtDate(d.timestamp))}</td></tr>`), 'No stored dark spectra for this device.');
  }

  async function removeRef(id, name) {
    if (!window.confirm(`Remove reference source "${name}"? This cannot be undone.`)) return;
    const { r, j } = await apiDelete(`/api/v1/calibration/references/${encodeURIComponent(id)}`);
    if (!r.ok) { toast(errText(j, r), 3500); return; }
    toast('Reference source removed');
    loadRefs();
  }

  function readText(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result));
      fr.onerror = () => reject(new Error('File read failed'));
      fr.readAsText(file);
    });
  }

  async function importRef() {
    const f = $('#ref-file').files && $('#ref-file').files[0];
    if (!f) { toast('Choose a lamp file (.lmp / .csv) first'); return; }
    let content;
    try { content = await readText(f); } catch (e) { toast(e.message); return; }
    const num = (id) => { const v = $(id).value; return v === '' ? null : Number(v); };
    const name = ($('#ref-name').value || '').trim() || f.name.replace(/\.[^.]+$/, '');
    const body = {
      name, serial: $('#ref-serial').value.trim(), unit: $('#ref-unit').value || 'uW/cm2/nm',
      valid_min_nm: num('#ref-min'), valid_max_nm: num('#ref-max'),
      input_optic: $('#ref-input').value, notes: $('#ref-notes').value.trim(),
      filename: f.name, content,
    };
    const { r, j } = await apiPost('/api/v1/calibration/references', body);
    if (!r.ok) { toast(errText(j, r), 4000); return; }
    toast(`Imported: ${j.name}`);
    $('#ref-import-form').hidden = true;
    $('#ref-file').value = '';
    loadRefs();
  }

  function open() {
    showModal('calmgr-dialog', true);
    renderProfiles(); renderRefs();
    Cal.refresh();
    loadRefs();
    loadDarks();
  }
  const close = () => showModal('calmgr-dialog', false);

  function init() {
    $('#cal-manage').addEventListener('click', open);
    $('#calmgr-close').addEventListener('click', close);
    $('#calmgr-x').addEventListener('click', close);
    $('#ref-import-toggle').addEventListener('click', () => {
      const f = $('#ref-import-form'); f.hidden = !f.hidden;
    });
    $('#ref-import-cancel').addEventListener('click', () => { $('#ref-import-form').hidden = true; });
    $('#ref-import-go').addEventListener('click', importRef);
    $('#ref-table').addEventListener('click', (e) => {
      const b = e.target.closest('[data-ref-del]');
      if (b) removeRef(b.dataset.refDel, b.dataset.name);
    });
    loadRefs();
  }

  return { init, setProfiles, loadRefs, loadDarks, close };
})();

// ── Calibration run / derivation dialog (P-10) ───────────────
const CalRun = (() => {
  let mode = 'lamp';            // 'lamp' | 'derive' | 'sphere'
  let refs = [], profiles = null;
  let adm = null;               // /calibration/admissibility (one rule set, server side)
  let jobId = null, running = false, order = [], lastJob = null;

  const PLAN = {
    lamp: ['wavelength_check', 'integration', 'dark', 'calibration_capture',
           'crosscheck', 'compute', 'save', 'protocol'],
    derive: ['mount_known', 'integration_known', 'dark_known', 'light_known', 'swap_optics',
             'integration_new', 'dark_new', 'light_new', 'dark_new_after', 'swap_back',
             'repeat_known', 'dark_known_after', 'compute', 'save'],
    sphere: ['mount', 'integration', 'dark_before', 'position', 'repeat', 'dark_after',
             'compute', 'save', 'protocol'],
  };
  const LABELS = {
    wavelength_check: 'Wavelength check', integration: 'Integration time',
    dark: 'Dark spectra', calibration_capture: 'Calibration spectrum + stability',
    crosscheck: 'Integration-time cross check', compute: 'Compute', save: 'Save profile',
    protocol: 'Protocol (PDF + CSV)', known: 'Known optics', new: 'New optics',
    dark_known: 'Dark (known optics)', light_known: 'Light (known optics)',
    dark_new: 'Dark (new optics)', light_new: 'Light (new optics)',
    mount_known: 'Mount the known optics on the source',
    integration_known: 'Integration time (known optics)',
    integration_new: 'Integration time (new optics)',
    swap_optics: 'Mount the new optics on the same source',
    dark_new_after: 'Dark after (new optics)', swap_back: 'Mount the known optics again',
    repeat_known: 'Repeat with the known optics (source drift)',
    dark_known_after: 'Dark after (known optics)',
    mount: 'Mount the CC on the sphere port', dark_before: 'Dark before',
    position: 'Irradiance at each distance', repeat: 'Repeat of the first distance (source drift)',
    dark_after: 'Dark after',
  };
  const label = (s) => LABELS[s] || String(s).replace(/_/g, ' ');

  function setRefs(r) { refs = r || []; fillRefs(); }
  function setProfiles(p) { profiles = p; fillKnown(); }

  function fillRefs() {
    const sel = $('#cr-ref'); if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = '';
    for (const s of refs) {
      const rng = (s.valid_min_nm != null && s.valid_max_nm != null)
        ? ` · ${Number(s.valid_min_nm).toFixed(0)}–${Number(s.valid_max_nm).toFixed(0)} nm` : '';
      const q = s.quantity === 'radiance' ? ' · radiance' : '';
      sel.appendChild(new Option(`${s.name}${s.serial ? ' · ' + s.serial : ''}${q}${rng}`, s.id));
    }
    if (keep) sel.value = keep;
    updateAdm();
  }

  async function loadAdm() {
    const { r, j } = await apiJson('/api/v1/calibration/admissibility');
    if (r.ok && j && Array.isArray(j.matrix)) adm = j;
    updateAdm();
  }
  function admissible(kind, quantity, input) {
    const row = ((adm && adm.matrix) || []).find(
      (x) => x.type === kind && x.quantity === quantity && x.input === input);
    return row ? { allowed: !!row.allowed, reason: row.reason || '' } : { allowed: true, reason: '' };
  }
  // Lamp run: disable the calibration types that are not allowed for the chosen
  // reference quantity and input optic (with the reason), show the radiance hint.
  function updateAdm() {
    const kind = $('#cr-kind'), note = $('#cr-adm-note'), hint = $('#cr-radiance-hint');
    if (!kind || !note || !hint) return;
    const ref = refs.find((x) => x.id === ($('#cr-ref') && $('#cr-ref').value));
    const q = (ref && ref.quantity) || 'irradiance';
    const input = $('#cr-input').value;
    const reasons = [];
    for (const o of kind.options) {
      const v = admissible(o.value, q, input);
      o.disabled = !v.allowed;
      o.title = v.allowed ? '' : v.reason;
      if (!v.allowed && v.reason) reasons.push(v.reason);
    }
    if (kind.selectedOptions[0] && kind.selectedOptions[0].disabled) {
      const ok = [...kind.options].find((o) => !o.disabled);
      if (ok) kind.value = ok.value;
    }
    note.textContent = reasons.join(' ');
    note.hidden = mode !== 'lamp' || !reasons.length;
    hint.textContent = (adm && adm.radiance_source_hint) || '';
    hint.hidden = mode !== 'lamp' || q !== 'radiance' || !hint.textContent;
    $('#cr-sphere-hint').textContent = (adm && adm.sphere_source_hint) || '';
    $('#cr-sphere-hint').hidden = mode !== 'sphere' || !$('#cr-sphere-hint').textContent;
  }
  function fillKnown() {
    const sel = $('#cr-known'); if (!sel) return;
    const keep = sel.value;
    sel.innerHTML = '';
    for (const it of ((profiles && profiles.items) || [])) {
      if (it.state === 'builtin' || !it.type) continue;
      sel.appendChild(new Option(`${it.label}${it.plausible === false ? ' ⚠' : ''}`, it.value));
    }
    if (keep) sel.value = keep;
  }

  function setMode(m) {
    mode = m;
    $('#cr-mode-lamp').classList.toggle('on', m === 'lamp');
    $('#cr-mode-derive').classList.toggle('on', m === 'derive');
    $('#cr-mode-sphere').classList.toggle('on', m === 'sphere');
    document.querySelectorAll('#calrun-dialog .lamp-only').forEach((e) => { e.hidden = m !== 'lamp'; });
    document.querySelectorAll('#calrun-dialog .derive-only').forEach((e) => { e.hidden = m !== 'derive'; });
    document.querySelectorAll('#calrun-dialog .sphere-only').forEach((e) => { e.hidden = m !== 'sphere'; });
    document.querySelectorAll('#calrun-dialog .nonsphere-only').forEach((e) => { e.hidden = m === 'sphere'; });
    $('#cr-save-failed-text').textContent = m === 'lamp' ? 'Save although checks failed'
      : (m === 'derive' ? 'Save although the drift check failed'
        : 'Save although the drift or consistency check failed');
    $('#calrun-title').textContent = m === 'lamp' ? 'Calibration run'
      : (m === 'derive' ? 'Derive new optics' : 'Characterise sphere (radiance reference)');
    updateAdm();
  }

  function updateWarn() {
    const on = ($('#cr-allow-implausible').checked && mode !== 'sphere') || $('#cr-save-failed').checked;
    $('#cr-warn').hidden = !on;
  }

  function showForm() {
    $('#cr-form').hidden = false;
    $('#cr-run').hidden = true;
    $('#cr-result').hidden = true;
    $('#cr-start').hidden = false; $('#cr-start').textContent = 'Start';
    $('#cr-cancel').hidden = true; $('#cr-close').hidden = false; $('#cr-x').hidden = false;
    jobId = null; running = false; lastJob = null;
  }
  function showRun() {
    $('#cr-form').hidden = true;
    $('#cr-run').hidden = false;
    $('#cr-result').hidden = true;
    $('#cr-start').hidden = true;
    $('#cr-cancel').hidden = false; $('#cr-close').hidden = true; $('#cr-x').hidden = true;
    $('#cr-bar').style.width = '0%';
    $('#cr-log').textContent = '';
    order = PLAN[mode].slice();
    renderSteps(null);
  }

  async function open(m) {
    if (running) { showModal('calrun-dialog', true); return; }
    setMode(m);
    showForm();
    showModal('calrun-dialog', true);
    await Promise.all([CalMgr.loadRefs(), Cal.refresh(), loadAdm()]);
    fillRefs(); fillKnown();
  }
  function close() { if (!running) showModal('calrun-dialog', false); }

  async function startSphere() {
    const num = (id) => { const v = $(id).value; return v === '' ? null : Number(v); };
    const orv = (v, d) => (v == null || !isFinite(v)) ? d : v;
    const name = $('#cr-sp-name').value.trim();
    if (!name) { toast('Enter a sphere name'); $('#cr-sp-name').focus(); return; }
    const radius = num('#cr-sp-radius');
    if (!(radius > 0)) { toast('Enter the port radius in mm (measured at the part)'); $('#cr-sp-radius').focus(); return; }
    const heights = $('#cr-sp-heights').value.split(/[;,\s]+/).filter(Boolean).map(Number);
    if (heights.length < 3 || heights.some((h) => !isFinite(h))) {
      toast('Enter at least 3 heights in mm, separated by commas'); $('#cr-sp-heights').focus(); return;
    }
    if (new Set(heights).size !== heights.length) { toast('The heights must all differ'); return; }
    const sphere = {
      name, serial: $('#cr-sp-serial').value.trim(), port_radius_mm: radius,
      edge_depth_mm: orv(num('#cr-sp-edge'), 11.0), cc_ref_offset_mm: orv(num('#cr-sp-offset'), 0),
      u_radius_mm: orv(num('#cr-sp-ur'), 0), u_distance_mm: orv(num('#cr-sp-ud'), 0),
    };
    const ucc = num('#cr-sp-ucc');
    if (ucc != null && isFinite(ucc)) sphere.cc_uncertainty_pct = ucc;
    const body = { sphere, heights_mm: heights, save_on_failed_checks: $('#cr-save-failed').checked,
                   integration_ms: 'auto' };
    if ($('#cr-sp-int-mode').value === 'fixed') {
      const v = parseFloat($('#cr-sp-int-ms').value);
      if (!(v > 0)) { toast('Enter a valid integration time'); return; }
      body.integration_ms = v;
    }
    const frames = parseInt($('#cr-frames').value, 10);
    if (isFinite(frames)) body.frames = frames;
    if (body.save_on_failed_checks && !window.confirm('Warning: a result that fails the drift or ' +
        'consistency check will be saved anyway as a reference source.\n\nStart with this option?')) return;
    $('#cr-start').disabled = true;
    const { r, j } = await apiPost('/api/v1/calibration/spheres', body);
    $('#cr-start').disabled = false;
    if (!r.ok) { toast(errText(j, r), 5000); return; }
    jobId = j.job.id; running = true;
    showRun();
    Jobs.track(jobId, onJob);
  }

  async function start() {
    if (mode === 'sphere') { await startSphere(); return; }
    const name = $('#cr-name').value.trim();
    if (!name) { toast('Enter an optics name'); $('#cr-name').focus(); return; }
    const optics = { name, input: $('#cr-input').value, notes: $('#cr-notes').value.trim() };
    const frames = parseInt($('#cr-frames').value, 10);
    const body = {
      optics,
      allow_implausible: $('#cr-allow-implausible').checked,
      save_on_failed_checks: $('#cr-save-failed').checked,
    };
    // integration time: one value (lamp run) or one per optics (derivation)
    const intField = (modeId, msId, what) => {
      if ($(modeId).value !== 'fixed') return 'auto';
      const v = parseFloat($(msId).value);
      if (!(v > 0)) { toast(`Enter a valid integration time${what}`); return null; }
      return v;
    };
    if (isFinite(frames)) body.frames = frames;
    let url;
    if (mode === 'lamp') {
      body.kind = $('#cr-kind').value;
      body.reference_id = $('#cr-ref').value;
      body.integration_ms = intField('#cr-int-mode', '#cr-int-ms', '');
      if (body.integration_ms === null) return;
      if (!body.reference_id) { toast('Choose a reference source'); return; }
      url = '/api/v1/calibration/runs';
    } else {
      body.known_profile_id = $('#cr-known').value;
      if (!body.known_profile_id) { toast('Choose the known profile'); return; }
      body.integration_known_ms = intField('#cr-int-known-mode', '#cr-int-known-ms', ' (known optics)');
      if (body.integration_known_ms === null) return;
      body.integration_new_ms = intField('#cr-int-new-mode', '#cr-int-new-ms', ' (new optics)');
      if (body.integration_new_ms === null) return;
      url = '/api/v1/calibration/derivations';
    }
    if (body.allow_implausible || body.save_on_failed_checks) {
      if (!window.confirm('Warning: a result that fails the plausibility / stability check will be ' +
          'saved anyway (flagged as implausible, not activated).\n\nStart with this option?')) return;
    }
    $('#cr-start').disabled = true;
    const { r, j } = await apiPost(url, body);
    $('#cr-start').disabled = false;
    if (!r.ok) { toast(errText(j, r), 5000); return; }
    jobId = j.job.id; running = true;
    showRun();
    Jobs.track(jobId, onJob);
  }

  function renderSteps(job) {
    const host = $('#cr-steps');
    const cur = job && job.step ? job.step : null;
    if (cur && order.indexOf(cur) < 0) order.push(cur);
    const ci = cur ? order.indexOf(cur) : -1;
    const fin = job && ['done'].includes(job.status);
    const waiting = job && job.status === 'waiting_for_user';
    const failed = job && (job.status === 'failed' || job.status === 'cancelled');
    host.innerHTML = order.map((s, i) => {
      let cls = '', mark = '○';
      if (fin || i < ci) { cls = 'done'; mark = '✓'; }
      else if (i === ci) {
        if (failed) { cls = ''; mark = '✗'; }
        else if (waiting) { cls = 'waiting'; mark = '…'; }
        else { cls = 'active'; mark = '▶'; }
      }
      return `<div class="cr-step ${cls}"><span class="mark">${mark}</span><span>${esc(label(s))}</span></div>`;
    }).join('');
  }

  function renderLog(job) {
    const lines = (job.log || []).map((e) => `${(e.t || '').slice(11, 19)}  ${e.message}`);
    const pre = $('#cr-log');
    const txt = lines.join('\n');
    if (pre.textContent !== txt) { pre.textContent = txt; pre.scrollTop = pre.scrollHeight; }
  }

  const STATUS_TEXT = {
    saved: ['ok', 'Saved'],
    saved_implausible: ['warn', 'Saved, but flagged as implausible (not activated)'],
    not_saved_implausible: ['bad', 'Not saved: the plausibility check failed'],
    not_saved_checks_failed: ['bad', 'Not saved: the stability / integration-time checks failed'],
    drift_check_failed: ['bad', 'Drift check failed'],
    failed: ['bad', 'Failed'],
  };

  const SPHERE_STATUS_TEXT = {
    saved: ['ok', 'Saved as radiance reference'],
    not_saved_checks_failed: ['bad', 'Not saved: the drift or consistency check failed'],
  };

  // ── result detail renderers (checks, dark drift, verification) ──
  const fnum = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d);
  function checksHtml(res) {
    const ck = res.checks;
    if (!ck || typeof ck !== 'object') return [];
    const causes = res.causes || {};
    const row = (name, pct, pass, nfail, cause, fallbackCause) => {
      const cls = pass === false ? 'bad' : '';
      const c = cause || fallbackCause;
      const msg = c ? (typeof c === 'string' ? c : (c.message || c.dominant || '')) : '';
      return `<tr class="${cls}"><td>${esc(name)}</td><td class="num">${fnum(pct, 2)} %</td>` +
        `<td>${pass === true ? 'pass' : (pass === false ? 'FAIL' : 'n/a')}</td>` +
        `<td class="num">${nfail == null ? '—' : esc(nfail)}</td><td class="msg">${esc(msg) || '—'}</td></tr>`;
    };
    return ['<div class="cr-sub">Checks</div><div class="tbl-wrap"><table class="mini-table">' +
      '<thead><tr><th>Check</th><th>Deviation</th><th>Result</th><th>Masked pixels</th><th>Cause</th></tr></thead><tbody>' +
      row('Stability', ck.stability_pct, ck.stability_pass, ck.stability_fail_pixels,
          ck.stability_cause, causes.stability) +
      row('Integration-time cross check', ck.itime_crosscheck_pct, ck.itime_pass,
          ck.itime_fail_pixels, ck.itime_cause, causes.itime) +
      '</tbody></table></div>'];
  }
  // derivation: both sides (integration time, drive level), source drift, consistency
  function derivationHtml(res) {
    const out = [];
    const ms = (us) => (us == null || !isFinite(us)) ? '—' : `${fnum(Number(us) / 1000, 3)} ms`;
    const pc = (f) => (f == null || !isFinite(f)) ? '—' : `${fnum(100 * Number(f), 1)} % FS`;
    if (res.integration_known_us != null || res.integration_new_us != null) {
      out.push('<div class="cr-sub">Integration time and drive level</div><div class="tbl-wrap">' +
        '<table class="mini-table"><thead><tr><th>Optics</th><th>t_int</th><th>Peak</th></tr></thead><tbody>' +
        `<tr><td>Known</td><td class="num">${ms(res.integration_known_us)}</td><td class="num">${pc(res.peak_frac_known)}</td></tr>` +
        `<tr><td>New</td><td class="num">${ms(res.integration_new_us)}</td><td class="num">${pc(res.peak_frac_new)}</td></tr>` +
        `<tr><td>Known, repeat</td><td class="num">${ms(res.integration_known_us)}</td><td class="num">${pc(res.peak_frac_repeat)}</td></tr>` +
        '</tbody></table></div>');
    }
    const d = res.drift;
    if (d && typeof d === 'object') {
      const pass = d.pass === true ? 'pass' : (d.pass === false ? 'FAIL' : 'n/a');
      out.push('<div class="cr-sub">Source drift (known optics, first capture vs. repeat)</div><div class="tbl-wrap">' +
        '<table class="mini-table"><thead><tr><th>p95</th><th>Mean</th><th>Limit</th><th>Result</th>' +
        '<th>Masked pixels</th><th>Cause</th></tr></thead><tbody>' +
        `<tr class="${d.pass === false ? 'bad' : ''}"><td class="num">${fnum(d.p95_pct, 2)} %</td>` +
        `<td class="num">${fnum(d.mean_pct, 2)} %</td><td class="num">${fnum(d.threshold_pct, 2)} %</td>` +
        `<td>${pass}</td><td class="num">${d.n_fail_pixels == null ? '—' : esc(d.n_fail_pixels)}</td>` +
        `<td class="msg">${esc(d.cause || '') || '—'}</td></tr></tbody></table></div>`);
    } else if (res.checks) {
      out.push('<div class="kv">Source drift: not checked.</div>');
    }
    const ck = res.checks || {};
    const bad = [];
    if (ck.itime_match_known === false) bad.push('integration times of the known optics differ between dark and light');
    if (ck.itime_match_new === false) bad.push('integration times of the new optics differ between dark and light');
    if (ck.linearity_consistent === false) bad.push('the linearity correction changed between the captures');
    if (bad.length) out.push('<div><b>Consistency</b></div><ul>' + bad.map((b) => `<li>${esc(b)}</li>`).join('') + '</ul>');
    return out;
  }
  // sphere characterisation: geometry offset, consistency over the distances, drift, uncertainty
  function sphereHtml(res) {
    const out = [];
    const cons = res.consistency || {};
    const ov = cons.overall || {};
    const pf = (p) => p === true ? 'pass' : (p === false ? 'FAIL' : 'n/a');
    if (res.d0_mm != null) {
      out.push('<div class="cr-sub">Result</div><div class="tbl-wrap"><table class="mini-table">' +
        '<thead><tr><th>Offset d0</th><th>Plausible</th><th>Method</th><th>Valid range</th></tr></thead><tbody>' +
        `<tr class="${res.d0_plausible === false ? 'bad' : ''}"><td class="num">${fnum(res.d0_mm, 3)} mm</td>` +
        `<td>${res.d0_plausible ? 'yes' : 'NO'}</td><td>${res.method === 'fit' ? 'fit of d0' : 'mean over the distances'}</td>` +
        `<td class="num">${Array.isArray(res.valid_range_nm) ? fnum(res.valid_range_nm[0], 0) + '–' + fnum(res.valid_range_nm[1], 0) + ' nm' : '—'}</td></tr>` +
        '</tbody></table></div>');
    }
    if (cons.overall) {
      const bands = Array.isArray(cons.bands) ? cons.bands : [];
      out.push('<div class="cr-sub">Consistency of L over the distances · ' + pf(cons.pass) + '</div>' +
        '<div class="tbl-wrap"><table class="mini-table"><thead><tr><th>Band (nm)</th><th>Spread</th>' +
        '<th>d0</th><th>u</th><th></th></tr></thead><tbody>' +
        bands.map((b) => `<tr class="${b.pass === false ? 'bad' : ''}">` +
          `<td class="num">${fnum(b.lo, 0)}–${fnum(b.hi, 0)}</td><td class="num">${fnum(b.spread_pct, 2)} %</td>` +
          `<td class="num">${fnum(b.d0_mm, 3)} mm</td><td class="num">${fnum(b.u_rel_pct, 2)} %</td>` +
          `<td>${b.pass === false ? '⚠' : ''}</td></tr>`).join('') +
        `<tr><td><b>Overall</b></td><td class="num">${fnum(ov.spread_pct, 2)} % (limit ${fnum(cons.max_pct, 2)} %)</td>` +
        `<td></td><td></td><td></td></tr></tbody></table></div>`);
    }
    const d = res.drift;
    if (d && typeof d === 'object') {
      out.push('<div class="cr-sub">Source drift (first distance vs. repeat)</div><div class="tbl-wrap">' +
        '<table class="mini-table"><thead><tr><th>p95</th><th>Mean</th><th>Limit</th><th>Result</th></tr></thead><tbody>' +
        `<tr class="${d.pass === false ? 'bad' : ''}"><td class="num">${fnum(d.p95_pct, 2)} %</td>` +
        `<td class="num">${fnum(d.mean_pct, 2)} %</td><td class="num">${fnum(d.limit_pct, 2)} %</td>` +
        `<td>${pf(d.pass)}</td></tr></tbody></table></div>`);
    }
    const u = res.uncertainty;
    if (u && typeof u === 'object') {
      out.push(`<div class="kv">Uncertainty of L: median u = ${fnum(u.u_rel_median_pct, 2)} % ` +
        `(U, k = 2: ${fnum(u.U_rel_median_pct_k2, 2)} %), maximum u = ${fnum(u.u_rel_max_pct, 2)} %.</div>`);
    }
    if (res.source_id) out.push(`<div class="kv">Reference source: ${esc(res.source_id)} (quantity radiance).</div>`);
    return out;
  }
  function darkDriftHtml(dd) {
    if (!Array.isArray(dd) || !dd.length) return [];
    return ['<div class="cr-sub">Dark drift (before / after)</div><div class="tbl-wrap"><table class="mini-table">' +
      '<thead><tr><th>t_int</th><th>Mean</th><th>% of peak</th><th>Sigma</th><th></th></tr></thead><tbody>' +
      dd.map((d) => `<tr class="${d.warn ? 'warn' : ''}">` +
        `<td class="num">${d.integration_us != null ? fnum(d.integration_us / 1000, 2) + ' ms' : '—'}</td>` +
        `<td class="num">${fnum(d.mean_counts, 1)} counts</td><td class="num">${fnum(d.pct_of_peak, 2)} %</td>` +
        `<td class="num">${fnum(d.sigma_counts, 2)}</td><td>${d.warn ? '⚠ drift' : 'ok'}</td></tr>`).join('') +
      '</tbody></table></div>'];
  }
  function verificationHtml(v) {
    if (!v) return '';
    const bands = Array.isArray(v.bands) ? v.bands : [];
    const ov = v.overall || {};
    let h = `<div class="cr-sub">Verification against ${esc(v.reference_name || 'reference')}` +
      `${v.profile_label ? ' with ' + esc(v.profile_label) : ''} · ` +
      `${v.pass === true ? 'pass' : (v.pass === false ? 'FAIL' : 'n/a')}</div>`;
    h += '<div class="tbl-wrap"><table class="mini-table"><thead><tr><th>Band (nm)</th><th>Pixels</th>' +
      '<th>Mean dev.</th><th>Max |dev.|</th><th></th></tr></thead><tbody>' +
      (bands.length ? bands.map((b) => `<tr class="${b.warn ? 'warn' : ''}">` +
        `<td class="num">${fnum(b.lo, 0)}–${fnum(b.hi, 0)}</td><td class="num">${esc(b.n)}</td>` +
        `<td class="num">${fnum(b.mean_pct, 2)} %</td><td class="num">${fnum(b.max_abs_pct, 2)} %</td>` +
        `<td>${b.warn ? '⚠' : ''}</td></tr>`).join('')
        : '<tr><td class="empty" colspan="5">No bands.</td></tr>') +
      '</tbody></table></div>';
    h += `<div class="kv">Overall: rms ${fnum(ov.rms_pct, 2)} %, mean |dev.| ${fnum(ov.mean_abs_pct, 2)} %, ` +
      `max |dev.| ${fnum(ov.max_abs_pct, 2)} %` +
      (v.norm_range_nm ? ` · normalised at ${fnum(v.norm_range_nm[0], 0)}–${fnum(v.norm_range_nm[1], 0)} nm` : '') +
      '</div>';
    return h;
  }

  async function verifyAgain() {
    const profileId = State.calSelection;
    const referenceId = ($('#cr-ref') && $('#cr-ref').value) || State.cfg.scope_reference_id || '';
    const st = $('#cr-result .cr-verify-status');
    if (!profileId || profileId === 'None') { toast('Activate a calibration profile first'); return; }
    if (!referenceId) { toast('Choose a reference source'); return; }
    const { r, j } = await apiPost('/api/v1/calibration/verifications',
      { profile_id: profileId, reference_id: referenceId });
    if (!r.ok) { toast(errText(j, r), 4000); return; }
    if (st) st.textContent = 'Verifying…';
    Jobs.track(j.job.id, async (job, fin) => {
      const s2 = $('#cr-result .cr-verify-status');
      if (!fin) { if (s2) s2.textContent = job.status === 'waiting_for_user' ? 'Waiting for you (see dialog)…' : 'Verifying…'; return; }
      if (job.status !== 'done') {
        if (s2) s2.textContent = `Verification ${job.status}: ${(job.error && job.error.message) || ''}`;
        return;
      }
      const { r: r2, j: v } = await apiJson('/api/v1/calibration/verification');
      const host = $('#cr-result .cr-verif');
      if (r2.ok && v && host) host.innerHTML = verificationHtml(v);
      else if (r2.ok && v) $('#cr-result .cr-actions').insertAdjacentHTML('beforebegin', `<div class="cr-verif">${verificationHtml(v)}</div>`);
      if (s2) s2.textContent = 'Verification finished.';
    });
  }

  function renderResult(job) {
    const host = $('#cr-result');
    const res = job.result || {};
    let cls = 'ok', title = 'Finished', extra = [];
    if (job.status === 'cancelled') { cls = 'warn'; title = 'Cancelled'; }
    else if (job.status === 'failed') {
      cls = 'bad'; title = 'Failed';
      extra.push(`<div>${esc((job.error && job.error.message) || 'The job failed.')}</div>`);
    } else {
      const t = (job.kind === 'sphere' && SPHERE_STATUS_TEXT[res.status])
        || STATUS_TEXT[res.status] || ['warn', res.status || 'Finished'];
      cls = t[0]; title = t[1];
      if (job.kind === 'sphere' && res.status === 'saved' && res.saved_checks_failed) {
        cls = 'warn';
        title = 'Saved although the checks failed (' + (res.failed_checks || []).join(', ') + ')';
      }
      if (res.status === 'saved' && res.activated) title += ' and activated';
      if (res.status === 'saved' && !res.activated) title += ' (not activated)';
      if (res.status === 'drift_check_failed') {
        // the drift is the primary cause: shown instead of "not plausible"
        const ds = res.drift_status || {};
        title = ds.message || (res.drift && isFinite(res.drift.p95_pct)
          ? `Drift check failed: ${Number(res.drift.p95_pct).toFixed(2)} % (limit ${Number(res.drift.threshold_pct).toFixed(2)} %)`
          : 'Drift check failed');
        extra.push('<div class="kv">Not saved. Run again with “Save although the drift check failed” to store it anyway.</div>');
      }
      if (res.error) extra.push(`<div>${esc(res.error)}</div>`);
      if (res.status === 'not_saved_implausible') {
        extra.push('<div class="kv">Run again with “Save even if implausible” to store it flagged.</div>');
      }
      if (res.status === 'not_saved_checks_failed') {
        extra.push('<div class="kv">Run again with “Save although ' +
          (job.kind === 'derivation' ? 'the drift check failed'
            : (job.kind === 'sphere' ? 'the drift or consistency check failed' : 'checks failed')) +
          '” to store it anyway.</div>');
      }
    }
    const pl = res.plausibility;
    if (pl && Array.isArray(pl.failures) && pl.failures.length) {
      extra.push('<div><b>Plausibility</b></div><ul>' +
        pl.failures.map((f) => `<li>${esc(f)}</li>`).join('') + '</ul>');
    } else if (pl && pl.pass) {
      extra.push('<div class="kv">Plausibility: passed.</div>');
    }
    if (job.kind === 'derivation') extra.push(...derivationHtml(res));
    else if (job.kind === 'sphere') extra.push(...sphereHtml(res));
    else extra.push(...checksHtml(res));
    extra.push(...darkDriftHtml(res.dark_drift));
    const hints = pl && Array.isArray(pl.hints) ? pl.hints : [];
    if (hints.length) {
      extra.push('<div class="cr-sub">Plausibility hints</div><ul>' +
        hints.map((h) => `<li>${esc(h)}</li>`).join('') + '</ul>');
    }
    if (res.verification) extra.push(`<div class="cr-verif">${verificationHtml(res.verification)}</div>`);
    else if (res.verification_error) {
      extra.push(`<div class="cr-verif"><div class="cr-sub">Verification</div>` +
        `<div class="cr-hint">${esc(res.verification_error)}</div></div>`);
    }
    if (job.kind === 'calibration' && job.status === 'done') {
      extra.push('<div class="cr-actions"><button data-verify-again="1" data-lock="control" ' +
        'title="Measure the reference again with the active profile and compare">Verify again</button>' +
        '<span class="kv cr-verify-status"></span></div>');
    }
    if (Array.isArray(res.valid_segments_nm) && res.valid_segments_nm.length) {
      extra.push('<div class="kv">Valid segments: ' + res.valid_segments_nm.map(
        (g) => `${Number(g[0]).toFixed(0)}–${Number(g[1]).toFixed(0)}`).join(', ') + ' nm</div>');
    }
    const wc = res.wavelength_check;
    if (wc && Array.isArray(wc.messages) && wc.messages.length) {
      extra.push('<div class="kv">' + wc.messages.map(esc).join('<br>') + '</div>');
    }
    if (Array.isArray(res.warnings) && res.warnings.length) {
      extra.push('<div><b>Warnings</b></div><ul>' + res.warnings.map((w) => `<li>${esc(w)}</li>`).join('') + '</ul>');
    }
    const prot = Array.isArray(res.protocol) ? res.protocol.map((p) => String(p).toLowerCase()) : [];
    const links = [];
    if (['calibration', 'derivation', 'sphere'].includes(job.kind) && prot.length) {
      if (prot.some((p) => p.endsWith('.pdf'))) links.push(['pdf', 'Protocol (PDF)']);
      if (prot.some((p) => p.endsWith('.csv'))) links.push(['csv', 'Protocol (CSV)']);
    }
    if (links.length) {
      extra.push('<div style="margin-top:6px">' + links.map(
        ([f, t]) => `<a data-protocol="${f}">⤓ ${t}</a>`).join('') + '</div>');
    }
    if (res.protocol_message) extra.push(`<div class="kv">${esc(res.protocol_message)}</div>`);
    host.className = `cr-result ${cls}`;
    host.innerHTML = `<h4>${esc(title)}</h4>${extra.join('')}`;
    host.hidden = false;
  }

  function onJob(job, fin) {
    lastJob = job;
    $('#cr-bar').style.width = `${Math.round(100 * (Number(job.progress) || 0))}%`;
    renderSteps(job);
    renderLog(job);
    if (!fin) return;
    running = false;
    $('#cr-cancel').hidden = true; $('#cr-close').hidden = false; $('#cr-x').hidden = false;
    $('#cr-start').hidden = false; $('#cr-start').textContent = 'New run';
    $('#cr-bar').style.width = job.status === 'done' ? '100%' : $('#cr-bar').style.width;
    renderResult(job);
    const st = (job.result || {}).status;
    if (st === 'saved' || st === 'saved_implausible') {
      Cal.refresh();
      CalMgr.loadDarks();
      if (job.kind === 'sphere') CalMgr.loadRefs();
    }
  }

  async function cancelRun() {
    if (!jobId) return;
    if (!window.confirm('Cancel the running job?')) return;
    await Jobs.cancel(jobId);
  }

  function init() {
    $('#cal-run-open').addEventListener('click', () => open('lamp'));
    $('#cal-derive-open').addEventListener('click', () => open('derive'));
    $('#cal-sphere-open').addEventListener('click', () => open('sphere'));
    $('#cr-mode-sphere').addEventListener('click', () => { if (!running) { setMode('sphere'); updateWarn(); } });
    $('#cr-sp-int-mode').addEventListener('change', () => {
      $('#cr-sp-int-ms').disabled = $('#cr-sp-int-mode').value !== 'fixed';
    });
    $('#cr-ref').addEventListener('change', updateAdm);
    $('#cr-input').addEventListener('change', updateAdm);
    $('#cr-mode-lamp').addEventListener('click', () => { if (!running) { setMode('lamp'); updateWarn(); } });
    $('#cr-mode-derive').addEventListener('click', () => { if (!running) { setMode('derive'); updateWarn(); } });
    $('#cr-int-mode').addEventListener('change', () => {
      $('#cr-int-ms').disabled = $('#cr-int-mode').value !== 'fixed';
    });
    for (const w of ['known', 'new']) {
      $(`#cr-int-${w}-mode`).addEventListener('change', () => {
        $(`#cr-int-${w}-ms`).disabled = $(`#cr-int-${w}-mode`).value !== 'fixed';
      });
    }
    $('#cr-allow-implausible').addEventListener('change', updateWarn);
    $('#cr-save-failed').addEventListener('change', updateWarn);
    $('#cr-start').addEventListener('click', () => {
      if ($('#cr-form').hidden) showForm(); else start();
    });
    $('#cr-cancel').addEventListener('click', cancelRun);
    $('#cr-close').addEventListener('click', close);
    $('#cr-x').addEventListener('click', close);
    $('#cr-result').addEventListener('click', (e) => {
      if (e.target.closest('[data-verify-again]')) { verifyAgain(); return; }
      const a = e.target.closest('[data-protocol]');
      if (a && jobId) {
        const kind = (lastJob && lastJob.kind) || 'calibration';
        const base = kind === 'sphere' ? 'spheres' : 'runs';
        apiDownload(`/api/v1/calibration/${base}/${encodeURIComponent(jobId)}/protocol?format=${a.dataset.protocol}`,
                    `${kind === 'derivation' ? 'derivation' : (kind === 'sphere' ? 'sphere' : 'calibration')}_protocol.${a.dataset.protocol}`);
      }
    });
  }

  return { init, setRefs, setProfiles, verificationHtml, isRunning: () => running, close };
})();

// ── Linearity panel (hardware test point 5) ──────────────────
const Lin = (() => {
  const L = '/api/v1/calibration/linearity';
  let data = null, jobId = null, running = false;
  const fnum = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d);
  const fmtDate = (s) => { if (!s) return '—'; const d = new Date(s); return isNaN(d) ? String(s) : d.toLocaleString(); };
  const rangeTxt = (r) => (Array.isArray(r) && r.length >= 2)
    ? `${fnum(r[0], 0)}–${fnum(r[1], 0)} counts` : '—';

  function renderState() {
    const host = $('#lin-state');
    const st = (data && data.state) || { state: 'none' };
    const factory = st.state === 'factory';
    $('#lin-factory-note').hidden = !factory;
    $('#lin-start').textContent = factory ? 'Run linearity check' : 'Run linearity calibration';
    let cls = 'ok', txt;
    switch (st.state) {
      case 'active': txt = `Linearity correction active${st.label ? ': ' + st.label : ''}` +
        ` (valid ${rangeTxt(st.valid_range_counts)}).`; break;
      case 'off': cls = 'warn'; txt = 'Linearity correction is switched off.'; break;
      case 'factory': txt = 'Factory linearisation of the device is in use (no software correction).'; break;
      default: cls = 'warn'; txt = 'No linearity correction available for this device.';
    }
    host.className = `cr-result ${cls}`;
    host.textContent = txt;
    host.hidden = false;
    $('#lin-table-note').textContent = data && data.device
      ? `${data.device}${data.serial ? ' · ' + data.serial : ''}` : 'no device';
    $('#lin-off').disabled = st.state !== 'active';
  }

  function renderTable() {
    const entries = (data && data.entries) || [];
    const act = data && data.active_id;
    const head = ['', 'Created', 'Mode', 'Valid range', 'Max dev.', 'RMS before → after', 'Degree', 'Steps', ''];
    const t = $('#lin-table');
    t.tHead.innerHTML = `<tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>`;
    t.tBodies[0].innerHTML = entries.length ? entries.map((e) =>
      `<tr class="${e.id === act ? 'active' : ''}"><td>${e.id === act ? '●' : ''}</td>` +
      `<td>${esc(fmtDate(e.created))}</td><td>${esc(e.mode || '—')}</td>` +
      `<td>${esc(rangeTxt(e.valid_range_counts))}</td><td class="num">${fnum(e.max_deviation_pct, 2)} %</td>` +
      `<td class="num">${fnum(e.rms_before_pct, 2)} → ${e.mode === 'check' ? '—' : fnum(e.rms_after_pct, 2)} %</td>` +
      `<td class="num">${e.degree == null ? '—' : esc(e.degree)}</td><td class="num">${e.n_steps == null ? '—' : esc(e.n_steps)}</td>` +
      `<td>${e.mode === 'check' || e.id === act ? '' :
        `<button class="row-btn lin-act" data-lin-act="${esc(e.id)}" data-lock="control">Activate</button>`}</td></tr>`
    ).join('') : `<tr><td class="empty" colspan="${head.length}">No linearity records for this device.</td></tr>`;
    Access.applyLocks();
  }

  async function refresh() {
    const { r, j } = await apiJson(L);
    data = r.ok ? j : null;
    renderState(); renderTable();
  }

  async function select(id) {
    const { r, j } = await apiPost(`${L}/select`, { entry_id: id });
    if (!r.ok) { toast(errText(j, r), 3500); return; }
    toast(id ? 'Linearity record activated' : 'Linearity correction switched off');
    refresh();
    Cal.refresh();
  }

  function showIdle() {
    $('#lin-form').hidden = false; $('#lin-run').hidden = true;
    $('#lin-start').hidden = false; $('#lin-cancel').hidden = true;
    $('#lin-close').hidden = false; $('#lin-x').hidden = false;
  }
  function showRunning() {
    $('#lin-form').hidden = true; $('#lin-run').hidden = false; $('#lin-result').hidden = true;
    $('#lin-start').hidden = true; $('#lin-cancel').hidden = false;
    $('#lin-close').hidden = true; $('#lin-x').hidden = true;
    $('#lin-bar').style.width = '0%';
  }

  async function start() {
    const fixed = $('#lin-int-mode').value === 'fixed';
    const ms = parseFloat($('#lin-int-ms').value);
    if (fixed && !(ms > 0)) { toast('Enter a valid integration time'); return; }
    const body = { integration_ms: fixed ? ms : 'auto', save: true, activate: true };
    const steps = parseInt($('#lin-steps').value, 10);
    const frames = parseInt($('#lin-frames').value, 10);
    if (isFinite(steps)) body.steps = Math.max(6, Math.min(10, steps));
    if (isFinite(frames)) body.frames = frames;
    $('#lin-start').disabled = true;
    const { r, j } = await apiPost(`${L}/runs`, body);
    $('#lin-start').disabled = false;
    if (!r.ok) { toast(errText(j, r), 4000); return; }
    jobId = j.job.id; running = true;
    showRunning();
    Jobs.track(jobId, onJob);
  }

  function onJob(job, fin) {
    $('#lin-bar').style.width = `${Math.round(100 * (Number(job.progress) || 0))}%`;
    const st = $('#lin-status');
    if (!fin) {
      st.textContent = job.status === 'waiting_for_user'
        ? 'Waiting for you (see dialog)…'
        : `${String(job.step || 'running').replace(/_/g, ' ')} ${Math.round(100 * (Number(job.progress) || 0))}%`;
      return;
    }
    running = false;
    st.textContent = '';
    showIdle();
    renderResult(job);
    refresh();
    Cal.refresh();
  }

  function renderResult(job) {
    const host = $('#lin-result');
    const res = job.result || {};
    let cls = 'ok', title, extra = [];
    if (job.status === 'cancelled') { cls = 'warn'; title = 'Cancelled'; }
    else if (job.status === 'failed') {
      cls = 'bad'; title = 'Failed';
      extra.push(`<div>${esc((job.error && job.error.message) || 'The job failed.')}</div>`);
    } else {
      const T = { saved: 'Saved and activated', check_saved: 'Check measurement saved (no correction applied)',
                  computed: 'Computed (not saved)', failed: 'Linearity fit failed' };
      title = T[res.status] || res.status || 'Finished';
      if (res.status === 'failed') cls = 'bad';
      else if (res.status === 'computed') cls = 'warn';
      const m = (res.result && res.result.model) || {};
      const lamp = (res.result && res.result.lamp) || {};
      const rows = [
        ['Mode', res.mode], ['Max deviation', m.max_deviation_pct != null ? fnum(m.max_deviation_pct, 2) + ' %' : null],
        ['RMS before', m.rms_before_pct != null ? fnum(m.rms_before_pct, 2) + ' %' : null],
        ['RMS after', res.mode === 'check' || m.rms_after_pct == null ? null : fnum(m.rms_after_pct, 2) + ' %'],
        ['Valid fraction', m.valid_range_frac != null ? fnum(100 * m.valid_range_frac, 0) + ' %' : null],
        ['Degree', m.degree], ['Lamp drift', lamp.drift_pct != null ? fnum(lamp.drift_pct, 2) + ' %' : null],
      ].filter((x) => x[1] != null && x[1] !== '');
      if (rows.length) extra.push('<div class="kv">' + rows.map(
        ([k, v]) => `${esc(k)}: ${esc(v)}`).join('<br>') + '</div>');
      const warns = [].concat(res.warnings || [], (res.result && res.result.warnings) || []);
      if (warns.length) extra.push('<div class="cr-sub">Warnings</div><ul>' +
        [...new Set(warns)].map((w) => `<li>${esc(w)}</li>`).join('') + '</ul>');
      const prot = Array.isArray(res.protocol) ? res.protocol.map((x) => String(x).toLowerCase()) : [];
      const links = [];
      if (prot.some((x) => x.endsWith('.pdf'))) links.push(['pdf', 'Protocol (PDF)']);
      if (prot.some((x) => x.endsWith('.csv'))) links.push(['csv', 'Protocol (CSV)']);
      if (links.length) extra.push('<div style="margin-top:6px">' + links.map(
        ([f, t]) => `<a data-lin-protocol="${f}">⤓ ${t}</a>`).join('') + '</div>');
      if (res.protocol_message) extra.push(`<div class="kv">${esc(res.protocol_message)}</div>`);
    }
    host.className = `cr-result ${cls}`;
    host.innerHTML = `<h4>${esc(title)}</h4>${extra.join('')}`;
    host.hidden = false;
  }

  function open() {
    showModal('lin-dialog', true);
    if (!running) showIdle();
    refresh();
  }
  function close() { if (!running) showModal('lin-dialog', false); }

  function init() {
    $('#lin-open').addEventListener('click', open);
    $('#lin-close').addEventListener('click', close);
    $('#lin-x').addEventListener('click', close);
    $('#lin-start').addEventListener('click', start);
    $('#lin-off').addEventListener('click', () => select(null));
    $('#lin-int-mode').addEventListener('change', () => {
      $('#lin-int-ms').disabled = $('#lin-int-mode').value !== 'fixed';
    });
    $('#lin-cancel').addEventListener('click', async () => {
      if (jobId && window.confirm('Cancel the running job?')) await Jobs.cancel(jobId);
    });
    $('#lin-table').addEventListener('click', (e) => {
      const b = e.target.closest('[data-lin-act]');
      if (b) select(b.dataset.linAct);
    });
    $('#lin-result').addEventListener('click', (e) => {
      const a = e.target.closest('[data-lin-protocol]');
      if (a && jobId) {
        apiDownload(`${L}/runs/${encodeURIComponent(jobId)}/protocol?format=${a.dataset.linProtocol}`,
                    `linearity_protocol.${a.dataset.linProtocol}`);
      }
    });
  }
  return { init, refresh };
})();

// ── Scope: reference overlay (hardware test points 3/4) ──────
const RefOverlay = (() => {
  const MIN_GAP_MS = 1500;
  let data = null, key = '', busy = false, lastAt = 0, timer = null;

  const enabled = () => !!State.cfg.scope_reference_overlay;
  const selEl = () => $('#scope-ref-select');
  const refId = () => State.cfg.scope_reference_id || (selEl() ? selEl().value : '') || '';

  function sync() {
    const b = $('#btn-refoverlay'), sel = selEl();
    if (b) b.classList.toggle('on', enabled());
    if (sel) {
      if (State.cfg.scope_reference_id && sel.value !== State.cfg.scope_reference_id &&
          [...sel.options].some((o) => o.value === State.cfg.scope_reference_id)) {
        sel.value = State.cfg.scope_reference_id;
      }
      sel.disabled = !sel.options.length;
    }
  }

  async function loadRefs() {
    const sel = selEl();
    if (!sel) return;
    const { r, j } = await apiJson('/api/v1/calibration/references');
    const list = (r.ok && Array.isArray(j)) ? j : [];
    sel.innerHTML = '';
    for (const s of list) sel.appendChild(new Option(s.name, s.id));
    sync();
    if (enabled()) request(true);
  }

  // Called per frame and on unit changes; throttled to one request per MIN_GAP_MS.
  function request(force) {
    if (!enabled() || !refId()) return;
    const now = Date.now();
    const k = `${refId()}|${State.displayModeEff}|${State.dispUnit}|${State.dispUnitLabel}`;
    if (!force && k === key && now - lastAt < MIN_GAP_MS * 2) return;
    if (busy || now - lastAt < MIN_GAP_MS) {
      if (!timer) timer = setTimeout(() => { timer = null; request(force); }, MIN_GAP_MS);
      return;
    }
    fetchOverlay(k);
  }

  async function fetchOverlay(k) {
    busy = true; lastAt = Date.now();
    try {
      const { r, j } = await apiJson(
        `/api/v1/calibration/reference-overlay?reference_id=${encodeURIComponent(refId())}`);
      data = (r.ok && j && Array.isArray(j.wavelengths)) ? j : null;
      key = k;
    } catch (e) { data = null; }
    busy = false;
    refreshScopeOverlay();
  }

  // Series on the current live axis; the server already scaled the values.
  function series(live) {
    const n = live ? live.length : 0;
    const xs = State.scopeData[SI.X];
    if (!enabled() || !data || !xs || xs.length !== n || !n) return new Array(n).fill(null);
    if (data.wavelengths.length === n) {
      return data.values.map((v) => (v == null || !isFinite(v)) ? null : v);
    }
    return interpToAxis(data.wavelengths, data.values, xs);
  }

  function setEnabled(on) {
    const partial = { scope_reference_overlay: !!on };
    if (on && selEl() && selEl().value) partial.scope_reference_id = selEl().value;
    pushCfg(partial);
    if (!on) data = null;
    sync();
    if (on) request(true); else refreshScopeOverlay();
  }

  function init() {
    $('#btn-refoverlay')?.addEventListener('click', () => setEnabled(!enabled()));
    selEl()?.addEventListener('change', (e) => {
      pushCfg({ scope_reference_id: e.target.value });
      data = null;
      if (enabled()) request(true);
    });
    loadRefs();
  }

  return { init, sync, request, series };
})();

// ── Display modes: selector, units, exposure bar, raw curve, per-mode limits ──
const DisplayMode = (() => {
  const isCountUnit = (u) => u === 'counts' || u === 'counts/s' || u === 'counts_corrected';
  let modesKey = '', lastTitle = '', rawUser = null;
  try { const v = localStorage.getItem('scope_raw_curve'); if (v === '1' || v === '0') rawUser = v === '1'; }
  catch (e) { /* storage unavailable */ }

  const modeLabel = (v) => {
    const m = State.displayModes.find((x) => x.value === v);
    return m ? m.label : (v || '');
  };
  const unitText = () => {
    if (State.dispUnit === 'counts' || State.dispUnit === 'counts_corrected') return 'ADC';
    if (State.dispUnit === 'counts/s') return 'counts/s';
    if (State.dispUnit === 'relative') return 'rel.';
    return State.dispUnitLabel || State.dispUnit || '';
  };
  // Display value for readouts: "–" for masked / non-finite pixels.
  function fmt(v) {
    if (v == null || typeof v !== 'number' || !isFinite(v)) return '–';
    if (State.dispUnit === 'counts' || State.dispUnit === 'counts_corrected') return v.toFixed(0);
    if (State.dispUnit === 'counts/s') return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2);
    const a = Math.abs(v);
    return a >= 100 ? v.toFixed(1) : (a === 0 ? '0' : v.toPrecision(4));
  }
  const fmtUnit = (v) => `${fmt(v)}<span class="unit"> ${esc(unitText())}</span>`;

  function yTitle() {
    let t;
    if (State.dispUnit === 'counts' || State.dispUnit === 'counts_corrected') t = 'Intensity (ADC)';
    else if (State.dispUnit === 'counts/s') t = 'Count rate (counts/s)';
    else if (State.dispUnit === 'relative') t = 'Relative intensity';
    else t = `Spectral irradiance (${State.dispUnitLabel || State.dispUnit})`;
    const ml = modeLabel(State.displayModeEff);
    return ml ? `${t} · ${ml}` : t;
  }

  function rawVisible() {
    if (State.displayModeEff === 'raw') return false;
    return rawUser == null ? true : rawUser;
  }

  function syncSelect() {
    const sel = $('#scope-display-mode');
    if (!sel) return;
    const want = State.cfg.display_mode || 'auto';
    if ([...sel.options].some((o) => o.value === want)) sel.value = want;
    sel.title = 'Display mode' + (want === 'auto' && State.displayModeEff
      ? ` (auto: ${modeLabel(State.displayModeEff)})` : '');
    const b = $('#btn-raw-curve');
    if (b) {
      b.classList.toggle('on', rawVisible());
      b.disabled = State.displayModeEff === 'raw';
    }
    const u = $('#y-unit-lbl');
    if (u) u.textContent = unitText();
    const hu = $('#hm-ymax-unit');
    if (hu) hu.textContent = unitText();
  }

  function buildSelect(modes) {
    const sel = $('#scope-display-mode');
    if (!sel || !Array.isArray(modes)) return;
    const list = modes.slice().sort((a, b) => (b.value === 'auto') - (a.value === 'auto'));
    const key = JSON.stringify(list);
    if (key === modesKey) return;
    modesKey = key;
    State.displayModes = list;
    sel.innerHTML = '';
    for (const m of list) {
      const o = new Option(m.label || m.value, m.value);
      o.disabled = m.available === false && m.value !== 'auto';
      if (o.disabled) o.title = 'Not available for the current calibration / device';
      sel.appendChild(o);
    }
    syncSelect();
  }

  function syncYControls() {
    const el = $('#cfg-y_max_adc');
    if (el && State.cfg.y_max_adc != null && document.activeElement !== el) el.value = State.cfg.y_max_adc;
    const tog = document.querySelector('.toggle[data-key="auto_y"]');
    if (tog) tog.classList.toggle('on', State.cfg.auto_y !== false);
  }

  // Manual limits are stored per mode; a mode without them autoscales.
  function onModeChange(eff) {
    const first = State.displayModeEff == null;
    State.displayModeEff = eff;
    const lim = (State.cfg.y_limits_by_mode || {})[eff];
    const manual = lim && lim.auto === false && isFinite(Number(lim.max)) && Number(lim.max) > 0;
    if (manual || !first) {
      State._applyingMode = true;
      try { pushCfg(manual ? { auto_y: false, y_max_adc: Number(lim.max) } : { auto_y: true }); }
      finally { State._applyingMode = false; }
      syncYControls();
    }
    HmLimits.onModeChange();
    syncSelect();
  }

  function updateRawSeries() {
    const n = (State.scopeData[SI.LIVE] || []).length;
    const on = rawVisible() && Array.isArray(State.rawData) && State.rawData.length === n && n > 0;
    State.scopeData[SI.RAW] = on ? State.rawData : new Array(n).fill(null);
    const u = State.scopePlot;
    if (u && u.axes[2] && u.axes[2].show !== on) {
      u.axes[2].show = on;
      u.redraw(true, true);
    }
  }

  function updateExposure() {
    const bar = $('#exposure-bar'), txt = $('#exposure-text'), fill = $('#exposure-fill');
    if (!bar) return;
    const ex = State.exposure;
    if (State.displayModeEff === 'raw' || !ex || ex.peak_pct_fs == null || !isFinite(ex.peak_pct_fs)) {
      bar.className = 'exposure-bar off';
      txt.textContent = State.displayModeEff === 'raw' ? 'Raw ADC' : '—';
      fill.style.width = '0%';
      return;
    }
    const pct = Number(ex.peak_pct_fs);
    const sat = !!ex.saturated || Number(ex.n_saturated) > 0;
    // state and threshold come from the server (config key saturation_warn_frac)
    const warnPct = ex.warn_pct_fs != null && isFinite(ex.warn_pct_fs) ? Number(ex.warn_pct_fs) : null;
    let state = ex.state;
    if (state !== 'ok' && state !== 'warn' && state !== 'danger') {
      state = sat ? 'danger' : (warnPct != null && pct >= warnPct ? 'warn' : 'ok');
    }
    bar.className = 'exposure-bar ' + (state === 'danger' ? 'danger' : (state === 'warn' ? 'warn' : ''));
    bar.title = 'Peak of the raw signal relative to the detector full scale' +
      (warnPct != null ? `; warning from ${warnPct.toFixed(0)} % FS` : '');
    let t = `Peak ${pct.toFixed(0)} % FS`;
    if (ex.n_saturated > 0) t += ` · ${ex.n_saturated} px saturated`;
    else if (state === 'danger') t += ' · saturated';
    txt.textContent = t;
    fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  }

  function onFrame(msg) {
    State.dispUnit = msg.display_unit != null ? msg.display_unit : (msg.unit || 'counts');
    State.dispUnitLabel = msg.display_unit_label || msg.unit_label || State.dispUnit;
    State.exposure = msg.exposure || null;
    State.rawData = Array.isArray(msg.raw) ? msg.raw : null;
    buildSelect(msg.display_modes);
    const eff = msg.display_mode || null;
    if (eff !== State.displayModeEff) onModeChange(eff);
    updateRawSeries();
    updateExposure();
    const t = yTitle();
    if (t !== lastTitle) {
      lastTitle = t;
      syncSelect();
      updateYAxisLabel();
    }
  }

  async function select(v) {
    const { r, j } = await apiPut('/api/v1/acquisition', { display_mode: v });
    if (!r.ok) { toast(errText(j, r), 3500); syncSelect(); return; }
    State.cfg.display_mode = v;
    syncSelect();
  }

  function toggleRaw() {
    rawUser = !rawVisible();
    try { localStorage.setItem('scope_raw_curve', rawUser ? '1' : '0'); } catch (e) { /* ignore */ }
    syncSelect();
    updateRawSeries();
    if (State.scopePlot && State.activeTab === 'scope') State.scopePlot.setData(State.scopeData);
  }

  function init() {
    $('#scope-display-mode')?.addEventListener('change', (e) => select(e.target.value));
    $('#btn-raw-curve')?.addEventListener('click', toggleRaw);
  }

  return { init, onFrame, fmt, fmtUnit, unitText, yTitle, syncSelect, isCountUnit };
})();

// Heatmap upper limit (auto per frame, or manual), stored per display mode.
const HmLimits = (() => {
  const lim = () => (State.cfg.heatmap_limits_by_mode || {})[State.displayModeEff] || null;
  const manualMax = () => { const l = lim(); return (l && l.auto === false && Number(l.max) > 0) ? Number(l.max) : null; };

  // Upper colour limit for one row of display values.
  function yMax(ys) {
    const m = manualMax();
    if (m != null) return m;
    let mx = 0;
    for (const v of ys) if (v != null && isFinite(v) && v > mx) mx = v;
    if (mx <= 0) return 1;
    return DisplayMode.isCountUnit(State.dispUnit) ? Math.max(50, mx * 1.1) : mx * 1.1;
  }
  function norm(ys) {
    const top = yMax(ys), n = ys.length, out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const v = ys[i];
      out[i] = (v == null || !isFinite(v)) ? 0 : Math.max(0, Math.min(1, v / top));
    }
    return out;
  }
  function renorm() {
    if (!State.hm) return;
    for (const r of State.hm.rows) r.norm = norm(r.raw);
    if (State.activeTab === 'heatmap') redrawHeatmap();
  }
  function syncUi() {
    const a = $('#hm-ymax-auto'), n = $('#hm-ymax');
    if (!a || !n) return;
    const m = manualMax();
    a.checked = m == null;
    n.disabled = m == null;
    if (m != null && document.activeElement !== n) n.value = m;
    if (m == null && document.activeElement !== n) n.value = '';
  }
  function store(auto, max) {
    const by = Object.assign({}, State.cfg.heatmap_limits_by_mode || {});
    if (State.displayModeEff) by[State.displayModeEff] = { auto, max };
    pushCfg({ heatmap_limits_by_mode: by });
    renorm();
    syncUi();
  }
  function onModeChange() {
    // Buffered rows hold values of the previous mode: restart the local view.
    if (State.hm) { State.hm.rows.length = 0; State.hm.pin = null; State.hm.lastPush = 0; }
    try { Hm.onRows(); } catch (e) { /* not ready */ }
    syncUi();
    if (State.hm && State.activeTab === 'heatmap') redrawHeatmap();
  }
  function init() {
    $('#hm-ymax-auto')?.addEventListener('change', (e) => {
      if (e.target.checked) { store(true, null); return; }
      const top = State.hm && State.hm.rows.length ? yMax(State.hm.rows[0].raw) : 1;
      const n = $('#hm-ymax');
      n.value = Number(top.toPrecision(3));
      store(false, Number(n.value));
    });
    $('#hm-ymax')?.addEventListener('change', (e) => {
      const v = parseFloat(e.target.value);
      if (v > 0) store(false, v); else syncUi();
    });
  }
  return { init, yMax, norm, renorm, syncUi, onModeChange };
})();

// ── Data section: export folder / file-name template (P-07) ──
const DataSec = (() => {
  let timer = null;
  async function preview() {
    const tpl = $('#cfg-export_filename_template');
    if (!tpl) return;
    const { r, j } = await apiJson('/api/v1/export/filename?kind=spectrum&ext=csv&template=' +
      encodeURIComponent(tpl.value || ''));
    const out = $('#export-preview');
    if (!r.ok || !j) { out.textContent = '—'; return; }
    out.textContent = `Example: ${j.filename}` + (j.export_dir ? `  ·  folder on server: ${j.export_dir}` : '') +
      (j.warning ? `  ⚠ ${j.warning}` : '');
    out.className = 'cal-status' + (j.warning ? ' warn' : '');
    const tk = $('#export-tokens');
    if (tk && j.tokens) tk.textContent = j.tokens;
  }
  const later = (ms) => { clearTimeout(timer); timer = setTimeout(preview, ms); };
  function init() {
    $('#cfg-export_filename_template').addEventListener('input', () => later(300));
    $('#cfg-export_dir').addEventListener('change', () => later(500));
    $('#sec-data').addEventListener('toggle', (e) => { if (e.target.open) preview(); });
    preview();
  }
  return { init, preview };
})();

// ── Wavelength calibration dialog (P-11) ─────────────────────
const WlCal = (() => {
  let active = false, saved = false, preview = null, peaks = [];
  const W = '/api/v1/wavelength';
  const setStatus = (t, cls) => { const e = $('#wl-status'); e.textContent = t; e.className = 'cal-status ' + (cls || ''); };

  function drawPlot() {
    const p = prepCanvas($('#wl-plot'));
    if (!p) return;
    const { ctx, W: cw, H: ch } = p;
    ctx.clearRect(0, 0, cw, ch);
    ctx.fillStyle = getCss('--bg-0'); ctx.fillRect(0, 0, cw, ch);
    if (!preview || !preview.wavelengths.length) return;
    const xs = preview.wavelengths, ys = preview.values;
    const x0 = xs[0], x1 = xs[xs.length - 1];
    let mx = 1; for (const v of ys) if (v != null && isFinite(v) && v > mx) mx = v;
    const X = (x) => 6 + (x - x0) / Math.max(1e-9, x1 - x0) * (cw - 12);
    const Y = (v) => ch - 14 - (v / mx) * (ch - 24);
    ctx.strokeStyle = '#2dd4bf'; ctx.lineWidth = 1.2; ctx.beginPath();
    xs.forEach((x, i) => { const v = ys[i] == null ? 0 : ys[i]; i ? ctx.lineTo(X(x), Y(v)) : ctx.moveTo(X(x), Y(v)); });
    ctx.stroke();
    ctx.fillStyle = getCss('--warn'); ctx.font = '10px JetBrains Mono';
    for (const pk of peaks) {
      const px = X(pk.wavelength), py = Y(pk.height);
      ctx.beginPath(); ctx.arc(px, py, 3, 0, 6.3); ctx.fill();
      ctx.fillText(Number(pk.wavelength).toFixed(0), px - 10, Math.max(10, py - 6));
    }
    ctx.fillStyle = getCss('--fg-3');
    ctx.fillText(`${x0.toFixed(0)} nm`, 6, ch - 3);
    ctx.fillText(`${x1.toFixed(0)} nm`, cw - 56, ch - 3);
  }

  function renderPairs(pairs) {
    const tb = $('#wl-pairs').tBodies[0];
    tb.innerHTML = '';
    (pairs || []).forEach((pr) => addRow(pr[0], pr[1]));
  }
  function addRow(px, nm) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><input type="number" step="any" value="${px == null ? '' : esc(Number(px).toFixed(2))}"></td>` +
      `<td><input type="number" step="any" value="${nm == null ? '' : esc(Number(nm).toFixed(3))}"></td>` +
      '<td><button class="row-btn">Delete</button></td>';
    tr.querySelector('button').addEventListener('click', () => tr.remove());
    $('#wl-pairs').tBodies[0].appendChild(tr);
  }
  function readPairs() {
    const out = [];
    for (const tr of $('#wl-pairs').tBodies[0].rows) {
      const i = tr.querySelectorAll('input');
      const a = parseFloat(i[0].value), b = parseFloat(i[1].value);
      if (isFinite(a) && isFinite(b)) out.push([a, b]);
    }
    return out;
  }

  const fail = (j, r) => {
    const code = j && j.error && j.error.code;
    if (code === 'wavelength_not_supported') {
      return 'This device does not support wavelength calibration (' + errText(j, r) + ')';
    }
    return errText(j, r);
  };

  async function startSession() {
    const { r, j } = await apiPost(`${W}/session`);
    if (!r.ok) { active = false; setStatus(fail(j, r), 'error'); return; }
    active = true; saved = !!j.saved;
    setStatus(`Session: ${j.device} · ${j.n_pixels} px · ${Number(j.wavelength_min).toFixed(1)}–${Number(j.wavelength_max).toFixed(1)} nm${j.resumed ? ' (resumed)' : ''}`);
    renderPairs(j.pairs);
  }

  async function loadLamps() {
    const { r, j } = await apiJson(`${W}/lamps`);
    const sel = $('#wl-lamp'); sel.innerHTML = '';
    if (r.ok && j) for (const l of j.lamps) sel.appendChild(new Option(`${l.name} (${(l.lines || []).length} lines)`, l.name));
  }

  async function capture() {
    const body = { frames: parseInt($('#wl-frames').value, 10) || 16 };
    const ms = parseFloat($('#wl-int').value);
    if (ms > 0) body.integration_ms = ms;
    setStatus('Capturing…');
    const { r, j } = await apiPost(`${W}/capture`, body);
    if (!r.ok) { setStatus(fail(j, r), 'error'); return; }
    preview = j.preview; peaks = j.peaks || [];
    $('#wl-capture-info').textContent =
      `${j.n_frames} frames at ${Number(j.integration_ms).toFixed(2)} ms · peak ${Number(j.peak_counts).toFixed(0)} counts · ` +
      `${j.n_peaks} peaks` + (j.n_saturated ? ` · ${j.n_saturated} saturated` : '') +
      (j.warning ? `  ⚠ ${j.warning}` : '');
    $('#wl-capture-info').className = 'cal-status' + (j.has_signal ? '' : ' warn');
    setStatus('Capture done.');
    drawPlot();
  }

  async function match() {
    const lamp = $('#wl-lamp').value;
    if (!lamp) { toast('Choose a lamp'); return; }
    const { r, j } = await apiPost(`${W}/match`, { lamp, accumulate: $('#wl-accumulate').checked });
    if (!r.ok) { setStatus(fail(j, r), 'error'); return; }
    renderPairs(j.pairs);
    setStatus(`Matched ${j.pairs.length} pair(s).`);
  }

  async function applyPairs() {
    const { r, j } = await apiPut(`${W}/pairs`, { pairs: readPairs() });
    if (!r.ok) { setStatus(fail(j, r), 'error'); return false; }
    renderPairs(j.pairs);
    setStatus(`${j.n_total} pair(s) set.`);
    return true;
  }

  async function fit() {
    if (!(await applyPairs())) return;
    const { r, j } = await apiPost(`${W}/fit`, { degree: parseInt($('#wl-degree').value, 10) || 3 });
    const box = $('#wl-fit-result');
    const res = r.ok ? j : (j && j.error && j.error.details) || null;
    if (!r.ok && !(res && res.reasons)) { setStatus(fail(j, r), 'error'); return; }
    const lines = [];
    let cls = 'ok', title = 'Fit accepted (live preview applied)';
    if (!r.ok) {
      cls = 'bad'; title = 'Fit rejected: previous axis kept';
      lines.push('<ul>' + (res.reasons || []).map((x) => `<li>${esc(x)}</li>`).join('') + '</ul>');
    }
    const f = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d);
    lines.push(`<div class="kv">degree ${esc(res.degree)} · ${esc(res.n_lines)} lines · max error ${f(res.max_error, 3)} nm · RMS ${f(res.rms, 3)} nm</div>`);
    if (res.coverage_warning) { lines.push(`<div class="kv">⚠ ${esc(res.coverage_warning)}</div>`); if (cls === 'ok') cls = 'warn'; }
    if (res.rms_warning) { lines.push(`<div class="kv">⚠ ${esc(res.rms_warning)}</div>`); if (cls === 'ok') cls = 'warn'; }
    box.className = `cr-result ${cls}`;
    box.innerHTML = `<h4>${esc(title)}</h4>${lines.join('')}`;
    box.hidden = false;
  }

  async function save() {
    if (!$('#wl-confirm').checked) { toast('Tick “Back up & confirm” first'); return; }
    const { r, j } = await apiPost(`${W}/save`, { confirm: true });
    if (!r.ok) { setStatus(fail(j, r), 'error'); toast(fail(j, r), 4000); return; }
    saved = true;
    setStatus(`Saved. Backup written on the server: ${j.backup_file}` + (j.extra_note ? ` · ${j.extra_note}` : ''), 'ok');
    toast('Wavelength calibration saved');
    $('#wl-confirm').checked = false;
  }

  async function closeDialog() {
    showModal('wlcal-dialog', false);
    if (active) {
      active = false;
      const { r, j } = await apiDelete(`${W}/session`);
      if (r.ok && j && j.rolled_back) toast('Unsaved wavelength preview rolled back');
    }
  }

  async function open() {
    showModal('wlcal-dialog', true);
    preview = null; peaks = []; saved = false;
    $('#wl-fit-result').hidden = true; $('#wl-capture-info').textContent = '';
    drawPlot();
    await Promise.all([startSession(), loadLamps()]);
  }

  function init() {
    $('#wlcal-open').addEventListener('click', open);
    $('#wlcal-close').addEventListener('click', closeDialog);
    $('#wlcal-x').addEventListener('click', closeDialog);
    $('#wl-capture').addEventListener('click', capture);
    $('#wl-match').addEventListener('click', match);
    $('#wl-add-row').addEventListener('click', () => addRow(null, null));
    $('#wl-apply-pairs').addEventListener('click', applyPairs);
    $('#wl-fit').addEventListener('click', fit);
    $('#wl-save').addEventListener('click', save);
  }
  return { init, close: closeDialog };
})();

// Esc closes the non-critical dialogs (never a pending user step / token prompt).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#calmgr-dialog').hidden) CalMgr.close();
  else if (!$('#wlcal-dialog').hidden) WlCal.close();
  else if (!$('#calrun-dialog').hidden && !CalRun.isRunning()) CalRun.close();
});

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
      if (wl == null) return;
      $('#ro-pixel').textContent = idx;
      $('#ro-wave').innerHTML = `${wl.toFixed(1)}<span class="unit"> nm</span>`;
      // The filter tab works on the corrected data (frame.intensities).
      const u2 = State.unit === 'counts' ? 'ADC' : (State.unitLabel || State.unit);
      const txt = (it == null || !isFinite(it)) ? '–'
        : (State.unit === 'counts' ? it.toFixed(0) : Number(it).toPrecision(4));
      $('#ro-int').innerHTML  = `${txt}<span class="unit"> ${esc(u2)}</span>`;
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
    apiFetch('/api/filter', {
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
    apiFetch('/api/filter/report', {
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
    apiFetch('/api/tm30', {
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