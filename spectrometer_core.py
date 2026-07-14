"""
spectrometer_core.py — Application-wide constants and generic utilities.

This module is intentionally device-agnostic.  It contains:
  - Application-wide display/GUI constants (heatmap size, Y-axis margin, etc.)
  - Dark mode string constants shared by all drivers
  - The reference spectrum library generator
  - The ContextHelp delayed-tooltip widget helper

Device-specific code (USB communication, acquisition threads, hardware
constants, wavelength calibration) lives exclusively in each driver:
  _device_pasco.py   — PASCO PS-2600A
  _device_lr2t.py    — ASEQ Instruments / Lasertrack LR-2T
  _device_ocean.py   — Ocean Optics / Ocean Insight (seabreeze)

Calibration utilities (polynomial fitting, response compensation) are in:
  calibration_utils.py
"""

from __future__ import annotations

import csv
import os
import sys

import numpy as np

# ── Optional Qt imports (only needed for ContextHelp) ─────────────────────
try:
    from PyQt6.QtWidgets import QToolTip
    from PyQt6.QtCore import QObject, QTimer, QEvent
    _QT_AVAILABLE = True
except ImportError:
    _QT_AVAILABLE = False

# ── Application-wide display constants ────────────────────────────────────

# Heatmap / waterfall
HEATMAP_HISTORY_SIZE   = 100

# Plot Y-axis
Y_AXIS_BOTTOM_MARGIN_ADC = -20
DEFAULT_Y_MAX_ADC        = 4000

# Default wavelength display range (nm) — may be overridden per device
DEFAULT_X_MIN_NM = 300
DEFAULT_X_MAX_NM = 1050

# Dark correction mode strings (shared by all drivers and the GUI)
DARK_MODE_OPTICAL_BLACK = "optical_black"
DARK_MODE_PARAMETRIC    = "parametric"

# Reference library
REFERENCE_LIBRARY_FILENAME = "reference_spectra.csv"

def ensure_reference_library_exists():
    """
    Generates reference_spectra.csv.
    Regenerated when "Tungsten 2856K (Illuminant A)" column is absent
    (version sentinel — bump this string whenever columns are added).

    Normalisation strategy
    ----------------------
    All spectra are stored as relative values 0.0 – 1.0 where 1.0 = the
    brightest point in the spectrum.  Within each CLASS (discharge lamps,
    fluorescent, LED colour, LED white) the spectra are co-normalised so
    their relative brightness is preserved: the brightest spectrum in the
    class sets the 1.0 anchor, all others are scaled down proportionally.
    This means you can compare e.g. "how bright is the 546 nm Hg line vs
    the 589 nm Na doublet" without either spectrum clipping at 3500 ADC.

    When the GUI overlays a reference, it scales 1.0 → current_y_max × 0.9
    so the reference always fits the current measurement regardless of
    signal level.
    """
    sentinel_col = "Tungsten 2856K (Illuminant A)"
    if os.path.exists(REFERENCE_LIBRARY_FILENAME):
        try:
            with open(REFERENCE_LIBRARY_FILENAME, newline="") as f:
                header = f.readline()
            if sentinel_col in header:
                return  # up to date
        except Exception:
            pass
    print(f"Generating reference library: {REFERENCE_LIBRARY_FILENAME}")

    W = 0.5   # narrow line FWHM for discharge lamps (nm)

    # ── Raw spectral definitions ──────────────────────────────────────────
    # Each entry:  (center_nm, relative_weight, fwhm_nm)
    # Weight is internal only — everything gets normalised before writing.
    spectral_defs = {
        # ── Discharge lamps ──────────────────────────────────────────────
        "Hydrogen (H)": [
            (410.2, 0.43, W), (434.0, 0.57, W), (486.1, 0.86, W), (656.3, 1.00, W)
        ],
        "Mercury (Hg)": [
            (365.0, 0.23, W), (404.7, 0.43, W), (407.8, 0.11, W),
            (435.8, 0.86, W), (546.1, 1.00, W), (576.9, 0.57, W), (579.1, 0.51, W)
        ],
        "Sodium (Na)": [
            (568.8, 0.06, W), (589.0, 1.00, W), (589.6, 0.86, W),
            (615.4, 0.09, W), (616.1, 0.07, W)
        ],
        "Neon (Ne)": [
            (585.2, 0.27, W), (594.5, 0.20, W), (597.6, 0.23, W), (607.4, 0.30, W),
            (614.3, 0.17, W), (616.4, 0.20, W), (621.7, 0.27, W), (626.6, 0.23, W),
            (630.5, 0.40, W), (633.4, 0.30, W), (638.3, 0.50, W), (640.2, 0.67, W),
            (650.6, 1.00, W), (659.9, 0.33, W), (667.8, 0.20, W), (671.7, 0.17, W),
            (692.9, 0.40, W), (703.2, 0.83, W), (717.4, 0.20, W), (724.5, 0.17, W),
            (743.9, 0.27, W)
        ],
        "Argon (Ar)": [
            (696.5, 0.34, W), (706.7, 0.29, W), (714.7, 0.17, W), (727.3, 0.23, W),
            (738.4, 0.57, W), (750.4, 0.86, W), (763.5, 1.00, W), (772.4, 0.23, W),
            (794.8, 0.17, W), (800.6, 0.14, W), (811.5, 0.71, W), (826.5, 0.17, W),
            (840.8, 0.29, W), (842.5, 0.34, W)
        ],
        "Helium (He)": [
            (388.9, 0.17, W), (396.5, 0.10, W), (447.1, 0.50, W), (471.3, 0.27, W),
            (492.2, 0.33, W), (501.6, 0.40, W), (587.6, 1.00, W),
            (667.8, 0.33, W), (706.5, 0.27, W), (728.1, 0.17, W)
        ],
        "Krypton (Kr)": [
            (427.4, 0.25, W), (431.9, 0.30, W), (436.3, 0.35, W), (450.2, 0.20, W),
            (461.5, 0.25, W), (473.9, 0.40, W), (476.2, 0.30, W), (482.5, 0.35, W),
            (557.0, 0.30, W), (587.1, 0.40, W), (602.0, 0.25, W),
            (760.2, 0.60, W), (769.5, 0.90, W), (785.5, 0.30, W),
            (810.4, 0.45, W), (819.0, 0.75, W), (829.8, 1.00, W)
        ],
        "Xenon (Xe)": [
            (450.1, 0.30, W), (462.4, 0.35, W), (467.1, 0.25, W),
            (473.4, 0.40, W), (480.7, 0.30, W), (492.3, 0.25, W),
            (823.2, 1.00, W), (828.0, 0.75, W), (834.7, 0.90, W), (880.0, 0.60, W)
        ],
        "Cadmium (Cd)": [
            (346.6, 0.10, W), (361.1, 0.13, W), (467.8, 0.50, W),
            (480.0, 0.67, W), (508.6, 0.40, W), (643.8, 1.00, W)
        ],
        # ── Mixed gas ────────────────────────────────────────────────────
        "Neon-Argon Mix": [
            (614.3, 0.20, W), (621.7, 0.30, W), (638.3, 0.60, W), (640.2, 0.75, W),
            (650.6, 1.00, W), (692.9, 0.45, W), (703.2, 0.80, W), (743.9, 0.30, W),
            (696.5, 0.40, W), (706.7, 0.35, W), (738.4, 0.60, W),
            (750.4, 0.70, W), (763.5, 0.80, W), (811.5, 0.60, W)
        ],
        # ── Fluorescent tubes (Hg background + phosphor bands) ───────────
        "Fluorescent 2-band": [
            (365.0, 0.16, W), (404.7, 0.24, W), (435.8, 0.60, W),
            (546.1, 0.40, W), (576.9, 0.32, W),
            (452.0, 0.80, 20.0), (612.0, 1.00, 25.0)
        ],
        "Fluorescent 3-band": [
            (365.0, 0.10, W), (404.7, 0.17, W), (435.8, 0.33, W),
            (546.1, 0.27, W), (576.9, 0.20, W),
            (453.0, 0.83, 15.0), (542.0, 0.67, 18.0), (611.0, 1.00, 20.0)
        ],
        "Fluorescent 4-band": [
            (365.0, 0.11, W), (404.7, 0.18, W), (435.8, 0.36, W),
            (546.1, 0.29, W), (576.9, 0.21, W),
            (405.0, 0.43, 10.0), (453.0, 0.71, 15.0),
            (542.0, 0.64, 18.0), (611.0, 1.00, 20.0)
        ],
        # ── Single-colour LEDs ────────────────────────────────────────────
        # Shapes differ, but within this class peak = 1.0 and FWHM reflects
        # the typical spectral width (narrower = deeper colour).
        "LED Violet": [(405.0, 1.00, 14.0)],
        "LED Blue":   [(465.0, 1.00, 24.0)],
        "LED Cyan":   [(500.0, 1.00, 28.0)],
        "LED Green":  [(520.0, 1.00, 34.0)],
        "LED Yellow": [(578.0, 1.00, 18.0)],
        "LED Amber":  [(593.0, 1.00, 18.0)],
        "LED Orange": [(617.0, 1.00, 20.0)],
        "LED Red":    [(638.0, 1.00, 22.0)],
        # ── White LEDs (blue InGaN pump + broad phosphor) ─────────────────
        # The pump peak is weaker relative to the phosphor band in real
        # warm-white LEDs; values reflect typical relative magnitudes.
        "LED White Warm":    [(455.0, 0.44, 25.0), (570.0, 1.00, 120.0)],
        "LED White Neutral": [(450.0, 0.40, 22.0), (545.0, 1.00, 100.0)],
        "LED White Cool":    [(445.0, 0.39, 20.0), (530.0, 1.00,  85.0)],
        # ── Solar spectrum (AM1.5G approximation) ─────────────────────────
        # Blackbody 5778 K envelope with key Fraunhofer absorption lines
        # and atmospheric bands, normalised to 1.0 at ~550 nm.
        "Solar AM1.5G": [],   # generated analytically below
        # ── Incandescent / thermal sources (Planckian, no lines) ──────────
        # Continuous blackbody-like emitters. Each is a Planck curve at its
        # colour temperature (tungsten filament ≈ graybody), rising steadily
        # toward the red/NIR — the classic warm-source shape. Generated below.
        "Incandescent 2700K":            [],   # classic household bulb (Glühlampe)
        "Tungsten 2856K (Illuminant A)": [],   # CIE Standard Illuminant A
        "Halogen 3000K":                 [],   # typical halogen
        "Halogen 3200K":                 [],   # studio / photographic tungsten-halogen
    }

    # Colour temperatures (K) for the Planckian thermal sources above.
    BLACKBODY_SOURCES = {
        "Incandescent 2700K":            2700.0,
        "Tungsten 2856K (Illuminant A)": 2856.0,
        "Halogen 3000K":                 3000.0,
        "Halogen 3200K":                 3200.0,
    }

    # Normalisation groups — spectra within a group share the same
    # 1.0-reference point so relative brightness is comparable.
    # Groups:  list of keys that belong together.
    # The brightest spectrum in each group (peak of the rendered curve)
    # defines 1.0; others are scaled proportionally.
    CLASS_GROUPS = [
        ["Hydrogen (H)", "Mercury (Hg)", "Sodium (Na)", "Neon (Ne)",
         "Argon (Ar)", "Helium (He)", "Krypton (Kr)", "Xenon (Xe)", "Cadmium (Cd)"],
        ["Neon-Argon Mix"],
        ["Fluorescent 2-band", "Fluorescent 3-band", "Fluorescent 4-band"],
        ["LED Violet", "LED Blue", "LED Cyan", "LED Green",
         "LED Yellow", "LED Amber", "LED Orange", "LED Red"],
        ["LED White Warm", "LED White Neutral", "LED White Cool"],
        ["Solar AM1.5G"],
        # Each thermal source normalised to its own peak so it fills the plot
        # (the overlay is rescaled to the live Y-max, which assumes peak = 1.0).
        ["Incandescent 2700K"],
        ["Tungsten 2856K (Illuminant A)"],
        ["Halogen 3000K"],
        ["Halogen 3200K"],
    ]

    generic_waves = np.arange(300.0, 1100.5, 0.5)
    sigma_cache = {}

    def _render(peaks, waves):
        """Render a list of (center, weight, fwhm) peaks onto waves → array."""
        out = np.zeros(len(waves))
        for center, weight, fwhm in peaks:
            if fwhm not in sigma_cache:
                sigma_cache[fwhm] = fwhm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
            sig = sigma_cache[fwhm]
            out += weight * np.exp(-((waves - center) ** 2) / (2.0 * sig ** 2))
        return out

    def _solar_am15(waves):
        """
        Approximate AM1.5G solar irradiance, normalised to 1.0 at 550 nm.
        Uses a 5778 K Planckian envelope scaled by atmospheric transmission,
        plus the 20 most prominent Fraunhofer and molecular absorption dips.
        """
        # Planckian envelope (relative, not absolute)
        h, c, k = 6.626e-34, 3e8, 1.381e-23
        T = 5778.0
        lam = waves * 1e-9
        with np.errstate(over='ignore', invalid='ignore'):
            bb = (2 * h * c**2 / lam**5) / (np.exp(h * c / (lam * k * T)) - 1)
        bb = np.where(np.isfinite(bb), bb, 0.0)

        # Atmospheric transmission envelope (empirical polynomial approximation)
        # UV cutoff below 290 nm, NIR water/O2 bands, general Rayleigh slope
        atm = np.ones(len(waves))
        # UV cutoff
        atm *= np.clip((waves - 280) / 30, 0.0, 1.0)
        # O2 A-band 762 nm (width ~10 nm, depth ~0.35)
        atm *= 1.0 - 0.35 * np.exp(-((waves - 762) / 5) ** 2)
        # O2 B-band 686 nm (width ~4 nm, depth ~0.15)
        atm *= 1.0 - 0.15 * np.exp(-((waves - 686) / 3) ** 2)
        # H2O 720 nm (width ~20 nm, depth ~0.20)
        atm *= 1.0 - 0.20 * np.exp(-((waves - 720) / 12) ** 2)
        # H2O 820 nm (width ~25 nm, depth ~0.25)
        atm *= 1.0 - 0.25 * np.exp(-((waves - 820) / 15) ** 2)
        # H2O 940 nm (width ~30 nm, depth ~0.55)
        atm *= 1.0 - 0.55 * np.exp(-((waves - 940) / 18) ** 2)
        # NIR general falloff
        atm *= np.exp(-np.maximum(0.0, waves - 700) / 900)

        spd = bb * atm

        # Fraunhofer absorption lines (narrow dips)
        fraunhofer = [
            (393.4, 0.70, 0.6),  # Ca II K
            (396.8, 0.65, 0.5),  # Ca II H
            (430.8, 0.25, 0.4),  # CH / Fe blend
            (438.4, 0.20, 0.3),  # Fe
            (486.1, 0.30, 0.5),  # H-beta (Fraunhofer F)
            (516.7, 0.15, 0.4),  # Mg b₃
            (517.3, 0.18, 0.4),  # Mg b₂
            (518.4, 0.22, 0.5),  # Mg b₁
            (527.0, 0.12, 0.3),  # Fe
            (589.0, 0.45, 0.5),  # Na D₂
            (589.6, 0.40, 0.5),  # Na D₁
            (627.4, 0.10, 0.3),  # O₂ (telluric)
            (656.3, 0.35, 0.6),  # H-alpha (Fraunhofer C)
            (686.7, 0.18, 0.4),  # O₂ B-band edge
            (718.0, 0.08, 0.3),  # H₂O
            (759.4, 0.08, 0.3),  # O₂ A-band edge
        ]
        for cen, depth, fw in fraunhofer:
            sig_f = fw / (2.0 * np.sqrt(2.0 * np.log(2.0)))
            dip   = depth * np.exp(-((waves - cen) ** 2) / (2.0 * sig_f ** 2))
            spd  *= (1.0 - dip)

        # Normalise to 1.0 at 550 nm
        idx550 = int(np.argmin(np.abs(waves - 550.0)))
        ref    = spd[idx550]
        if ref > 0:
            spd /= ref
        return np.clip(spd, 0.0, None)

    def _planck(waves, T):
        """Relative Planck spectral radiance at temperature T (K) over waves
        (nm). Absolute scale is irrelevant — the caller normalises. Used for
        tungsten/halogen/incandescent references (tungsten ≈ graybody, so the
        Planckian shape is the standard approximation; Illuminant A is defined
        exactly as a 2856 K Planckian radiator)."""
        h, c, k = 6.626e-34, 3.0e8, 1.381e-23
        lam = np.asarray(waves, dtype=float) * 1e-9
        with np.errstate(over="ignore", invalid="ignore"):
            rad = (2 * h * c**2 / lam**5) / (np.exp(h * c / (lam * k * T)) - 1.0)
        return np.where(np.isfinite(rad), rad, 0.0)

    # ── Render all spectra ─────────────────────────────────────────────────
    rendered = {}
    for name, peaks in spectral_defs.items():
        if name == "Solar AM1.5G":
            rendered[name] = _solar_am15(generic_waves)
        elif name in BLACKBODY_SOURCES:
            rendered[name] = _planck(generic_waves, BLACKBODY_SOURCES[name])
        else:
            rendered[name] = _render(peaks, generic_waves)

    # ── Co-normalise within each class ────────────────────────────────────
    # Find the brightest spectrum in each group, set that as 1.0,
    # scale all others in the group proportionally.
    for group in CLASS_GROUPS:
        group_peaks = [np.max(rendered[k]) for k in group if k in rendered]
        if not group_peaks:
            continue
        class_max = max(group_peaks)
        if class_max <= 0:
            continue
        for k in group:
            if k in rendered:
                rendered[k] = rendered[k] / class_max

    # ── Write CSV ─────────────────────────────────────────────────────────
    headers = ["Wavelength_nm"] + list(spectral_defs.keys())
    with open(REFERENCE_LIBRARY_FILENAME, "w", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(headers)
        for i, wave in enumerate(generic_waves):
            row = [round(float(wave), 2)]
            for name in spectral_defs:
                row.append(round(float(rendered[name][i]), 6))
            writer.writerow(row)


def load_reference_library(target_wavelengths=None):
    """Parse reference_spectra.csv into name -> normalised (0..1) spectrum.

    Shared by the desktop GUI and the web server so both read the same file.
    Generates the library first if it is missing.

    If ``target_wavelengths`` is given, each spectrum is interpolated onto it
    (0.0 outside the reference range) so it lines up with the active device's
    pixel axis; otherwise the native wavelength column is returned.

    Returns ``(names, wavelengths, {name: np.ndarray})``.
    """
    ensure_reference_library_exists()
    names: list[str] = []
    waves: list[float] = []
    columns: dict[str, np.ndarray] = {}
    if not os.path.exists(REFERENCE_LIBRARY_FILENAME):
        return names, np.asarray(waves, dtype=float), columns
    try:
        with open(REFERENCE_LIBRARY_FILENAME, "r", newline="") as fp:
            reader = csv.reader(fp, delimiter=";")
            headers = next(reader)
            names = headers[1:]
            raw = {n: [] for n in names}
            for row in reader:
                if not row or len(row) < 2:
                    continue
                try:
                    waves.append(float(row[0]))
                    for j, n in enumerate(names):
                        raw[n].append(float(row[j + 1]) if j + 1 < len(row) else 0.0)
                except ValueError:
                    continue
        wl = np.asarray(waves, dtype=float)
        if target_wavelengths is not None:
            tw = np.asarray(target_wavelengths, dtype=float)
            for n in names:
                columns[n] = np.interp(tw, wl, raw[n], left=0.0, right=0.0)
            return names, tw, columns
        for n in names:
            columns[n] = np.asarray(raw[n], dtype=float)
        return names, wl, columns
    except Exception as e:
        print(f"[ref-lib] load error: {e}")
        return names, np.asarray(waves, dtype=float), columns


# ============================================================
# USB BACKEND — WinUSB on Windows, libusb (pyusb) elsewhere.
# ============================================================

# ── ContextHelp delayed tooltip ────────────────────────────────────────────

if _QT_AVAILABLE:
    class ContextHelp(QObject):
        """Installs a 2-second delayed tooltip on any QWidget."""

        def __init__(self, widget, text: str, delay_ms: int = 2000):
            super().__init__(widget)
            self._widget = widget
            self._text   = text
            self._timer  = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(delay_ms)
            self._timer.timeout.connect(self._show)
            widget.installEventFilter(self)

        def eventFilter(self, obj, event) -> bool:
            t = event.type()
            if t == QEvent.Type.Enter:
                self._timer.start()
            elif t in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
                self._timer.stop()
                QToolTip.hideText()
            return False

        def _show(self) -> None:
            if self._widget.underMouse():
                pos = self._widget.mapToGlobal(
                    self._widget.rect().bottomLeft())
                QToolTip.showText(pos, self._text, self._widget)

    def add_help(widget, text: str, delay_ms: int = 2000):
        """Attach a delayed context tooltip to a widget.  Returns widget."""
        ContextHelp(widget, text, delay_ms)
        return widget

else:
    def add_help(widget, text: str, delay_ms: int = 2000):
        """No-op when PyQt6 is unavailable (e.g. web server context)."""
        return widget
