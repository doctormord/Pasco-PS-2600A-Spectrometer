"""
Central export location + filename composition, shared by every save function
in the app (spectrum CSV/PNG, time-lapse CSV/frame/autosave, colour & filter
reports and metrics) so a single "DATA" setting governs where and how files are
named everywhere.

- Directory comes from config key ``export_dir`` (empty = the program folder).
  If it can't be used (missing/uncreatable/not writable) the caller gets the
  program folder plus an error string to surface in the UI.
- Filename comes from config key ``export_filename_template`` — a pattern with
  {tokens} the user composes in the DATA section. Unknown/empty tokens degrade
  gracefully; the result is always filesystem-safe.
"""
from __future__ import annotations

import os
import re
import datetime

from app_config import Config

DEFAULT_TEMPLATE = "{kind}_{datetime}"
# Tokens offered in the UI. Keep this list and the fields dict in build_filename
# in sync.
TOKENS = ["{kind}", "{device}", "{serial}", "{wl0}", "{wl1}",
          "{date}", "{time}", "{datetime}", "{n}", "{n:03}"]
TOKEN_HELP = ("Available: {kind} {device} {serial} {wl0} {wl1} {date} {time} "
              "{datetime} · counter {n} (or {n:03} for zero-pad, e.g. 001)")

# Matches a counter token: {n} or {n:03} (the number = minimum zero-pad width).
_COUNTER = re.compile(r"\{n(?::0*(\d+))?\}")


def program_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def configured_dir() -> str:
    d = (Config.get("export_dir", "") or "").strip()
    return d or program_dir()


def ensure_export_dir() -> tuple[str, str | None]:
    """Return (directory, error_or_None). Try to use/create the configured
    export directory; on any failure fall back to the program folder and return
    a human-readable error so the UI can show it."""
    d = configured_dir()
    try:
        os.makedirs(d, exist_ok=True)
        if not os.path.isdir(d) or not os.access(d, os.W_OK):
            raise OSError("not writable")
        return d, None
    except OSError as e:
        return program_dir(), f"Export folder '{d}' unusable ({e}); using program folder."


_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]+")
_REPEAT = re.compile(r"[_-]{2,}")


def _san(s) -> str:
    return _ILLEGAL.sub("_", str(s)).strip("_")


def build_filename(kind: str, ext: str, meta: dict | None = None,
                   counter: int | None = None) -> str:
    """Compose the filename. A counter token ({n} / {n:03}) is filled with
    `counter` if given, else with its minimum padded start value (for previews).
    Use export_path() to get a real, collision-free counter."""
    meta = meta or {}
    now = datetime.datetime.now()

    def _num(key):
        v = meta.get(key, "")
        try:
            return str(int(round(float(v))))
        except (TypeError, ValueError):
            return ""

    fields = {
        "kind":     _san(kind) or "export",
        "device":   _san(meta.get("device", "")),
        "serial":   _san(meta.get("serial", "")),
        "wl0":      _num("wl0"),
        "wl1":      _num("wl1"),
        "date":     now.strftime("%Y%m%d"),
        "time":     now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y%m%d_%H%M%S"),
    }
    tmpl = (Config.get("export_filename_template", DEFAULT_TEMPLATE)
            or DEFAULT_TEMPLATE)

    # Fill counter token(s) first (str.format can't express {n:03} with a name).
    def _fill_counter(m):
        width = int(m.group(1)) if m.group(1) else 1
        val = counter if counter is not None else 1
        return str(val).zfill(width)
    tmpl_filled = _COUNTER.sub(_fill_counter, tmpl)

    try:
        name = tmpl_filled.format(**fields)
    except (KeyError, IndexError, ValueError):
        name = _COUNTER.sub(_fill_counter, DEFAULT_TEMPLATE).format(**fields)
    name = _san(name)
    name = _REPEAT.sub("_", name).strip("_")     # collapse empty-token gaps
    if not name:
        name = fields["kind"] + "_" + fields["datetime"]
    ext = ext if ext.startswith(".") else "." + ext
    return name + ext


def template_has_counter(tmpl: str | None = None) -> bool:
    tmpl = tmpl if tmpl is not None else Config.get("export_filename_template", DEFAULT_TEMPLATE)
    return bool(_COUNTER.search(tmpl or ""))


def export_path(kind: str, ext: str, meta: dict | None = None) -> tuple[str, str | None]:
    """Full path inside the export directory. Returns (path, error_or_None).
    If the template uses a counter token, scans for the next free index so an
    existing file is never overwritten; otherwise just returns the composed
    name (callers that reuse timestamps get uniqueness from {datetime})."""
    d, err = ensure_export_dir()
    if not template_has_counter():
        return os.path.join(d, build_filename(kind, ext, meta)), err
    # Counter template: find the lowest index whose file doesn't exist yet.
    n = 1
    while True:
        candidate = os.path.join(d, build_filename(kind, ext, meta, counter=n))
        if not os.path.exists(candidate):
            return candidate, err
        n += 1
        if n > 1_000_000:                        # safety valve
            return candidate, err


def meta_from_report(report_meta: dict | None, wl=None) -> dict:
    """Adapt a report_meta() dict (+ optional wavelength array) to the token
    fields used here."""
    report_meta = report_meta or {}
    m = {
        "device": report_meta.get("Spectrometer", "") or "",
        "serial": report_meta.get("Serial", "") or "",
    }
    if wl is not None and len(wl):
        m["wl0"] = wl[0]
        m["wl1"] = wl[-1]
    return m
