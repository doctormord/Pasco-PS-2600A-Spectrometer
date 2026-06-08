#!/usr/bin/env python3
"""Generate all figures for the user manual (vector PDF, light background)."""
import os, sys, numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

sys.path.insert(0, "/mnt/project")
FIG = os.path.join(os.path.dirname(__file__), "figs")
os.makedirs(FIG, exist_ok=True)

plt.rcParams.update({
    "figure.dpi": 120, "savefig.bbox": "tight", "font.size": 9.5,
    "axes.grid": True, "grid.alpha": 0.25, "axes.spines.top": False,
    "axes.spines.right": False, "font.family": "DejaVu Sans",
})
ACC = "#0d7f74"; ACC2 = "#b45309"; GREY = "#555"

def save(fig, name):
    fig.savefig(os.path.join(FIG, name)); plt.close(fig)
    print("wrote", name)

# ───────────────────────────── 1. Architecture / data path ────────────────
def fig_architecture():
    fig, ax = plt.subplots(figsize=(7.2, 4.6)); ax.axis("off")
    ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.grid(False)
    def box(x, y, w, h, t, c="#eef3f3", ec=ACC):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.04,rounding_size=0.12",
                    fc=c, ec=ec, lw=1.3))
        ax.text(x+w/2, y+h/2, t, ha="center", va="center", fontsize=8.6)
    def arrow(x1, y1, x2, y2):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                    mutation_scale=12, lw=1.3, color=GREY))
    box(0.3, 8.4, 3.0, 1.2, "Hardware driver\n_device_*.py\n(acquire + tag frame)", "#fdeede", ACC2)
    box(3.7, 8.4, 2.6, 1.2, "Fast Preview\nshort-exposure\nscale-up (in driver)", "#fdeede", ACC2)
    box(0.3, 6.4, 3.0, 1.1, "SpectrumFusion\n(optional temporal\nsmoothing) fusion.py")
    box(3.9, 6.4, 3.0, 1.1, "Emit timer\n_fused_emit\n(→ GUI thread)")
    box(0.3, 4.4, 3.0, 1.1, "processing.py\ndark / average /\nspatial smooth / resp.")
    box(6.7, 8.4, 3.0, 1.2, "BaseSpectrometer\non_frame(pixels, dark,\nintegration_us, …)", "#e8eef7", "#3b5bdb")
    box(3.9, 4.4, 5.8, 1.1, "process_new_spectrum  (gui_dashboard / web_server)")
    box(0.3, 2.2, 2.1, 1.3, "Scope")
    box(2.6, 2.2, 2.1, 1.3, "Heatmap\n(waterfall)")
    box(4.9, 2.2, 2.3, 1.3, "Color · CRI\n+ TM-30 / CQS\ncolor_science.py")
    box(7.4, 2.2, 2.3, 1.3, "Filter\nfilter_analysis.py")
    box(2.6, 0.3, 4.6, 1.1, "Reports  PDF / PNG / CSV   (shared renderers)")
    arrow(1.8, 8.4, 1.8, 7.5); arrow(8.2, 8.4, 5.4, 5.5)
    arrow(1.8, 6.4, 1.8, 5.5); arrow(5.4, 6.4, 5.4, 5.5)
    arrow(1.8, 4.4, 3.0, 5.0)
    for x in (1.35, 3.65, 6.05, 8.55): arrow(x, 4.4, x, 3.5)
    arrow(6.0, 2.2, 5.0, 1.4); arrow(8.5, 2.2, 6.0, 1.4)
    ax.set_title("Architecture & frame data path", fontsize=11)
    save(fig, "fig_architecture.pdf")

# ───────────────────────────── 2. GUI overview schematic ──────────────────
def fig_gui():
    fig, ax = plt.subplots(figsize=(7.4, 4.4)); ax.axis("off")
    ax.set_xlim(0, 12); ax.set_ylim(0, 8); ax.grid(False)
    ax.add_patch(FancyBboxPatch((0.1, 0.1), 11.8, 7.8, boxstyle="round,pad=0.02",
                 fc="#fbfdfd", ec="#222", lw=1.4))
    # tab bar
    tabs = ["Scope", "Heatmap", "Color · CRI", "Replay", "Filter"]
    for i, t in enumerate(tabs):
        on = (t == "Color · CRI")
        ax.add_patch(FancyBboxPatch((0.4+i*1.7, 7.0), 1.6, 0.6, boxstyle="round,pad=0.03",
                     fc=ACC if on else "#e9efef", ec=ACC))
        ax.text(0.4+i*1.7+0.8, 7.3, t, ha="center", va="center", fontsize=7.5,
                color="white" if on else "#222")
    # plot area
    ax.add_patch(FancyBboxPatch((0.4, 0.4), 8.0, 6.2, boxstyle="round,pad=0.02",
                 fc="#f0f5f5", ec=ACC, lw=1.0))
    ax.text(4.4, 6.2, "Main plot / canvas area", ha="center", fontsize=9, color=ACC)
    ax.text(4.4, 3.4, "(per-tab content:\nlive trace, waterfall, CIE\ndiagram + radar, T(λ) …)",
            ha="center", va="center", fontsize=8, color="#444")
    # sidebar
    ax.add_patch(FancyBboxPatch((8.7, 0.4), 3.0, 6.2, boxstyle="round,pad=0.02",
                 fc="#eef1f6", ec="#3b5bdb", lw=1.0))
    secs = ["Acquisition · 04", "Smoothing · 05", "Display · 03", "Processing · 06"]
    for i, s in enumerate(secs):
        ax.add_patch(FancyBboxPatch((8.9, 5.4-i*1.25), 2.6, 0.95, boxstyle="round,pad=0.03",
                     fc="white", ec="#3b5bdb"))
        ax.text(10.2, 5.87-i*1.25, s, ha="center", va="center", fontsize=8)
    ax.text(10.2, 6.3, "Control sidebar", ha="center", fontsize=8.5, color="#3b5bdb")
    ax.set_title("GUI layout (desktop & web are equivalent)", fontsize=11)
    save(fig, "fig_gui_overview.pdf")

# ───────────────────────────── 3. Pixel→wavelength mapping ─────────────────
def fig_mapping():
    C = [75.45192816, 0.345838363, -2.33680103e-05, 1.22593755e-09]
    i = np.arange(3648)
    wl = np.polyval(list(reversed(C)), i)
    disp = np.gradient(wl, i)  # nm/px
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.4, 3.0))
    a1.plot(i, wl, color=ACC, lw=1.6)
    a1.set_xlabel("pixel index i"); a1.set_ylabel("wavelength  λ(i)  [nm]")
    a1.set_title("PASCO PS-2600A pixel → λ (cubic)")
    for ln in (467.8, 480.0, 508.6, 643.8):
        a1.axhline(ln, color=ACC2, lw=0.7, ls="--", alpha=0.7)
    a1.text(60, 720, "Cd calibration lines\n467.8 / 480.0 / 508.6 / 643.8 nm",
            fontsize=7, color=ACC2)
    a2.plot(wl, disp, color="#3b5bdb", lw=1.6)
    a2.set_xlabel("wavelength [nm]"); a2.set_ylabel("dispersion  dλ/dpx  [nm/px]")
    a2.set_title("Reciprocal linear dispersion")
    save(fig, "fig_mapping.pdf")

# ───────────────────────────── 4. Reference spectra grid ──────────────────
def _load_csv():
    p = "/mnt/project/reference_spectra.csv"
    hdr = open(p, encoding="utf-8").readline().rstrip("\n").split(";")
    data = np.genfromtxt(p, delimiter=";", skip_header=1)
    wl = data[:, 0]
    cols = {h.strip(): data[:, k] for k, h in enumerate(hdr)}
    return wl, cols

def fig_refspectra():
    wl, cols = _load_csv()
    picks = ["Solar AM1.5G", "LED White Cool", "Fluorescent 3-band",
             "Mercury (Hg)", "Sodium (Na)", "Tungsten" if "Tungsten" in cols else "LED Red"]
    picks = [p for p in picks if p in cols][:6]
    fig, axes = plt.subplots(2, 3, figsize=(7.6, 4.2), sharex=True)
    for ax, name in zip(axes.ravel(), picks):
        y = cols[name]; m = np.nanmax(y) or 1.0
        ax.fill_between(wl, y/m, color=ACC, alpha=0.35); ax.plot(wl, y/m, color=ACC, lw=0.9)
        ax.set_title(name, fontsize=8.5); ax.set_xlim(300, 1000); ax.set_ylim(0, 1.05)
        ax.set_yticks([])
    for ax in axes[-1]:
        ax.set_xlabel("λ [nm]")
    fig.suptitle("Reference spectrum library (peak-normalised) — reference_spectra.csv", fontsize=10)
    save(fig, "fig_refspectra.pdf")

# ───────────────────────────── 5. Demo soft saturation ────────────────────
def fig_demo_sat():
    full = 65535.0; frac = 0.98; knee = 0.85; fs_ms = 1000.0
    bias, dark_rate = 500.0, 1.0
    t = np.linspace(1, 1000, 500)
    lin = frac * full * (t / fs_ms)            # ideal linear net
    k = knee * full
    def soft(x):
        out = x.copy()
        hi = x > k
        out[hi] = k + (full - k) * (1 - np.exp(-(x[hi]-k)/(full-k)))
        return out
    net = soft(lin)
    dark = bias + dark_rate * t
    fig, ax = plt.subplots(figsize=(5.2, 3.2))
    ax.plot(t, lin, ls="--", color=GREY, lw=1.2, label="ideal linear (no saturation)")
    ax.plot(t, net, color=ACC, lw=1.8, label="soft-saturated net signal")
    ax.plot(t, net+dark, color=ACC2, lw=1.2, label="net + dark pedestal")
    ax.axhline(knee*full, color="#999", lw=0.7, ls=":"); ax.text(20, knee*full+1200,
            "soft knee (85 %)", fontsize=7, color="#666")
    ax.axhline(full, color="red", lw=0.7, ls=":"); ax.text(20, full-3500, "full scale (16-bit)", fontsize=7, color="red")
    ax.set_xlabel("exposure [ms]"); ax.set_ylabel("peak signal [ADC]")
    ax.set_title("Demo device: signal vs exposure"); ax.legend(fontsize=7, loc="lower right")
    save(fig, "fig_demo_saturation.pdf")

# ───────────────────────────── 6. Filter transmission example ─────────────
def fig_filter():
    wl = np.linspace(400, 800, 1601)
    T = 0.92*np.exp(-((wl-550)/22)**2) + 0.002   # bandpass ~550, FWHM~52
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.6, 3.0))
    a1.plot(wl, T*100, color=ACC, lw=1.8)
    pk = T.max()*100; half = pk/2
    pk_i = int(np.argmax(T))
    a1.axhline(half, color=GREY, ls="--", lw=0.8)
    # half-maximum crossings on EACH side of the peak (not the array midpoint)
    li = wl[:pk_i][np.argmin(np.abs(T[:pk_i]*100 - half))]
    ri = wl[pk_i:][np.argmin(np.abs(T[pk_i:]*100 - half))]
    a1.axvline(li, color=ACC2, ls=":", lw=0.9); a1.axvline(ri, color=ACC2, ls=":", lw=0.9)
    a1.annotate("", (li, half), (ri, half), arrowprops=dict(arrowstyle="<->", color=ACC2))
    a1.text((li+ri)/2, half+4, f"FWHM ≈ {ri-li:.0f} nm", ha="center", fontsize=7.5, color=ACC2)
    a1.plot([wl[pk_i]], [pk], "v", color=ACC, ms=6)
    a1.text(wl[pk_i], pk+1.6, f"peak T @ {wl[pk_i]:.0f} nm", ha="center", fontsize=7.5)
    a1.set_xlabel("λ [nm]"); a1.set_ylabel("transmission T [%]")
    a1.set_title("Band-pass filter — transmission")
    od = -np.log10(np.clip(T, 1e-6, None))
    a2.plot(wl, od, color="#3b5bdb", lw=1.6)
    a2.set_xlabel("λ [nm]"); a2.set_ylabel("optical density  OD = −log₁₀ T")
    a2.set_title("Blocking / OD")
    save(fig, "fig_filter_example.pdf")

# ───────────────────────────── 7. Waterfall illustration ──────────────────
def fig_waterfall():
    wl = np.linspace(380, 750, 300); frames = 120
    img = np.zeros((frames, wl.size))
    for f in range(frames):
        c = 500 + 120*np.sin(f/18.0)
        img[f] = np.exp(-((wl-c)/24)**2) + 0.05*np.random.rand(wl.size)
    # newest at bottom → row 0 newest. imshow origin lower puts row 0 at bottom.
    fig, ax = plt.subplots(figsize=(5.4, 3.2))
    ax.imshow(img, aspect="auto", origin="lower", cmap="inferno",
              extent=[wl[0], wl[-1], 0, frames])
    ax.set_xlabel("λ [nm]"); ax.set_ylabel("frame age (0 = newest, bottom)")
    ax.set_title("Heatmap / waterfall — newest at bottom, scrolls up")
    ax.grid(False)
    save(fig, "fig_waterfall.pdf")

# ───────────────────────────── 8. Planckian locus + Duv ───────────────────
def fig_cie():
    try:
        import color_science as cs
    except Exception as e:
        print("skip CIE (color_science import failed):", e); return
    Ts = np.linspace(1500, 15000, 60); xs, ys = [], []
    for T in Ts:
        spd = cs.planckian_spd(T)
        X, Y, Z = cs.spectrum_to_xyz(cs.CIE_WL_5, spd)
        x, y = cs.xyz_to_xy(X, Y, Z); xs.append(x); ys.append(y)
    fig, ax = plt.subplots(figsize=(4.8, 4.2))
    ax.plot(xs, ys, color="#222", lw=1.6, label="Planckian locus")
    # demo points
    wl = np.linspace(340, 850, 2048)
    for name, col in [("led_white", ACC), ("Fluorescent 2-band", ACC2)]:
        try:
            from _device_demo import _resolve_spectrum
            sh = _resolve_spectrum(name, wl)
            m = cs.measure_all(wl, sh, 0.0)
            ax.scatter([m["x"]], [m["y"]], s=42, color=col, zorder=5,
                       label=f"{name}  (Duv={m['duv']:+.3f})")
        except Exception as ex:
            print("pt skip", name, ex)
    for T in (2700, 4000, 6500):
        spd = cs.planckian_spd(T); X, Y, Z = cs.spectrum_to_xyz(cs.CIE_WL_5, spd)
        x, y = cs.xyz_to_xy(X, Y, Z); ax.annotate(f"{T} K", (x, y), (x+0.01, y+0.015), fontsize=7)
    ax.set_xlabel("CIE 1931 x"); ax.set_ylabel("CIE 1931 y")
    ax.set_title("Planckian locus & off-locus Duv"); ax.legend(fontsize=6.8, loc="upper right")
    ax.set_xlim(0.15, 0.6); ax.set_ylim(0.1, 0.55)
    save(fig, "fig_cie_locus.pdf")

for f in (fig_architecture, fig_gui, fig_mapping, fig_refspectra,
          fig_demo_sat, fig_filter, fig_waterfall, fig_cie):
    try:
        f()
    except Exception as e:
        print("FIG ERROR", f.__name__, e)
print("done")
