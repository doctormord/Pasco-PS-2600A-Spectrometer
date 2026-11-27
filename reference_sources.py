"""Reference sources (calibrated lamps, characterised spheres): parsers,
library, interpolation.

Device independent; no Qt. A reference source is data: wavelength grid plus
spectral values with measurement quantity, unit, validity range and intended
input optic.

Measurement quantities (own property of sources and profiles):
    irradiance  E(λ)  W/(m²·nm), mW/(m²·nm), µW/(cm²·nm)
    radiance    L(λ)  W/(m²·sr·nm), mW/(m²·sr·nm), µW/(cm²·sr·nm)
Units convert only within the same quantity; 'relative' only to 'relative'.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

UNIT_UW_CM2_NM = "uW/cm2/nm"
UNIT_W_M2_NM = "W/m2/nm"
UNIT_MW_M2_NM = "mW/m2/nm"
UNIT_UW_CM2_SR_NM = "uW/cm2/sr/nm"
UNIT_W_M2_SR_NM = "W/m2/sr/nm"
UNIT_MW_M2_SR_NM = "mW/m2/sr/nm"
UNIT_RELATIVE = "relative"

QUANTITY_IRRADIANCE = "irradiance"
QUANTITY_RADIANCE = "radiance"
QUANTITIES = (QUANTITY_IRRADIANCE, QUANTITY_RADIANCE)

IRRADIANCE_UNITS = (UNIT_UW_CM2_NM, UNIT_W_M2_NM, UNIT_MW_M2_NM)
RADIANCE_UNITS = (UNIT_UW_CM2_SR_NM, UNIT_W_M2_SR_NM, UNIT_MW_M2_SR_NM)
KNOWN_UNITS = IRRADIANCE_UNITS + RADIANCE_UNITS + (UNIT_RELATIVE,)

# default (display / storage) unit per quantity
DEFAULT_UNIT = {QUANTITY_IRRADIANCE: UNIT_UW_CM2_NM, QUANTITY_RADIANCE: UNIT_UW_CM2_SR_NM}
# SI unit per quantity (photometry)
SI_UNIT = {QUANTITY_IRRADIANCE: UNIT_W_M2_NM, QUANTITY_RADIANCE: UNIT_W_M2_SR_NM}
# human-readable unit labels
UNIT_LABELS = {UNIT_UW_CM2_NM: "µW/(cm²·nm)", UNIT_W_M2_NM: "W/(m²·nm)",
               UNIT_MW_M2_NM: "mW/(m²·nm)", UNIT_UW_CM2_SR_NM: "µW/(cm²·sr·nm)",
               UNIT_W_M2_SR_NM: "W/(m²·sr·nm)", UNIT_MW_M2_SR_NM: "mW/(m²·sr·nm)",
               UNIT_RELATIVE: "relative"}

# Input optics. CC = cosine corrector, sphere = integrating sphere as collector,
# bare = bare fibre, objective = microscope objective / imaging optic.
INPUT_OPTICS = ("CC", "bare", "sphere", "objective")

# factor to the SI unit of the quantity: W/(m²·nm) resp. W/(m²·sr·nm)
_TO_SI = {UNIT_UW_CM2_NM: 0.01, UNIT_W_M2_NM: 1.0, UNIT_MW_M2_NM: 1e-3,
          UNIT_UW_CM2_SR_NM: 0.01, UNIT_W_M2_SR_NM: 1.0, UNIT_MW_M2_SR_NM: 1e-3}
# backward-compatible name (irradiance units only)
_TO_W_M2_NM = {u: _TO_SI[u] for u in IRRADIANCE_UNITS}


def quantity_of_unit(unit: str) -> str | None:
    """'irradiance' / 'radiance' for an absolute unit, None for 'relative' or
    an unknown unit."""
    if unit in IRRADIANCE_UNITS:
        return QUANTITY_IRRADIANCE
    if unit in RADIANCE_UNITS:
        return QUANTITY_RADIANCE
    return None


def is_absolute_unit(unit: str) -> bool:
    return quantity_of_unit(unit) is not None


def unit_label(unit: str) -> str:
    return UNIT_LABELS.get(unit, unit or "")


def to_si_factor(unit: str) -> float:
    """Factor from an absolute unit to the SI unit of its quantity."""
    if unit not in _TO_SI:
        raise ValueError(f"'{unit}' is not an absolute unit")
    return _TO_SI[unit]


def convert_units(values, from_unit: str, to_unit: str) -> np.ndarray:
    """Convert spectral values between units of the SAME quantity
    (irradiance <-> irradiance, radiance <-> radiance). 'relative' only to
    'relative'; irradiance <-> radiance is refused (needs a geometry)."""
    for u in (from_unit, to_unit):
        if u not in KNOWN_UNITS:
            raise ValueError(f"Unknown unit '{u}' (known: {', '.join(KNOWN_UNITS)})")
    arr = np.asarray(values, dtype=float)
    if from_unit == to_unit:
        return arr.copy()
    if UNIT_RELATIVE in (from_unit, to_unit):
        raise ValueError(f"Cannot convert between '{from_unit}' and '{to_unit}': "
                         "relative values have no absolute scale")
    qf, qt = quantity_of_unit(from_unit), quantity_of_unit(to_unit)
    if qf != qt:
        raise ValueError(f"Cannot convert {qf} ('{from_unit}') to {qt} ('{to_unit}'): "
                         "different measurement quantities")
    return arr * (_TO_SI[from_unit] / _TO_SI[to_unit])

DEMO_SOURCE_ID = "demo-cal-3000k"
# Demo backend: the simulated spectrum that physically matches each built-in
# demo reference source (the calibration wizard switches to it for the run).
DEMO_SPECTRUM_FOR_SOURCE = {DEMO_SOURCE_ID: "cal_lamp"}


def convert_irradiance(values, from_unit: str, to_unit: str) -> np.ndarray:
    """Backward-compatible name of convert_units (same-quantity conversion)."""
    return convert_units(values, from_unit, to_unit)


# ---------------------------------------------------------------- parsing

def _read_text(source) -> str:
    s = os.fspath(source) if isinstance(source, os.PathLike) else source
    if not isinstance(s, str):
        raise ValueError("source must be a path or text")
    if "\n" in s or "\r" in s:
        return s.lstrip("﻿")
    if os.path.isfile(s):
        with open(s, "rb") as f:
            raw = f.read()
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    raise ValueError(f"File not found (and not multi-line text): {s[:80]!r}")


def _to_float(tok: str, allow_comma: bool):
    tok = tok.strip()
    if not tok:
        return None
    if allow_comma:
        tok = tok.replace(",", ".")
    try:
        v = float(tok)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _finish(pairs, what: str) -> tuple[np.ndarray, np.ndarray]:
    if len(pairs) < 2:
        raise ValueError(f"{what}: fewer than 2 numeric data points found")
    arr = np.array(pairs, dtype=float)
    order = np.argsort(arr[:, 0], kind="stable")
    wl, val = arr[order, 0], arr[order, 1]
    if np.any(np.diff(wl) == 0):
        dup = wl[:-1][np.diff(wl) == 0][0]
        raise ValueError(f"{what}: duplicate wavelength {dup:g} nm")
    return wl, val


def parse_lmp(source) -> tuple[np.ndarray, np.ndarray]:
    """Avantes .lmp (path or text): whitespace separated, decimal comma or point,
    optional leading index column. Non-numeric lines are skipped."""
    text = _read_text(source)
    pairs = []
    for line in text.splitlines():
        toks = line.split()
        nums = [_to_float(t, True) for t in toks]
        if not nums or any(n is None for n in nums):
            continue
        if len(nums) == 2:
            pairs.append((nums[0], nums[1]))
        elif len(nums) == 3:
            pairs.append((nums[1], nums[2]))
    return _finish(pairs, "LMP")


def _detect_delimiter(lines) -> str:
    for d in (";", "\t"):
        if any(d in ln for ln in lines):
            return d
    return ","


def parse_csv(source) -> tuple[np.ndarray, np.ndarray]:
    """CSV/TSV (path or text). Delimiter ; , or tab auto-detected; decimal comma
    only if the delimiter is not ','. First two numeric columns = wl, value."""
    text = _read_text(source)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    delim = _detect_delimiter(lines)
    allow_comma = delim != ","
    pairs = []
    for ln in lines:
        nums = [v for v in (_to_float(t, allow_comma) for t in ln.split(delim))
                if v is not None]
        if len(nums) >= 2:
            pairs.append((nums[0], nums[1]))
    return _finish(pairs, "CSV")


def parse_reference_file(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Parse by extension (.lmp -> LMP, else CSV); fall back to the other parser."""
    if not os.path.isfile(path):
        raise ValueError(f"File not found: {path}")
    is_lmp = os.path.splitext(path)[1].lower() == ".lmp"
    first, second = (parse_lmp, parse_csv) if is_lmp else (parse_csv, parse_lmp)
    try:
        return first(path)
    except ValueError as e1:
        try:
            return second(path)
        except ValueError:
            raise e1


# ---------------------------------------------------------- interpolation

def interpolate_to_axis(ref_wl, ref_values, axis_nm, lo_nm=None, hi_nm=None) -> np.ndarray:
    """PCHIP onto axis_nm. NaN outside [max(lo, wl.min), min(hi, wl.max)]; no extrapolation."""
    from scipy.interpolate import PchipInterpolator
    wl = np.asarray(ref_wl, dtype=float)
    val = np.asarray(ref_values, dtype=float)
    if wl.shape != val.shape or wl.ndim != 1 or wl.size < 2:
        raise ValueError("reference needs matching 1-D wl/values with >= 2 points")
    order = np.argsort(wl)
    wl, val = wl[order], val[order]
    axis = np.asarray(axis_nm, dtype=float)
    lo = wl[0] if lo_nm is None else max(float(lo_nm), wl[0])
    hi = wl[-1] if hi_nm is None else min(float(hi_nm), wl[-1])
    out = np.full(axis.shape, np.nan, dtype=float)
    mask = (axis >= lo) & (axis <= hi)
    if mask.any():
        out[mask] = PchipInterpolator(wl, val, extrapolate=False)(axis[mask])
    return out


# ------------------------------------------------------------- data model

def _slug(*parts: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", " ".join(p for p in parts if p).lower()).strip("-")
    return s or "source"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ReferenceSource:
    id: str
    name: str
    serial: str
    unit: str
    valid_min_nm: float
    valid_max_nm: float
    input_optic: str
    source_file: str
    imported: str
    wl: np.ndarray
    values: np.ndarray
    notes: str = ""
    builtin: bool = False
    quantity: str = QUANTITY_IRRADIANCE
    # free metadata (e.g. sphere characterisation: geometry, date, device,
    # CC profile id/version, uncertainty summary)
    meta: dict = field(default_factory=dict)
    # optional per-point standard uncertainty (same unit as values)
    uncertainty: np.ndarray | None = None

    def __post_init__(self):
        q = quantity_of_unit(self.unit)
        if not self.quantity:
            self.quantity = q or QUANTITY_IRRADIANCE
        if self.quantity not in QUANTITIES:
            raise ValueError(f"Reference '{self.id}': unknown quantity '{self.quantity}' "
                             f"(allowed: {', '.join(QUANTITIES)})")
        if q is not None and q != self.quantity:
            raise ValueError(f"Reference '{self.id}': unit '{self.unit}' is a {q} unit, "
                             f"but the quantity is '{self.quantity}'")

    def to_dict(self) -> dict:
        out = {
            "id": self.id, "name": self.name, "serial": self.serial,
            "unit": self.unit, "quantity": self.quantity,
            "valid_min_nm": float(self.valid_min_nm),
            "valid_max_nm": float(self.valid_max_nm),
            "input_optic": self.input_optic, "source_file": self.source_file,
            "imported": self.imported, "notes": self.notes, "builtin": bool(self.builtin),
            "wl": [float(x) for x in self.wl],
            "values": [float(x) for x in self.values],
        }
        if self.meta:
            out["meta"] = dict(self.meta)
        if self.uncertainty is not None:
            out["uncertainty"] = [float(x) for x in self.uncertainty]
        return out

    @classmethod
    def from_dict(cls, d) -> "ReferenceSource":
        try:
            src = cls(
                id=str(d["id"]), name=str(d["name"]), serial=str(d.get("serial", "")),
                unit=str(d["unit"]),
                valid_min_nm=float(d["valid_min_nm"]), valid_max_nm=float(d["valid_max_nm"]),
                input_optic=str(d.get("input_optic", "")),
                source_file=str(d.get("source_file", "")),
                imported=str(d.get("imported", "")),
                wl=np.asarray(d["wl"], dtype=float),
                values=np.asarray(d["values"], dtype=float),
                notes=str(d.get("notes", "")), builtin=bool(d.get("builtin", False)),
                # records written before the quantity existed: from the unit;
                # 'relative' sources default to irradiance
                quantity=str(d.get("quantity") or quantity_of_unit(str(d.get("unit", "")))
                             or QUANTITY_IRRADIANCE),
                meta=dict(d.get("meta") or {}),
                uncertainty=(None if d.get("uncertainty") is None
                             else np.asarray(d["uncertainty"], dtype=float)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Invalid reference source record: {e!r}") from e
        if src.unit not in KNOWN_UNITS:
            raise ValueError(f"Reference '{src.id}': unknown unit '{src.unit}'")
        if src.wl.shape != src.values.shape or src.wl.size < 2:
            raise ValueError(f"Reference '{src.id}': wl/values mismatch or < 2 points")
        if src.uncertainty is not None and src.uncertainty.shape != src.wl.shape:
            raise ValueError(f"Reference '{src.id}': uncertainty/wl mismatch")
        return src

    @property
    def is_absolute(self) -> bool:
        return self.unit != UNIT_RELATIVE

    @property
    def unit_text(self) -> str:
        return unit_label(self.unit)

    def on_axis(self, axis_nm, unit: str | None = None) -> np.ndarray:
        out = interpolate_to_axis(self.wl, self.values, axis_nm,
                                  self.valid_min_nm, self.valid_max_nm)
        if unit is not None and unit != self.unit:
            out = convert_units(out, self.unit, unit)
        return out

    @property
    def data_range_nm(self) -> tuple[float, float]:
        return float(np.min(self.wl)), float(np.max(self.wl))


def builtin_sources() -> list[ReferenceSource]:
    import calibration_utils
    wl = np.arange(300.0, 1100.0 + 0.5, 1.0)
    return [ReferenceSource(
        id=DEMO_SOURCE_ID, name="DEMO-CAL (synthetic 3000 K)", serial="DEMO-LAMP-1",
        unit=UNIT_UW_CM2_NM, valid_min_nm=350.0, valid_max_nm=1000.0,
        input_optic="CC", source_file="builtin", imported="",
        wl=wl, values=np.asarray(calibration_utils.demo_lamp_irradiance(wl), dtype=float),
        notes="Synthetic demo lamp, not a real certificate.", builtin=True)]


# ---------------------------------------------------------------- library

class ReferenceLibrary:
    def __init__(self, path: str | None = None):
        if path is None:
            from app_config import Config
            import data_paths
            path = data_paths.resolve(Config.get("reference_sources_file",
                                                 "reference_sources.json"))
        self.path = path
        self._builtin = {s.id: s for s in builtin_sources()}
        self._stored: dict[str, ReferenceSource] = {}
        self.load()

    def load(self) -> None:
        self._stored = {}
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"Cannot read reference library {self.path}: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise ValueError(f"Reference library {self.path}: unexpected format")
        for d in data["sources"]:
            src = ReferenceSource.from_dict(d)
            src.builtin = False
            if src.id not in self._builtin:
                self._stored[src.id] = src

    def save(self) -> None:
        payload = {"schema": 1, "sources": [s.to_dict() for s in self._stored.values()]}
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, self.path)

    def list(self) -> list[ReferenceSource]:
        return list(self._builtin.values()) + sorted(
            self._stored.values(), key=lambda s: (s.name.lower(), s.id))

    def get(self, source_id: str) -> ReferenceSource | None:
        return self._builtin.get(source_id) or self._stored.get(source_id)

    def add(self, src: ReferenceSource) -> ReferenceSource:
        if src.id in self._builtin:
            raise ValueError(f"'{src.id}' is a built-in source id and cannot be replaced")
        src.builtin = False
        self._stored[src.id] = src
        self.save()
        return src

    def import_file(self, path: str, *, name: str, serial: str,
                    unit: str = UNIT_UW_CM2_NM,
                    valid_min_nm: float | None = None, valid_max_nm: float | None = None,
                    input_optic: str = "", notes: str = "",
                    quantity: str | None = None) -> ReferenceSource:
        if not str(name).strip():
            raise ValueError("Name must not be empty")
        if unit not in KNOWN_UNITS:
            raise ValueError(f"Unknown unit '{unit}' (known: {', '.join(KNOWN_UNITS)})")
        q = quantity_of_unit(unit)
        if not quantity:
            quantity = q or QUANTITY_IRRADIANCE
        if quantity not in QUANTITIES:
            raise ValueError(f"Unknown quantity '{quantity}' (allowed: {', '.join(QUANTITIES)})")
        if q is not None and q != quantity:
            raise ValueError(f"Unit '{unit}' is a {q} unit, not {quantity}")
        if input_optic and input_optic not in INPUT_OPTICS:
            raise ValueError(f"Unknown input optic '{input_optic}' "
                             f"(allowed: {', '.join(INPUT_OPTICS)} or empty)")
        wl, values = parse_reference_file(path)
        dmin, dmax = float(wl[0]), float(wl[-1])
        vmin = dmin if valid_min_nm is None else float(valid_min_nm)
        vmax = dmax if valid_max_nm is None else float(valid_max_nm)
        if not vmin < vmax:
            raise ValueError(f"valid_min_nm ({vmin:g}) must be below valid_max_nm ({vmax:g})")
        if vmin < dmin or vmax > dmax:
            raise ValueError(f"Validity range {vmin:g}-{vmax:g} nm lies outside the data "
                             f"range {dmin:g}-{dmax:g} nm")
        src = ReferenceSource(
            id=_slug(name, serial), name=str(name).strip(), serial=str(serial).strip(),
            unit=unit, valid_min_nm=vmin, valid_max_nm=vmax, input_optic=input_optic,
            source_file=os.path.basename(path), imported=_now_iso(),
            wl=wl, values=values, notes=notes, quantity=quantity)
        return self.add(src)

    def remove(self, source_id: str) -> bool:
        if source_id in self._builtin or source_id not in self._stored:
            return False
        del self._stored[source_id]
        self.save()
        return True
