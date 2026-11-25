"""
calibration_linearity.py — Detector linearity calibration per unit (Qt-free).

Why: a spectral profile (S or K) is a fixed per-pixel factor measured at ONE
signal level. If the detector response is not proportional to the light
(LR-2T: ≈ +1.5 % between 39 % and 78 % FS), every measurement at another
level is biased, and the t/2 cross check of the spectral calibration fails.

Measurement (LinearityWorkflow)
  * stable broadband source, 6–10 integration times chosen so the raw peak
    sits at ≈ 10 % … 90 % of full scale (below the saturation level),
  * a dark capture at every integration time BEFORE and AFTER the light
    steps (time-interpolated per step, calibration_capture.interpolate_dark),
  * a REFERENCE step (≈ 50 % FS) repeated at the start, after every few steps
    and at the end; its count rate tracks the lamp drift, which is divided
    out of every step (linear interpolation in time),
  * all frames are taken with the existing linearity correction BYPASSED
    (raw driver output).

Model
  For pixel p and step k with net counts N = light − dark:
      N / (t · L(τ)) = R_p · h(N),     h(N) = exp(Σ_j d_j (N/FS)^j),  h(0) = 1
  R_p is the pixel's linear count rate (nuisance, eliminated per pixel), L the
  lamp factor. The correction is
      counts_true = f(counts_measured) = N / h(N)
  i.e. the response at LOW signal is the linear reference (corrected =
  measured for weak signals). f is stored as a polynomial (d_j) AND as a
  monotonic support-point table over the measured range; beyond the range the
  end ratio h(N_max) is kept constant.

Application order (engine, BaseSpectrometer.set_linearizer)
  The nonlinearity is a property of each single readout (charge-to-voltage
  conversion / ADC) and acts on the accumulated signal (photo charge + dark
  current) ABOVE the electronic offset. Therefore it is corrected
    1. per FRAME, directly after the driver — before fusion, frame averaging
       and fast-preview scaling (all of these assume linear counts) and before
       the calibration / measurement frame taps,
    2. on the signal above the frame's baseline b (the driver's dark scalar:
       optical black / darkest-pixel estimate):  b + f(raw − b)  — the
       electronic offset itself is not subject to the detector nonlinearity,
    3. before the dark subtraction; the dark frames pass through the same
       correction (f(x) ≈ x for small x), so light − dark stays consistent.
  Fast-preview frames scaled by `ratio` are corrected at their true level:
  b + ratio · f((raw − b)/ratio).
  Devices with a factory linearisation (Ocean HDX, nl_correction_active True)
  only get a CHECK measurement; a stored correction is never applied on top of
  an active factory correction (no double correction).

Storage: linearity_profiles.json in the user data directory (data_paths),
one list of records; the active correction per unit (backend + serial).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

import numpy as np

import calibration_capture as cc

DEFAULT_FILE = "linearity_profiles.json"
SCHEMA = 1
MODE_CORRECTION = "correction"
MODE_CHECK = "check"

STATE_NONE = "none"            # no correction stored / selected
STATE_ACTIVE = "active"        # correction applied
STATE_FACTORY = "factory"      # device linearises itself (HDX) → not applied
STATE_OFF = "off"              # stored but switched off by the user


# ══════════════════════════════════════════════════════════════════════════
#  Configuration
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class LinearityConfig:
    n_steps: int = 8                  # 6–10 integration times
    frac_lo: float = 0.10             # raw peak of the shortest step (fraction of FS)
    frac_hi: float = 0.90             # raw peak of the longest step (capped below saturation)
    ref_frac: float = 0.50            # level of the repeated reference step
    ref_every: int = 3                # reference step after every N steps (+ start, end)
    n_frames: int = 16
    degree: int = 3                   # polynomial degree of ln h(N/FS)
    snr_k: float = 20.0               # pixels used: net > snr_k · sigma
    min_signal_frac: float = 0.02     # … and net > this fraction of FS
    min_points_per_pixel: int = 3
    n_knots: int = 65
    warn_residual_pct: float = 0.3    # rms residual after the fit above this → warning
    warn_lamp_drift_pct: float = 1.0
    settle_s: float = 0.0             # optional wait before each light step

    @classmethod
    def from_config(cls) -> "LinearityConfig":
        d = cls()
        try:
            from app_config import Config as C
        except Exception:                                   # noqa: BLE001
            return d

        def num(key, default, typ=float):
            try:
                v = typ(C.get(key, default))
                return v if np.isfinite(v) else default
            except (TypeError, ValueError):
                return default
        return cls(
            n_steps=int(min(10, max(6, num("linearity_steps", d.n_steps, int)))),
            frac_lo=num("linearity_frac_lo", d.frac_lo),
            frac_hi=num("linearity_frac_hi", d.frac_hi),
            ref_frac=num("linearity_ref_frac", d.ref_frac),
            ref_every=max(1, num("linearity_ref_every", d.ref_every, int)),
            n_frames=max(2, num("linearity_capture_frames",
                                num("calibration_capture_frames", d.n_frames, int), int)),
            degree=int(min(5, max(1, num("linearity_degree", d.degree, int)))),
            warn_residual_pct=num("linearity_warn_residual_pct", d.warn_residual_pct),
            warn_lamp_drift_pct=num("linearity_warn_lamp_drift_pct", d.warn_lamp_drift_pct),
        )


# ══════════════════════════════════════════════════════════════════════════
#  Model
# ══════════════════════════════════════════════════════════════════════════

def _poly_ln_h(coeffs, u):
    """Σ_j d_j u^j (j = 1..deg), u = N / FS."""
    u = np.asarray(u, float)
    out = np.zeros_like(u)
    for j, d in enumerate(coeffs, start=1):
        out = out + float(d) * u ** j
    return out


def model_h(model: dict, x) -> np.ndarray:
    """Relative response h(N) of the model (1 = linear) for measured counts x."""
    x = np.asarray(x, float)
    fs = float(model["full_scale"])
    lo, hi = model.get("valid_range_counts", [0.0, fs])
    xc = np.clip(x, 0.0, float(hi))
    return np.exp(_poly_ln_h(model["coeffs"], xc / fs))


def correct_counts(model: dict, x) -> np.ndarray:
    """counts_true = f(counts_measured) on net counts (above the baseline).

    Support-point table inside the measured range (monotonic by
    construction/check), constant end ratio beyond it, identity for x <= 0."""
    x = np.asarray(x, float)
    kx = np.asarray(model["knots_x"], float)
    kf = np.asarray(model["knots_f"], float)
    out = np.array(x, float, copy=True)
    pos = x > 0
    if not np.any(pos) or kx.size < 2:
        return out
    hi = kx[-1]
    inside = pos & (x <= hi)
    out[inside] = np.interp(x[inside], kx, kf)
    above = pos & (x > hi)
    if np.any(above):
        out[above] = x[above] * (kf[-1] / hi if hi > 0 else 1.0)
    return out


def make_linearizer(model: dict):
    """Frame-level correction for BaseSpectrometer.set_linearizer:
    fn(pixels, dark, ratio) → b + ratio · f((pixels − b)/ratio), b = the
    frame's baseline scalar (driver dark estimate)."""
    def fn(pixels, dark=0.0, ratio=1.0):
        px = np.asarray(pixels, float)
        try:
            b = float(np.mean(dark))
        except (TypeError, ValueError):
            b = 0.0
        r = float(ratio) if ratio and float(ratio) > 0 else 1.0
        x = (px - b) / r
        return b + r * correct_counts(model, x)
    fn.model = model
    return fn


def _monotonic(f_vals) -> bool:
    d = np.diff(np.asarray(f_vals, float))
    return bool(np.all(d > 0))


def fit_linearity(steps: list[dict], *, full_scale: float, degree: int = 3,
                  snr_k: float = 20.0, min_signal_frac: float = 0.02,
                  min_points: int = 3, n_knots: int = 65) -> dict:
    """Fit h(N) from the light steps.

    steps: [{"t_us", "net" (counts, per pixel), "sigma" (counts, per pixel),
             "lamp" (factor, 1 = start), "saturated" (bool per pixel)}, …]
    Returns the model dict (coeffs, knots, valid range, residuals, deviation
    curve). Degree is reduced until f is monotonic over the range."""
    fs = float(full_scale)
    P, Y, U, W = [], [], [], []
    for s in steps:
        net = np.asarray(s["net"], float)
        sig = np.maximum(np.asarray(s["sigma"], float), 1e-9)
        sat = np.asarray(s.get("saturated", np.zeros(net.size, bool)), bool)
        t = max(float(s["t_us"]), 1.0) / 1e6
        lamp = float(s.get("lamp", 1.0)) or 1.0
        ok = (np.isfinite(net) & (net > snr_k * sig) & (net > min_signal_frac * fs) & ~sat)
        idx = np.flatnonzero(ok)
        P.append(idx)
        Y.append(np.log(net[idx] / (t * lamp)))
        U.append(net[idx] / fs)
        W.append((net[idx] / sig[idx]) ** 2)
    if not P:
        raise ValueError("No linearity steps.")
    P = np.concatenate(P)
    Y = np.concatenate(Y)
    U = np.concatenate(U)
    W = np.concatenate(W)
    # pixels with enough points
    cnt = np.bincount(P, minlength=int(P.max()) + 1 if P.size else 1)
    use = cnt[P] >= int(min_points)
    P, Y, U, W = P[use], Y[use], U[use], W[use]
    if P.size < 10 or np.unique(P).size < 3:
        raise ValueError("Too few usable pixels for the linearity fit (signal too low "
                         "or saturated in most steps).")
    npx = int(P.max()) + 1
    sw = np.bincount(P, weights=W, minlength=npx)

    def demean(v):
        mean = np.bincount(P, weights=W * v, minlength=npx) / np.where(sw > 0, sw, 1)
        return v - mean[P]

    Yd = demean(Y)
    rms_before = float(np.sqrt(np.sum(W * Yd ** 2) / np.sum(W))) * 100.0
    x_lo = float(np.percentile(U, 0.5)) * fs
    x_hi = float(np.max(U)) * fs
    grid = np.linspace(0.0, x_hi, int(n_knots))
    chosen = None
    for deg in range(int(degree), 0, -1):
        X = np.column_stack([demean(U ** j) for j in range(1, deg + 1)])
        Xw = X * W[:, None]
        try:
            d = np.linalg.solve(X.T @ Xw, Xw.T @ Yd)
        except np.linalg.LinAlgError:
            continue
        res = Yd - X @ d
        ln_h = _poly_ln_h(d, grid / fs)
        f = grid / np.exp(ln_h)
        if _monotonic(f[1:]) and f[1] > 0:
            rms_after = float(np.sqrt(np.sum(W * res ** 2) / np.sum(W))) * 100.0
            chosen = (deg, d, f, ln_h, rms_after)
            break
    if chosen is None:
        raise ValueError("Linearity fit is not monotonic for any degree.")
    deg, d, f, ln_h, rms_after = chosen
    h = np.exp(ln_h)
    inside = grid >= x_lo
    dev = 100.0 * (h - 1.0)
    return {
        "type": "poly_ln_h",
        "degree": int(deg),
        "coeffs": [float(v) for v in d],
        "full_scale": fs,
        "knots_x": [float(v) for v in grid],
        "knots_f": [float(v) for v in f],
        "valid_range_counts": [x_lo, x_hi],
        "valid_range_frac": [x_lo / fs, x_hi / fs],
        "rms_before_pct": rms_before,
        "rms_after_pct": rms_after,
        "max_deviation_pct": float(np.max(np.abs(dev[inside]))) if np.any(inside) else 0.0,
        "deviation_curve": [[float(x), float(v)] for x, v in zip(grid, dev)],
        "n_points": int(P.size),
        "n_pixels": int(np.unique(P).size),
        "monotonic": True,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Store (per unit: backend + serial)
# ══════════════════════════════════════════════════════════════════════════

def _path() -> str:
    try:
        from app_config import Config
        name = Config.get("linearity_profiles_file", DEFAULT_FILE)
    except Exception:                                       # noqa: BLE001
        name = DEFAULT_FILE
    try:
        import data_paths
        return data_paths.resolve(name)
    except Exception:                                       # noqa: BLE001
        return name


def load_store() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {"schema": SCHEMA, "entries": [], "active": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception as e:                                  # noqa: BLE001
        print(f"[linearity] failed to read {p}: {e}")
        return {"schema": SCHEMA, "entries": [], "active": {}}
    d.setdefault("entries", [])
    d.setdefault("active", {})
    return d


def save_store(d: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_clean(d), f, indent=1, allow_nan=False)
    os.replace(tmp, p)


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if np.isfinite(v) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def device_key(backend: str, serial: str | None) -> str:
    return f"{backend or ''}|{serial or ''}"


def entries_for(backend: str, serial: str | None) -> list[dict]:
    """All records of this unit, newest first."""
    out = [e for e in load_store()["entries"]
           if e.get("backend") == backend and (e.get("serial") or None) == (serial or None)]
    out.sort(key=lambda e: str(e.get("created", "")), reverse=True)
    return out


def get_entry(eid: str) -> dict | None:
    for e in load_store()["entries"]:
        if e.get("id") == eid:
            return e
    return None


def active_id(backend: str, serial: str | None) -> str | None:
    return load_store()["active"].get(device_key(backend, serial)) or None


def set_active(backend: str, serial: str | None, eid: str | None) -> None:
    d = load_store()
    k = device_key(backend, serial)
    if eid is None:
        d["active"].pop(k, None)
    else:
        e = next((x for x in d["entries"] if x.get("id") == eid), None)
        if e is None:
            raise KeyError(eid)
        if e.get("mode") != MODE_CORRECTION:
            raise ValueError("A check measurement cannot be activated as a correction.")
        if e.get("backend") != backend or (e.get("serial") or None) != (serial or None):
            raise ValueError("The linearity correction belongs to another unit.")
        d["active"][k] = eid
    save_store(d)


def add_entry(entry: dict, *, activate: bool) -> dict:
    d = load_store()
    e = dict(entry)
    e.setdefault("id", "lin-" + uuid.uuid4().hex[:10])
    e.setdefault("created", cc.now_iso())
    d["entries"].append(_clean(e))
    if activate and e.get("mode") == MODE_CORRECTION:
        d["active"][device_key(e.get("backend"), e.get("serial"))] = e["id"]
    save_store(d)
    return e


def delete_entry(eid: str) -> bool:
    d = load_store()
    n = len(d["entries"])
    d["entries"] = [e for e in d["entries"] if e.get("id") != eid]
    for k, v in list(d["active"].items()):
        if v == eid:
            d["active"].pop(k)
    if len(d["entries"]) != n:
        save_store(d)
        return True
    return False


def entry_summary(e: dict | None) -> dict | None:
    """Compact JSON-able summary (no arrays)."""
    if not e:
        return None
    m = e.get("model") or {}
    return {"id": e.get("id"), "created": e.get("created"), "mode": e.get("mode"),
            "backend": e.get("backend"), "serial": e.get("serial"),
            "valid_range_counts": m.get("valid_range_counts"),
            "valid_range_frac": m.get("valid_range_frac"),
            "max_deviation_pct": m.get("max_deviation_pct"),
            "rms_before_pct": m.get("rms_before_pct"),
            "rms_after_pct": m.get("rms_after_pct"),
            "degree": m.get("degree"), "n_steps": len(e.get("steps") or [])}


# ══════════════════════════════════════════════════════════════════════════
#  Runtime (what the engine installs on the backend)
# ══════════════════════════════════════════════════════════════════════════

def runtime_for(backend, backend_name: str) -> tuple:
    """(linearizer | None, info) for the connected unit.

    info = {"state": none|active|factory|off, "id", "created",
            "valid_range_counts", "factory_nl": bool|None, "label"}"""
    serial = getattr(backend, "serial", None) if backend is not None else None
    try:
        factory = backend.nl_correction_active if backend is not None else None
    except Exception:                                       # noqa: BLE001
        factory = None
    factory = factory if isinstance(factory, bool) else None
    info = {"state": STATE_NONE, "id": None, "created": None,
            "valid_range_counts": None, "factory_nl": factory, "label": "n/a"}
    if factory is True:
        info.update(state=STATE_FACTORY, label="factory linearisation (device)")
        return None, info
    eid = active_id(backend_name, serial)
    if not eid:
        info["label"] = "off"
        if entries_for(backend_name, serial):
            info["state"] = STATE_OFF
        return None, info
    e = get_entry(eid)
    if e is None or not e.get("model"):
        info["label"] = "off"
        return None, info
    info.update(state=STATE_ACTIVE, id=e["id"], created=e.get("created"),
                valid_range_counts=(e["model"] or {}).get("valid_range_counts"),
                label=f"on ({e['id']}, {str(e.get('created', ''))[:10]})")
    return make_linearizer(e["model"]), info


def profile_linearity_tag(info: dict | None) -> dict:
    """What a calibration profile stores about the linearity correction that
    was active when it was made."""
    info = info or {}
    st = info.get("state", STATE_NONE)
    return {"active": st == STATE_ACTIVE, "id": info.get("id") if st == STATE_ACTIVE else None,
            "created": info.get("created") if st == STATE_ACTIVE else None,
            "state": st}


def linearity_mismatch(profile_tag: dict | None, info: dict | None) -> str | None:
    """Hint text when the linearity correction active now differs from the one
    active when the profile was made (None = consistent / unknown)."""
    if not isinstance(profile_tag, dict):
        return None
    now = profile_linearity_tag(info)
    if bool(profile_tag.get("active")) == bool(now["active"]) and \
            (profile_tag.get("id") or None) == (now["id"] or None):
        return None
    was = (f"on ({profile_tag.get('id')})" if profile_tag.get("active") else "off")
    is_ = (f"on ({now['id']})" if now["active"] else "off")
    return (f"Linearity correction was {was} when this profile was made and is "
            f"{is_} now — recalibrate the profile for exact values.")


def onoff_na(info_or_flag) -> str:
    """'on' / 'off' / 'n/a' for protocols and exports."""
    if isinstance(info_or_flag, dict):
        st = info_or_flag.get("state")
        if st == STATE_ACTIVE or info_or_flag.get("active") is True:
            return "on"
        if st == STATE_FACTORY:
            return "n/a (factory)"
        if st in (STATE_NONE, STATE_OFF) or info_or_flag.get("active") is False:
            return "off"
        return "n/a"
    if info_or_flag is True:
        return "on"
    if info_or_flag is False:
        return "off"
    return "n/a"


# ══════════════════════════════════════════════════════════════════════════
#  Measurement workflow
# ══════════════════════════════════════════════════════════════════════════

def plan_integration_times(rate_peak_cps: float, baseline_counts: float, *,
                           full_scale: float, saturation_level: float,
                           bounds_us: tuple[int, int], cfg: LinearityConfig) -> tuple[list[int], int]:
    """Integration times whose raw peak lands at linspace(frac_lo, frac_hi)
    of full scale (capped at 95 % of the saturation level), plus the
    reference time (ref_frac). Duplicates after clamping are removed."""
    if not (rate_peak_cps > 0):
        raise ValueError("No signal from the source — check the light path.")
    cap = 0.95 * float(saturation_level)
    lo, hi = int(bounds_us[0]), int(bounds_us[1])

    def t_for(frac):
        target = min(frac * float(full_scale), cap) - float(baseline_counts)
        return int(np.clip(round(max(target, 1.0) / rate_peak_cps * 1e6), lo, hi))
    fr = np.linspace(cfg.frac_lo, cfg.frac_hi, int(cfg.n_steps))
    ts = []
    for f in fr:
        t = t_for(float(f))
        if not ts or t > ts[-1]:
            ts.append(t)
    return ts, t_for(cfg.ref_frac)


class LinearityWorkflow:
    """One linearity run on a live backend (blocking steps; GUI runs them in a
    worker thread and shows cover/uncover prompts via needs_user_cover).

    Steps: plan() → capture_darks("before") → capture_steps() →
           capture_darks("after") → compute() → save()"""

    def __init__(self, backend, backend_name: str, cfg: LinearityConfig | None = None):
        self.backend = backend
        self.backend_name = backend_name
        self.cfg = cfg or LinearityConfig.from_config()
        try:
            factory = backend.nl_correction_active
        except Exception:                                   # noqa: BLE001
            factory = None
        self.factory_nl = factory if isinstance(factory, bool) else None
        self.mode = MODE_CHECK if self.factory_nl is True else MODE_CORRECTION
        self.times_us: list[int] = []
        self.ref_us: int | None = None
        self.sequence: list[tuple[str, int]] = []      # ("ref"|"step", t_us)
        self.darks_before: dict[int, cc.Capture] = {}
        self.darks_after: dict[int, cc.Capture] = {}
        self.lights: list[dict] = []                    # {"role", "t_us", "capture"}
        self.model: dict | None = None
        self.steps_table: list[dict] = []
        self.lamp: dict | None = None
        self.drift: list[dict] = []
        self.warnings: list[str] = []
        self.saved: dict | None = None
        self.started = cc.now_iso()
        self.probe: dict | None = None

    @property
    def needs_user_cover(self) -> bool:
        return not getattr(self.backend, "SUPPORTS_SHUTTER", False)

    @property
    def all_times(self) -> list[int]:
        return sorted(set(self.times_us + ([self.ref_us] if self.ref_us else [])))

    def plan(self, integration_us: int | None = None) -> dict:
        """Probe the source (auto exposure to ≈ 50 % FS, raw frames) and plan
        the integration times and the measurement order."""
        b = self.backend
        prev = getattr(b, "linearity_bypass", False)
        b.linearity_bypass = True
        try:
            if integration_us is None:
                t, cap, _ok = cc.auto_expose(b, target_frac=(0.40, 0.55),
                                             backend_name=self.backend_name)
            else:
                b.set_integration_time_us(int(integration_us))
                t = int(b.current_integration_time_us)
                cap = cc.capture(b, 2, integration_us=t, backend_name=self.backend_name,
                                 raw=True)
        finally:
            b.linearity_bypass = prev
        base = float(np.percentile(cap.mean, 2))
        rate_peak = max(cap.peak_counts - base, 1.0) / (t / 1e6)
        bounds = (int(getattr(b, "min_integration_us", 1)),
                  int(getattr(b, "max_integration_us", 10_000_000)))
        self.times_us, self.ref_us = plan_integration_times(
            rate_peak, base, full_scale=cap.full_scale,
            saturation_level=cap.saturation_level, bounds_us=bounds, cfg=self.cfg)
        if len(self.times_us) < 4:
            self.warnings.append(f"Only {len(self.times_us)} distinct integration times "
                                 f"possible (device limits) — the fit is weak.")
        seq: list[tuple[str, int]] = [("ref", self.ref_us)]
        for i, tt in enumerate(self.times_us, 1):
            seq.append(("step", tt))
            if i % self.cfg.ref_every == 0 and i < len(self.times_us):
                seq.append(("ref", self.ref_us))
        seq.append(("ref", self.ref_us))
        self.sequence = seq
        self.probe = {"integration_us": int(t), "peak_counts": float(cap.peak_counts),
                      "baseline_counts": base, "rate_peak_cps": float(rate_peak),
                      "full_scale": float(cap.full_scale),
                      "saturation_level": float(cap.saturation_level)}
        b.set_integration_time_us(int(t))
        return {"times_us": list(self.times_us), "ref_us": self.ref_us,
                "sequence": [list(x) for x in seq], "probe": dict(self.probe),
                "mode": self.mode}

    def capture_darks(self, phase: str = "before", progress=None) -> dict:
        """Dark at every planned integration time (raw frames). The light path
        must be blocked (shutter used automatically if present)."""
        if not self.times_us:
            raise RuntimeError("plan the integration times first")
        store = self.darks_before if phase == "before" else self.darks_after
        ts = self.all_times
        for i, t in enumerate(ts):
            if progress:
                progress(i / max(len(ts), 1), f"dark {phase} {t/1000:.3f} ms")
            store[t] = cc.capture_dark(self.backend, self.cfg.n_frames, integration_us=t,
                                       backend_name=self.backend_name, raw=True)
        if phase == "after":
            self.drift = [cc.dark_drift(self.darks_before[t], self.darks_after[t])
                          for t in ts if t in self.darks_before]
        return {"phase": phase, "n": len(ts),
                "mean_counts": {int(t): float(np.mean(store[t].mean)) for t in ts}}

    def capture_steps(self, *, sleep=time.sleep, progress=None, check=None) -> dict:
        """The light steps in the planned order (reference interleaved)."""
        if not self.darks_before:
            raise RuntimeError("capture the dark spectra first")
        self.lights = []
        n = len(self.sequence)
        for i, (role, t) in enumerate(self.sequence):
            if check:
                check()
            if progress:
                progress(i / max(n, 1), f"{role} {t/1000:.3f} ms ({i + 1}/{n})")
            if self.cfg.settle_s > 0:
                sleep(self.cfg.settle_s)
            cap = cc.capture(self.backend, self.cfg.n_frames, integration_us=t,
                             backend_name=self.backend_name, raw=True)
            if cap.n_saturated and role == "ref":
                raise RuntimeError("The reference step saturates — reduce the source "
                                   "intensity and repeat.")
            self.lights.append({"role": role, "t_us": int(cap.integration_us), "capture": cap})
        return {"n": len(self.lights)}

    def compute(self) -> dict:
        """Dark interpolation, lamp drift, fit; fills model, steps_table."""
        if not self.lights:
            raise RuntimeError("capture the steps first")
        fs = float(self.lights[0]["capture"].full_scale)
        nets = []
        for L in self.lights:
            cap = L["capture"]
            t = L["t_us"]
            before = self.darks_before.get(t) or self.darks_before[min(
                self.darks_before, key=lambda k: abs(k - t))]
            after = self.darks_after.get(t)
            dk = cc.interpolate_dark(before, after, cap.t_mid)
            rate = cc.count_rate(cap, dk)
            nets.append({"role": L["role"], "t_us": t, "net": rate.rate * t / 1e6,
                         "sigma": rate.sigma_rate * t / 1e6, "saturated": cap.saturated,
                         "t_mid": cap.t_mid, "peak_frac": cap.peak_frac,
                         "peak_counts": cap.peak_counts})
        # lamp drift from the reference steps
        refs = [s for s in nets if s["role"] == "ref"]
        good = np.ones(nets[0]["net"].size, bool)
        for s in refs:
            good &= (s["net"] > self.cfg.snr_k * np.maximum(s["sigma"], 1e-9)) & ~s["saturated"]
        if np.count_nonzero(good) < 10:
            good = refs[0]["net"] > 0.1 * np.max(refs[0]["net"])
        lev = np.array([float(np.sum(s["net"][good])) for s in refs])
        tr = np.array([s["t_mid"] or i for i, s in enumerate(refs)], float)
        rel = lev / lev[0] if lev[0] else np.ones_like(lev)
        for s in nets:
            tm = s["t_mid"] if s["t_mid"] is not None else tr[0]
            s["lamp"] = float(np.interp(tm, tr, rel)) if tr.size > 1 else 1.0
        lamp_drift = float(100.0 * (np.max(rel) - np.min(rel))) if rel.size else 0.0
        self.lamp = {"ref_us": self.ref_us, "levels_rel": [float(v) for v in rel],
                     "drift_pct": lamp_drift}
        if lamp_drift > self.cfg.warn_lamp_drift_pct:
            self.warnings.append(f"Lamp drift {lamp_drift:.2f} % during the run "
                                 f"(corrected via the reference step; let the lamp warm "
                                 f"up longer for a better result).")
        for d in self.drift:
            if abs(d["mean_counts"]) > max(3 * d["sigma_counts"], 0.002 * fs):
                self.warnings.append(
                    f"Dark level changed by {d['mean_counts']:+.1f} counts at "
                    f"{d['integration_us']/1000:.3f} ms between the darks before and "
                    f"after (time-interpolated).")
        self.model = fit_linearity(
            [{"t_us": s["t_us"], "net": s["net"], "sigma": s["sigma"], "lamp": s["lamp"],
              "saturated": s["saturated"]} for s in nets],
            full_scale=fs, degree=self.cfg.degree, snr_k=self.cfg.snr_k,
            min_signal_frac=self.cfg.min_signal_frac,
            min_points=self.cfg.min_points_per_pixel, n_knots=self.cfg.n_knots)
        if self.model["rms_after_pct"] > self.cfg.warn_residual_pct:
            self.warnings.append(f"Residual after the fit {self.model['rms_after_pct']:.2f} % "
                                 f"> {self.cfg.warn_residual_pct:.2f} % (source unstable or "
                                 f"spectrally changing?).")
        # per-step table: response of the peak pixel region before/after correction
        top = None
        for s in nets:
            if s["role"] == "ref":
                top = s["net"] > 0.5 * np.max(s["net"])
                break
        base_rate = None
        self.steps_table = []
        for s in nets:
            sel = top & ~s["saturated"] & (s["net"] > 0)
            if not np.any(sel):
                continue
            t = s["t_us"] / 1e6
            rr = float(np.sum(s["net"][sel]) / (t * s["lamp"]))
            cr = float(np.sum(correct_counts(self.model, s["net"][sel])) / (t * s["lamp"]))
            self.steps_table.append({"role": s["role"], "t_us": int(s["t_us"]),
                                     "peak_pct_fs": 100.0 * float(s["peak_frac"]),
                                     "lamp": s["lamp"], "rate_raw": rr, "rate_corr": cr})
        lows = [r for r in self.steps_table if r["role"] == "step"]
        if lows:
            ref_raw = lows[0]["rate_raw"]
            ref_cor = lows[0]["rate_corr"]
            for r in self.steps_table:
                r["dev_raw_pct"] = 100.0 * (r["rate_raw"] / ref_raw - 1.0) if ref_raw else None
                r["dev_corr_pct"] = 100.0 * (r["rate_corr"] / ref_cor - 1.0) if ref_cor else None
        return {"model": {k: v for k, v in self.model.items()
                          if k not in ("knots_x", "knots_f", "deviation_curve")},
                "mode": self.mode, "lamp": self.lamp, "drift": self.drift,
                "warnings": list(self.warnings)}

    def save(self, *, activate: bool = True) -> dict:
        if self.model is None:
            raise RuntimeError("compute first")
        entry = {
            "schema": SCHEMA, "mode": self.mode,
            "backend": self.backend_name,
            "serial": getattr(self.backend, "serial", None),
            "factory_nl": self.factory_nl,
            "model": self.model,
            "steps": self.steps_table,
            "integration_us": list(self.times_us), "ref_us": self.ref_us,
            "lamp": self.lamp, "dark_drift": self.drift,
            "n_frames": self.cfg.n_frames, "warnings": list(self.warnings),
            "started": self.started,
        }
        self.saved = add_entry(entry, activate=activate and self.mode == MODE_CORRECTION)
        return self.saved

    def record(self) -> dict:
        """JSON-able run record for the protocol (PDF/CSV)."""
        m = self.model or {}
        return {
            "kind": "linearity_run",
            "started": self.started, "finished": cc.now_iso(),
            "mode": self.mode,
            "device": {"backend": self.backend_name,
                       "serial": getattr(self.backend, "serial", None)},
            "factory_nl": onoff_na(self.factory_nl),
            "settings": asdict(self.cfg),
            "integration_us": list(self.times_us), "ref_us": self.ref_us,
            "probe": self.probe,
            "steps": list(self.steps_table),
            "lamp": self.lamp, "dark_drift": list(self.drift),
            "model": {k: v for k, v in m.items() if k not in ("knots_x", "knots_f")},
            "curve": {"x_counts": m.get("knots_x"), "f_counts": m.get("knots_f"),
                      "deviation_pct": [v for _x, v in (m.get("deviation_curve") or [])]},
            "warnings": list(self.warnings),
            "saved": None if not self.saved else {"id": self.saved.get("id"),
                                                  "mode": self.saved.get("mode"),
                                                  "created": self.saved.get("created")},
            "activated": bool(self.saved and self.mode == MODE_CORRECTION
                              and active_id(self.backend_name,
                                            getattr(self.backend, "serial", None))
                              == self.saved.get("id")),
        }

    def run_all(self, *, sleep=time.sleep, save: bool = True,
                activate: bool = True) -> dict:
        """Headless full run (shutter devices / tests)."""
        self.plan()
        self.capture_darks("before")
        self.capture_steps(sleep=sleep)
        self.capture_darks("after")
        self.compute()
        if save:
            self.save(activate=activate)
        return self.record()
