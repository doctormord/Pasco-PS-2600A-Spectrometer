"""
fusion.py  —  Device-agnostic display smoothing + emit stage.

WHAT THIS IS (simplified model, 2026-06)
----------------------------------------
Fast Preview is now dead simple and lives entirely in the drivers: when FP is
on, a driver shortens the hardware exposure to `short_pct` of the user exposure,
reads frames back-to-back as fast as the device allows, and scales the net
signal up by `t_long / t_short` so the trace sits at the normal-exposure level
(dark current handled per frame). The driver forwards a FINISHED, displayable
frame — there is no long/short cadence, no interleave, no reconstruction here.

This module therefore has ONE remaining job: optionally soften the per-frame
shot noise of those fast frames without blurring real spectral features, and
push the result to the display on a steady free-running timer so the canvas
feels fluid. With fusion OFF it is a pure pass-through.

WHY IT STILL LIVES OUTSIDE THE DRIVERS
--------------------------------------
It is pure signal processing — no USB, no hardware, no device constants — so it
is shared by every device and by both the Qt GUI and the web server.

NOISE SOFTENING — adaptive per-pixel temporal filter (fusion ON only)
---------------------------------------------------------------------
The estimate F(λ) is an exponential moving average whose blend factor α backs
off where the signal is genuinely changing:

    z(λ)  = |S(λ) − F(λ)| / σ(λ)                # deviation in noise-sigmas
    α(λ)  = α_min + (1 − α_min)·clip(z/change_sigma, 0, 1)
    F(λ) ← α(λ)·S(λ) + (1 − α(λ))·F(λ)

  • Stable pixel (z≈0):       α = α_min  → heavy smoothing → low noise.
  • Changing pixel (z large): α → 1      → snaps to the new value instantly.

It never mixes across wavelength, so no peak is softened or hidden; a brand-new
peak produces a large z and appears immediately at full height. The change test
uses a 5-pixel-smoothed deviation so a single-pixel shot-noise spike (worst at
sharp, low-count peaks) does not trip α→1 and make the peak jitter.

EMIT TIMING
-----------
A free-running timer re-emits the current estimate at a fixed interval, so the
canvas refresh is decoupled from frame arrival (data simply repeats between
frames). The rate is explicit (fusion_emit_hz) or auto-derived from the frame
cadence. With fusion OFF, frames are emitted directly on arrival.
"""

from __future__ import annotations

import threading
import time

import numpy as np


# ── Frame-kind tags ───────────────────────────────────────────────────────
# Kept for backward compatibility with the on_frame(...) contract and existing
# imports. In the simplified model every driver forwards FRAME_STANDARD; the
# long/short tags are no longer produced or acted upon.
FRAME_LONG     = "long"
FRAME_SHORT    = "short"
FRAME_STANDARD = "standard"


# ── Configuration defaults ────────────────────────────────────────────────
# Mirrored in app_config.DEFAULT_CONFIG so they persist and are GUI-editable.
DEFAULTS = {
    "fusion_enabled":      False,
    "fusion_smoothing":    0.85,   # 0..1; temporal smoothing on stable pixels
    "fusion_change_sigma": 4.0,    # deviation (σ) at which smoothing backs off
    "fusion_noise_floor":  8.0,    # read-noise term r (ADC) for the noise model
    "fusion_noise_gain":   1.0,    # shot-noise coefficient g (ADC per √signal)
    "fusion_emit_hz":      0.0,    # 0 = auto-derive from frame cadence
    "fusion_emit_max_hz":  60.0,   # cap for auto mode
    "fusion_emit_min_hz":  2.0,    # floor for auto mode
}


class SpectrumFusion:
    """
    Optional adaptive temporal smoothing of a single frame stream, emitted on a
    free-running timer.

    Usage:
        fusion = SpectrumFusion(emit_callback=gui.process_new_spectrum)
        fusion.configure(**Config.as_dict())
        fusion.start()
        ...
        fusion.submit(pixels, dark, integration_us, kind, ratio)   # from driver
        ...
        fusion.stop()

    emit_callback signature matches the existing GUI/web sink:
        emit_callback(pixels: np.ndarray, dark: float, integration_us: int)
    """

    def __init__(self, emit_callback=None, **params):
        self._emit_cb = emit_callback
        self._p = dict(DEFAULTS)
        self._p.update({k: v for k, v in params.items() if k in DEFAULTS})

        self._lock = threading.Lock()
        self._estimate: np.ndarray | None = None
        self._update_seq = 0          # bumped whenever _estimate changes
        self._last_emitted_seq = -1   # last seq the emit timer pushed
        self._dark = 0.0
        self._integration_us = 0
        self._n_pixels = 0

        # Cadence tracking for auto emit-rate.
        self._last_frame_t = None
        self._frame_period_s = 0.1    # seeded; updated from real arrivals

        # Emitter thread
        self._emit_thread: threading.Thread | None = None
        self._running = False

    # ── Configuration ─────────────────────────────────────────────────────

    def configure(self, **params) -> None:
        """Update parameters live. Unknown keys are ignored."""
        with self._lock:
            for k, v in params.items():
                if k in DEFAULTS:
                    self._p[k] = v

    @property
    def enabled(self) -> bool:
        return bool(self._p["fusion_enabled"])

    # ── Frame intake ──────────────────────────────────────────────────────

    def submit(self, pixels: np.ndarray, dark: float,
               integration_us: int, kind: str = FRAME_STANDARD,
               ratio: float = 1.0) -> None:
        """Entry point called by a driver's frame callback.

        `kind`/`ratio` are accepted for contract compatibility but ignored —
        the driver already delivers a finished, level-correct frame.
        """
        self._submit_frame(pixels, dark, integration_us)

    def _submit_frame(self, pixels, dark, integration_us) -> None:
        S = np.asarray(pixels, dtype=float)
        now = time.monotonic()
        with self._lock:
            self._reset_if_size_changed(len(S))
            self._dark = float(dark)
            self._integration_us = int(integration_us)

            # Track frame cadence for the auto emit-rate.
            if self._last_frame_t is not None:
                dt = now - self._last_frame_t
                if 1e-4 < dt < 10.0:
                    self._frame_period_s = 0.7 * self._frame_period_s + 0.3 * dt
            self._last_frame_t = now

            if not self.enabled or self._estimate is None \
                    or len(self._estimate) != len(S):
                # Pass-through (fusion off) or first frame: adopt as-is.
                self._estimate = S.copy()
                self._update_seq += 1
            else:
                # Adaptive per-pixel temporal smoothing. σ from the frame's own
                # net level (read floor + shot term); the frame is already at
                # long-equivalent level so r,g are in display ADC units.
                r = float(self._p["fusion_noise_floor"])
                g = float(self._p["fusion_noise_gain"])
                net = np.maximum(S - self._dark, 0.0)
                sigma = np.maximum(np.sqrt(r * r + (g * g) * net), 1e-6)

                # 5-pt-smoothed deviation so single-pixel shot spikes (worst at
                # sharp, low-count peaks) don't trip α→1; a real change spans
                # several pixels and survives the smoothing.
                dev = np.abs(S - self._estimate)
                k = np.ones(5) / 5.0
                dev_s = np.convolve(np.pad(dev, 2, mode="edge"), k, mode="valid")
                z = dev_s / sigma

                smoothing = float(np.clip(self._p["fusion_smoothing"], 0.0, 0.99))
                a_min = max(1.0 - smoothing, 0.02)
                change_sigma = max(float(self._p["fusion_change_sigma"]), 0.1)
                alpha = a_min + (1.0 - a_min) * np.clip(z / change_sigma, 0.0, 1.0)
                self._estimate = alpha * S + (1.0 - alpha) * self._estimate
                self._update_seq += 1

        if not self.enabled:
            # No timer needed when off — emit on arrival.
            self._emit_now()

    # ── Emitter (free-running timer) ──────────────────────────────────────

    def start(self) -> None:
        if self._emit_thread is not None:
            return
        self._running = True
        self._emit_thread = threading.Thread(
            target=self._emit_loop, daemon=True, name="fusion-emit")
        self._emit_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._emit_thread:
            self._emit_thread.join(timeout=2.0)
            self._emit_thread = None

    def reset(self) -> None:
        with self._lock:
            self._estimate = None
            self._last_frame_t = None

    def current(self) -> np.ndarray | None:
        """Thread-safe copy of the current estimate."""
        with self._lock:
            return None if self._estimate is None else self._estimate.copy()

    def _emit_interval(self) -> float:
        """Seconds between emits — explicit Hz, or auto from frame cadence."""
        hz = float(self._p["fusion_emit_hz"])
        if hz > 0:
            return 1.0 / hz
        hz_auto = 1.0 / max(self._frame_period_s, 1e-3)
        hz_auto = min(hz_auto, float(self._p["fusion_emit_max_hz"]))
        hz_auto = max(hz_auto, float(self._p["fusion_emit_min_hz"]))
        return 1.0 / hz_auto

    def _emit_loop(self) -> None:
        # Steady-rate emitter (fusion ON only): pushes at the configured/auto
        # rate so the canvas refreshes smoothly, independent of frame arrival.
        # A few duplicate re-pushes keep the canvas alive between frames; then
        # it idles to avoid burning CPU on a static estimate.
        MAX_DUP = 4
        dup = 0
        while self._running:
            time.sleep(self._emit_interval())
            if not self.enabled:
                continue
            with self._lock:
                seq = self._update_seq
            if seq != self._last_emitted_seq:
                self._last_emitted_seq = seq
                dup = 0
                self._emit_now()
            elif dup < MAX_DUP:
                dup += 1
                self._emit_now()

    def _emit_now(self) -> None:
        if self._emit_cb is None:
            return
        with self._lock:
            if self._estimate is None:
                return
            frame = self._estimate.copy()
            dark = self._dark
            itime = self._integration_us
        self._emit_cb(frame, dark, itime)

    # ── Internal ──────────────────────────────────────────────────────────

    def _reset_if_size_changed(self, n: int) -> None:
        """Drop state on a pixel-count change (device switch)."""
        if n != self._n_pixels:
            self._n_pixels = n
            self._estimate = None
