"""Calibration and linearity protocols (PDF + CSV).

Input: CalibrationWorkflow.record() / LinearityWorkflow.record() dicts.
Device independent and Qt free; robust against missing fields / spectra=None.

The PDF is produced with a small flow layout engine (_Doc): a y cursor, wrapped
text measured with the renderer, row heights that grow with the wrapped lines,
automatic page breaks. Nothing is truncated and nothing overlaps.
"""
from __future__ import annotations

import re
from datetime import datetime

CSV_COLUMNS = ["wavelength_nm", "C_cal_counts_per_s", "E_ref", "S_relative", "K",
               "valid", "C_repeat", "C_half", "check_fail"]
_SPEC_KEYS = ["wl", "C_cal", "E_ref", "S", "K", "valid", "C_repeat", "C_half", "check_fail"]
LINEARITY_CURVE_COLUMNS = ["x_counts", "f_counts", "deviation_pct"]
LINEARITY_STEP_COLUMNS = ["role", "t_us", "peak_pct_fs", "lamp", "rate_raw", "rate_corr",
                          "dev_raw_pct", "dev_corr_pct"]


# ── helpers ──────────────────────────────────────────────────────────────
def _slug(s, default="x") -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(s or "")).strip("-")
    return s or default


def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _num(v, fmt="{:.6g}", none=""):
    """Formatted number; `none` for missing or non-finite values (never 'nan')."""
    if v is None:
        return none
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):
        return none
    return fmt.format(f)


def _yesno(v, none="n/a"):
    return none if v is None else ("yes" if v else "no")


def _spectra(record: dict) -> dict:
    sp = record.get("spectra") if isinstance(record, dict) else None
    return sp if isinstance(sp, dict) else {}


def _msg(x) -> str:
    """Text of a hint / failure given as str or dict."""
    if isinstance(x, dict):
        return str(x.get("message") or x.get("text") or x)
    return str(x)


def _onoff(v) -> str:
    """'on' / 'off' / 'n/a' (never True/False/empty)."""
    if isinstance(v, str):
        return v if v else "n/a"
    return "n/a" if v is None else ("on" if v else "off")


def _nl_text(acq: dict) -> str:
    t = acq.get("nl_correction_text")
    return str(t) if t else _onoff(acq.get("nl_correction"))


def _lin_text(acq: dict) -> str:
    t = acq.get("linearity_text")
    if t:
        return str(t)
    lin = acq.get("linearity")
    if isinstance(lin, dict) and lin.get("active") is not None:
        return _onoff(lin.get("active"))
    return "n/a"


def _stamp(record: dict) -> str:
    ts = (record or {}).get("finished") or (record or {}).get("started")
    if ts:
        try:
            return datetime.fromisoformat(str(ts)).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            digits = re.sub(r"\D", "", str(ts))
            if len(digits) >= 14:
                return f"{digits[:8]}_{digits[8:14]}"
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_protocol_basename(record: dict) -> str:
    record = record or {}
    parts = ["calib", _slug(_g(record, "device", "backend"), "device"),
             _slug(_g(record, "device", "serial"), "noserial"),
             _slug(_g(record, "optics", "name"), "optics"),
             _slug(record.get("type"), "type"), _stamp(record)]
    return re.sub(r"[^A-Za-z0-9_-]", "", "_".join(parts))


def default_linearity_basename(record: dict) -> str:
    record = record or {}
    parts = ["linearity", _slug(_g(record, "device", "backend"), "device"),
             _slug(_g(record, "device", "serial"), "noserial"),
             _slug(record.get("mode"), "mode"), _stamp(record)]
    return re.sub(r"[^A-Za-z0-9_-]", "", "_".join(parts))


def _segments_text(segs) -> str:
    if not segs:
        return ""
    return ", ".join(f"{float(a):.1f}-{float(b):.1f}" for a, b in segs)


def _band_text(b: dict) -> str:
    return f"{_num(b.get('lo'), '{:.0f}')}-{_num(b.get('hi'), '{:.0f}')} nm"


def _cause_lines(c) -> list[str]:
    """One line per cause (magnitude included) + the overall message."""
    if not isinstance(c, dict):
        return []
    out = []
    if c.get("message"):
        out.append(str(c["message"]))
    for x in c.get("causes") or []:
        mag = ""
        if x.get("value") is not None:
            mag = f"{_num(x.get('value'), '{:+.4g}')} {x.get('unit') or ''}".strip()
        eff = _num(x.get("effect_pct"), "{:+.3f} %")
        sig = "significant" if x.get("significant") else "not significant"
        line = f"{x.get('cause', '?')}: {mag or 'n/a'}, effect {eff or 'n/a'} ({sig})"
        if x.get("message"):
            line += f" - {x['message']}"
        out.append(line)
    return out


# ── metadata (shared by CSV and PDF) ─────────────────────────────────────
def _metadata(record: dict) -> list[tuple[str, str]]:
    r = record or {}
    acq, chk, ref = r.get("acquisition") or {}, r.get("checks") or {}, r.get("reference") or {}
    t = acq.get("integration_us")
    h = acq.get("half_integration_us")
    peak = acq.get("peak_frac")
    vr = r.get("valid_range_nm")
    gs = r.get("gain_span")
    if isinstance(gs, (list, tuple)):
        gs_s = " - ".join(_num(x, "{:.4g}") for x in gs)
    else:
        gs_s = _num(gs, "{:.4g}")
    skipped = chk.get("itime_pass") is None
    m = [
        ("started", r.get("started") or ""), ("finished", r.get("finished") or ""),
        ("device", _g(r, "device", "backend", default="")),
        ("serial", _g(r, "device", "serial", default="")),
        ("optics_name", _g(r, "optics", "name", default="")),
        ("optics_input", _g(r, "optics", "input", default="")),
        ("optics_notes", _g(r, "optics", "notes", default="")),
        ("reference_name", ref.get("name") or ""), ("reference_serial", ref.get("serial") or ""),
        ("reference_unit", ref.get("unit") or ""),
        ("reference_valid_min_nm", _num(ref.get("valid_min_nm"))),
        ("reference_valid_max_nm", _num(ref.get("valid_max_nm"))),
        ("reference_source_file", ref.get("source_file") or ""),
        ("type", r.get("type") or ""),
        ("t_int_us", _num(t, "{:.0f}")), ("t_int_ms", _num(None if t is None else float(t) / 1000, "{:.4g}")),
        ("t_int_half_us", _num(h, "{:.0f}")),
        ("t_int_half_ms", _num(None if h is None else float(h) / 1000, "{:.4g}")),
        ("averages_N", _num(acq.get("averages"), "{:.0f}")),
        ("nl_correction", _nl_text(acq)),
        ("peak_pct_fs", _num(None if peak is None else 100 * float(peak), "{:.1f}")),
        ("valid_range_nm", "" if not vr else " - ".join(_num(x, "{:.1f}") for x in vr)),
        ("gain_span", gs_s), ("n_valid", _num(r.get("n_valid"), "{:.0f}")),
        ("unit", r.get("unit") or ""),
        ("quantity", r.get("quantity") or ""),
        ("source_hint", r.get("source_hint") or ""),
        ("stability_pct",_num(chk.get("stability_pct"), "{:.3f}")),
        ("stability_threshold_pct", _num(chk.get("stability_threshold_pct"), "{:.3f}")),
        ("stability_delay_s", _num(chk.get("stability_delay_s"), "{:.3g}")),
        ("stability_pass", _yesno(chk.get("stability_pass"), "")),
        ("itime_crosscheck_pct", _num(chk.get("itime_crosscheck_pct"), "{:.3f}")),
        ("itime_threshold_pct", _num(chk.get("itime_threshold_pct"), "{:.3f}")),
        ("itime_pass", "skipped" if skipped else _yesno(chk.get("itime_pass"))),
        ("wl_fingerprint", r.get("wl_fingerprint") or ""),
        ("valid_segments_nm", _segments_text(r.get("valid_segments_nm"))),
    ]
    pl = r.get("plausibility") or {}
    if pl:
        met, lim = pl.get("metrics") or {}, pl.get("limits") or {}
        m += [
            ("plausibility_pass", _yesno(pl.get("pass"))),
            ("plausibility_override", _yesno(pl.get("override"), "no")),
            ("coverage_pct", _num(None if met.get("coverage") is None
                                  else 100 * float(met["coverage"]), "{:.1f}")),
            ("min_coverage_pct", _num(None if lim.get("min_coverage") is None
                                      else 100 * float(lim["min_coverage"]), "{:.1f}")),
            ("max_gain_span", _num(lim.get("max_gain_span"), "{:.4g}")),
            ("roughness_pct", _num(None if met.get("roughness") is None
                                   else 100 * float(met["roughness"]), "{:.2f}")),
            ("max_roughness_pct", _num(None if lim.get("max_roughness") is None
                                       else 100 * float(lim["max_roughness"]), "{:.2f}")),
            ("isolated_pct", _num(None if met.get("isolated_frac") is None
                                  else 100 * float(met["isolated_frac"]), "{:.1f}")),
            ("max_isolated_pct", _num(None if lim.get("max_isolated_frac") is None
                                      else 100 * float(lim["max_isolated_frac"]), "{:.1f}")),
            ("largest_segment_pct", _num(None if met.get("largest_segment_frac") is None
                                         else 100 * float(met["largest_segment_frac"]), "{:.1f}")),
            ("max_gap_nm", _num(lim.get("max_gap_nm"), "{:.3g}")),
        ]
        for i, f in enumerate(pl.get("failures") or [], 1):
            m.append((f"plausibility_failure_{i}", _msg(f)))
        for i, hnt in enumerate(pl.get("hints") or [], 1):
            m.append((f"plausibility_hint_{i}", _msg(hnt)))
    # ── new content ──
    lin = acq.get("linearity") if isinstance(acq.get("linearity"), dict) else {}
    m += [("linearity_correction", _lin_text(acq)),
          ("linearity_id", lin.get("id") or ""),
          ("linearity_created", lin.get("created") or ""),
          ("linearity_state", lin.get("state") or ""),
          ("dark_sequence", acq.get("dark_sequence") or "")]
    m += _limit_rows(r)
    for i, d in enumerate(r.get("dark_drift") or [], 1):
        m.append((f"dark_drift_{i}",
                  f"t_int_us={_num(d.get('integration_us'), '{:.0f}')} "
                  f"mean_counts={_num(d.get('mean_counts'), '{:+.3f}')} "
                  f"p95_abs_counts={_num(d.get('p95_abs_counts'), '{:.3f}')} "
                  f"sigma_counts={_num(d.get('sigma_counts'), '{:.3f}')} "
                  f"pct_of_peak={_num(d.get('pct_of_peak'), '{:.3f}')} "
                  f"seconds={_num(d.get('seconds'), '{:.1f}')} "
                  f"warn={_yesno(d.get('warn'), 'no')}"))
    for key in ("stability_fail_pixels", "itime_fail_pixels", "dark_drift_counts"):
        if chk.get(key) is not None:
            m.append((f"check_{key}", _num(chk.get(key), "{:.6g}")))
    for key in ("stability_cause", "itime_cause"):
        if chk.get(key):
            m.append((f"check_{key}", str(chk[key])))
    for kind, pc in (r.get("pixel_checks") or {}).items():
        pc = pc or {}
        m.append((f"pixel_check_{kind}",
                  f"n_tested={_num(pc.get('n_tested'), '{:.0f}')} "
                  f"n_fail={_num(pc.get('n_fail'), '{:.0f}')} "
                  f"fail_pct={_num(None if pc.get('fail_frac') is None else 100 * pc['fail_frac'], '{:.3f}')} "
                  f"k={_num(pc.get('k'), '{:.3g}')} "
                  f"threshold_pct={_num(pc.get('threshold_pct'), '{:.3g}')}"))
    for kind, c in (r.get("causes") or {}).items():
        c = c or {}
        m.append((f"cause_{kind}_dominant", str(c.get("dominant") or "none")))
        for i, line in enumerate(_cause_lines(c), 1):
            m.append((f"cause_{kind}_{i}", line))
    ver = r.get("verification")
    if isinstance(ver, dict):
        ov = ver.get("overall") or {}
        m += [("verification_pass", _yesno(ver.get("pass"))),
              ("verification_warn_pct", _num(ver.get("warn_pct"), "{:.3g}")),
              ("verification_norm_range_nm", " - ".join(_num(x, "{:.0f}") for x in
                                                        (ver.get("norm_range_nm") or []))),
              ("verification_band_nm", _num(ver.get("band_nm"), "{:.0f}")),
              ("verification_reference", ver.get("reference_name") or ""),
              ("verification_profile", ver.get("profile_label") or ""),
              ("verification_measurement_id", ver.get("measurement_id") or ""),
              ("verification_integration_us", _num(ver.get("integration_us"), "{:.0f}")),
              ("verification_rms_pct", _num(ov.get("rms_pct"), "{:.4g}")),
              ("verification_mean_abs_pct", _num(ov.get("mean_abs_pct"), "{:.4g}")),
              ("verification_max_abs_pct", _num(ov.get("max_abs_pct"), "{:.4g}"))]
        for pre, blk in (("verification", ver), ("verification_abs", ver.get("absolute"))):
            if not isinstance(blk, dict):
                continue
            if pre != "verification":
                aov = blk.get("overall") or {}
                m += [("verification_abs_rms_pct", _num(aov.get("rms_pct"), "{:.4g}")),
                      ("verification_abs_mean_abs_pct", _num(aov.get("mean_abs_pct"), "{:.4g}")),
                      ("verification_abs_max_abs_pct", _num(aov.get("max_abs_pct"), "{:.4g}"))]
            for i, b in enumerate(blk.get("bands") or [], 1):
                m.append((f"{pre}_band_{i}",
                          f"{_band_text(b)} n={_num(b.get('n'), '{:.0f}')} "
                          f"mean_pct={_num(b.get('mean_pct'), '{:+.3f}')} "
                          f"max_abs_pct={_num(b.get('max_abs_pct'), '{:.3f}')} "
                          f"warn={_yesno(b.get('warn'), 'no')}"))
    for i, w in enumerate(r.get("warnings") or [], 1):
        m.append((f"warning_{i}", str(w)))
    for i, p in enumerate(r.get("profiles") or [], 1):
        m.append((f"profile_{i}", f"{p.get('id', '')} type={p.get('type', '')} "
                                  f"version={p.get('version', '')}"))
    return m


# ── CSV ──────────────────────────────────────────────────────────────────
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    try:
        return repr(float(v))
    except (TypeError, ValueError):
        return str(v)


def _csv_clean(v) -> str:
    return str(v).replace(";", ",").replace("\r", " ").replace("\n", " ")


def _write_lines(path: str, lines: list[str]) -> str:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


def write_protocol_csv(record: dict, path: str) -> str:
    sp = _spectra(record)
    cols = {k: sp.get(k) for k in _SPEC_KEYS}
    wl = cols["wl"] or []
    lines = ["# calibration protocol"]
    for k, v in _metadata(record):
        lines.append(f"# {k};{_csv_clean(v)}")
    lines.append(";".join(CSV_COLUMNS))
    for i in range(len(wl)):
        row = []
        for k in _SPEC_KEYS:
            c = cols[k]
            row.append(_cell(c[i] if c is not None and i < len(c) else None))
        lines.append(";".join(row))
    return _write_lines(path, lines)


# ══════════════════════════════════════════════════════════════════════════
#  Flow layout engine
# ══════════════════════════════════════════════════════════════════════════
PAGE_W, PAGE_H = 595.28, 841.89          # A4 portrait, points
MARGIN = 42.0
CONTENT_W = PAGE_W - 2 * MARGIN
BOTTOM = PAGE_H - MARGIN - 6.0            # lowest allowed content y
GREY, BLUE, ORANGE, RED, DARK, MUTED = ("#d9d9d9", "#1f5fa8", "#d9730d", "#b00020",
                                        "#111111", "#555555")
WARN_FILL, HEAD_FILL = "#fde9d0", "#eeeeee"


class _Doc:
    """Paged flow layout on A4 portrait (matplotlib only). Coordinates in points,
    y grows downwards. Text is measured with the renderer and wrapped."""

    def __init__(self, title: str):
        import matplotlib
        matplotlib.use("Agg")
        self.title = title
        self.figs: list = []
        self.fig = self.ax = None
        self.y = MARGIN
        self.page_no = 0
        self.new_page()

    # -- pages --
    def new_page(self):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        fig = Figure(figsize=(PAGE_W / 72, PAGE_H / 72), dpi=72, facecolor="white")
        FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, PAGE_W)
        ax.set_ylim(PAGE_H, 0)
        ax.axis("off")
        self.fig, self.ax, self.y = fig, ax, MARGIN
        self.figs.append(fig)
        self.page_no += 1
        self._renderer = fig.canvas.get_renderer()

    def ensure(self, h: float):
        if self.y + h > BOTTOM and self.y > MARGIN + 1:
            self.new_page()

    def gap(self, h: float = 6.0):
        self.y += h

    # -- text --
    def _width(self, s: str, fs: float, bold: bool = False) -> float:
        from matplotlib.font_manager import FontProperties
        w, _h, _d = self._renderer.get_text_width_height_descent(
            s, FontProperties(size=fs, weight="bold" if bold else "normal"), False)
        return float(w)

    def wrap(self, text, fs: float, width: float, bold: bool = False) -> list[str]:
        lines: list[str] = []
        for para in str(text if text is not None else "").replace("\r", "").split("\n"):
            cur = ""
            for word in para.split(" "):
                cand = word if not cur else cur + " " + word
                if self._width(cand, fs, bold) <= width:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                while self._width(word, fs, bold) > width and len(word) > 1:
                    k = len(word)
                    while k > 1 and self._width(word[:k], fs, bold) > width:
                        k -= 1
                    lines.append(word[:k])
                    word = word[k:]
                cur = word
            lines.append(cur)
        return lines

    def _put(self, x, y, s, fs, bold=False, color=DARK):
        if s:
            self.ax.text(x, y, s, fontsize=fs, weight="bold" if bold else "normal",
                         color=color, va="top", ha="left", parse_math=False)

    @staticmethod
    def lh(fs: float) -> float:
        return fs * 1.35

    def title_block(self, text: str, sub: str = ""):
        self._put(MARGIN, self.y, text, 16, True)
        self.y += 16 * 1.4
        if sub:
            for ln in self.wrap(sub, 8, CONTENT_W):
                self._put(MARGIN, self.y, ln, 8, color=MUTED)
                self.y += self.lh(8)
        self.y += 4

    def heading(self, text: str):
        self.y += 6
        self.ensure(self.lh(9.5) + 3 * self.lh(7.5))
        self._put(MARGIN, self.y, text, 9.5, True)
        self.y += self.lh(9.5) + 2

    def para(self, text, fs=7.5, color=DARK, indent=0.0, bold=False):
        """Wrapped paragraph; breaks across pages line by line."""
        for ln in self.wrap(text, fs, CONTENT_W - indent, bold):
            self.ensure(self.lh(fs))
            self._put(MARGIN + indent, self.y, ln, fs, bold, color)
            self.y += self.lh(fs)
        self.y += 1.5

    def bullets(self, items, fs=7.5, color=DARK):
        items = list(items)
        if not items:
            self.para("none", fs, MUTED)
        for it in items:
            lines = self.wrap(it, fs, CONTENT_W - 10)
            for i, ln in enumerate(lines):
                self.ensure(self.lh(fs))
                if i == 0:
                    self._put(MARGIN, self.y, "-", fs, color=color)
                self._put(MARGIN + 10, self.y, ln, fs, color=color)
                self.y += self.lh(fs)
            self.y += 1.5

    # -- key/value block --
    def kv(self, items, ncols=2, fs=7.5, label_w=82.0):
        items = [(k, "-" if v in (None, "") else v) for k, v in items]
        cw = CONTENT_W / ncols
        for i in range(0, len(items), ncols):
            chunk = items[i:i + ncols]
            cells = []
            for k, v in chunk:
                kl = self.wrap(k, fs, label_w - 4)
                vl = self.wrap(v, fs, cw - label_w - 6)
                cells.append((kl, vl))
            h = max(max(len(a), len(b)) for a, b in cells) * self.lh(fs) + 2
            self.ensure(h)
            for j, (kl, vl) in enumerate(cells):
                x = MARGIN + j * cw
                for n, ln in enumerate(kl):
                    self._put(x, self.y + n * self.lh(fs), ln, fs, color=MUTED)
                for n, ln in enumerate(vl):
                    self._put(x + label_w, self.y + n * self.lh(fs), ln, fs)
            self.y += h

    # -- table --
    def table(self, headers, rows, widths, fs=7.5, row_fill=None):
        """rows: list of lists; a cell is str or (str, color). widths: fractions.
        row_fill: optional list of fill colours (or None) per row."""
        from matplotlib.patches import Rectangle
        pad = 3.0
        cw = [w / sum(widths) * CONTENT_W for w in widths]
        xs = [MARGIN + sum(cw[:i]) for i in range(len(cw))]

        def draw_row(cells, bold, fill):
            wrapped, col = [], []
            for c, w in zip(cells, cw):
                txt, colr = (c if isinstance(c, tuple) else (c, DARK))
                wrapped.append(self.wrap("" if txt is None else txt, fs, w - 2 * pad, bold))
                col.append(colr)
            h = max(len(w) for w in wrapped) * self.lh(fs) + 2 * pad
            return wrapped, col, h

        def paint(wrapped, col, h, bold, fill):
            for x, w, lines, colr in zip(xs, cw, wrapped, col):
                self.ax.add_patch(Rectangle((x, self.y), w, h, facecolor=fill or "white",
                                            edgecolor="#bbbbbb", lw=0.5, clip_on=False))
                for n, ln in enumerate(lines):
                    self._put(x + pad, self.y + pad + n * self.lh(fs), ln, fs, bold, colr)
            self.y += h

        hd = draw_row(list(headers), True, HEAD_FILL)
        self.ensure(hd[2] + 12)
        paint(*hd, True, HEAD_FILL)
        for i, r in enumerate(rows):
            rd = draw_row(r, False, None)
            if self.y + rd[2] > BOTTOM:
                self.new_page()
                paint(*hd, True, HEAD_FILL)
            paint(*rd, False, row_fill[i] if row_fill else None)
        self.y += 4

    # -- plots --
    def plot(self, height=120.0, top_pad=8.0, right_pad=0.0):
        """Reserve an axes block (height = plot area) incl. space for the x label
        below; returns the axes."""
        total = top_pad + height + 36.0
        self.ensure(total)
        left, right = MARGIN + 50.0, PAGE_W - MARGIN - 8.0 - right_pad
        top = self.y + top_pad
        rect = [left / PAGE_W, 1 - (top + height) / PAGE_H,
                (right - left) / PAGE_W, height / PAGE_H]
        a = self.fig.add_axes(rect)
        a.grid(alpha=0.3)
        a.tick_params(labelsize=7)
        self.y += total + 2
        return a

    def nodata(self, ax, txt="no data"):
        ax.text(0.5, 0.5, txt, ha="center", va="center", transform=ax.transAxes,
                color="grey", fontsize=8)

    # -- output --
    def save(self, path: str):
        from matplotlib.backends.backend_pdf import PdfPages
        n = len(self.figs)
        for i, fig in enumerate(self.figs, 1):
            fig.text(MARGIN / PAGE_W, 1 - (PAGE_H - MARGIN + 8) / PAGE_H, self.title,
                     fontsize=6.5, color=MUTED, va="top", parse_math=False)
            fig.text(1 - MARGIN / PAGE_W, 1 - (PAGE_H - MARGIN + 8) / PAGE_H,
                     f"Page {i}/{n}", fontsize=6.5, color=MUTED, va="top", ha="right")
        with PdfPages(path) as pdf:
            for fig in self.figs:
                pdf.savefig(fig)
        return path


def _arr(np, a, n):
    if a is None:
        return np.full(n, np.nan)
    out = np.array([np.nan if v is None else float(v) for v in a], float)
    if len(out) < n:
        out = np.concatenate([out, np.full(n - len(out), np.nan)])
    return out[:n]


def _style_axes(ax, xlabel=None, ylabel=None, color=None):
    ax.tick_params(labelsize=7)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=7.5)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=7.5, color=color or DARK)


# ══════════════════════════════════════════════════════════════════════════
#  Calibration protocol PDF
# ══════════════════════════════════════════════════════════════════════════
def _type_text(kind, quantity) -> str:
    """'absolute radiance' / 'absolute irradiance' / plain type."""
    kind = str(kind or "")
    if kind == "absolute" and quantity in ("radiance", "irradiance"):
        return f"absolute {quantity}"
    return kind


def _unit_text(unit) -> str:
    import reference_sources as rs
    return rs.unit_label(str(unit or ""))


def _status(v, ok="passed", bad="FAILED", none="n/a"):
    if v is None:
        return (none, DARK)
    return (ok, DARK) if v else (bad, RED)


def write_protocol_pdf(record: dict, path: str) -> str:
    import numpy as np

    record = record or {}
    sp = _spectra(record)
    wl = np.array([np.nan if v is None else float(v) for v in (sp.get("wl") or [])], float)
    n = len(wl)
    C, E = _arr(np, sp.get("C_cal"), n), _arr(np, sp.get("E_ref"), n)
    S, K = _arr(np, sp.get("S"), n), _arr(np, sp.get("K"), n)
    rep, half = _arr(np, sp.get("C_repeat"), n), _arr(np, sp.get("C_half"), n)
    vl, cf = sp.get("valid"), sp.get("check_fail")
    valid = np.zeros(n, bool) if vl is None else np.array(
        [bool(v) for v in vl] + [False] * max(0, n - len(vl)), bool)[:n]
    failpx = np.zeros(n, bool) if cf is None else np.array(
        [bool(v) for v in cf] + [False] * max(0, n - len(cf)), bool)[:n]
    vr = record.get("valid_range_nm")
    chk = record.get("checks") or {}
    kind = record.get("type")
    unit = record.get("unit") or ""
    segs = record.get("valid_segments_nm") or []
    meta = dict(_metadata(record))
    acq = record.get("acquisition") or {}

    def shade(ax):
        if n and segs:
            ax.axvspan(np.nanmin(wl), np.nanmax(wl), color=GREY, alpha=0.6, lw=0)
            for lo, hi in segs:
                ax.axvspan(float(lo), float(hi), color="#eef6ee", alpha=1.0, lw=0)
            return
        if n and vr and len(vr) == 2 and None not in vr:
            ax.axvspan(np.nanmin(wl), vr[0], color=GREY, alpha=0.6, lw=0)
            ax.axvspan(vr[1], np.nanmax(wl), color=GREY, alpha=0.6, lw=0)
            ax.axvspan(vr[0], vr[1], color="#eef6ee", alpha=0.6, lw=0)

    doc = _Doc("Calibration protocol")
    doc.title_block("Calibration protocol")

    # header
    lin_extra = lin = ""
    li = acq.get("linearity") if isinstance(acq.get("linearity"), dict) else {}
    lin = _lin_text(acq)
    if li.get("id"):
        lin_extra = f" (id {li['id']}" + (f", {li['created']}" if li.get("created") else "") + ")"
    doc.kv([("Started", meta["started"]), ("Finished", meta["finished"]),
            ("Device", meta["device"]), ("Serial", meta["serial"] or "-"),
            ("Type", _type_text(meta["type"], meta["quantity"])),
            ("Unit", _unit_text(meta["unit"])),
            ("Optics", f"{meta['optics_name']} ({meta['optics_input']})"),
            ("Reference", f"{meta['reference_name']} (S/N {meta['reference_serial'] or '-'})"),
            ("Ref. unit / range", f"{meta['reference_unit']}  "
                                  f"{meta['reference_valid_min_nm']}-{meta['reference_valid_max_nm']} nm"),
            ("t_int", f"{meta['t_int_us']} us ({meta['t_int_ms']} ms)"),
            ("t_int / 2", f"{meta['t_int_half_us']} us ({meta['t_int_half_ms']} ms)"),
            ("Averages N", meta["averages_N"]),
            ("NL correction", meta["nl_correction"]),
            ("Linearity corr.", lin + lin_extra),
            ("Dark sequence", meta["dark_sequence"] or "-"),
            ("Peak", f"{meta['peak_pct_fs']} % FS"),
            ("Valid range", f"{meta['valid_range_nm']} nm"),
            ("Gain span (hint)", meta["gain_span"]), ("Valid pixels", meta["n_valid"])])
    doc.kv([("Optics notes", meta["optics_notes"] or "-"),
            ("Ref. file", meta["reference_source_file"] or "-"),
            ("Valid segments", (meta["valid_segments_nm"] + " nm") if meta["valid_segments_nm"] else "-"),
            ("WL fingerprint", meta["wl_fingerprint"] or "-"),
            ("Limits", _limit_text(record))]
           + ([("Source note", meta["source_hint"])]
              if meta["quantity"] == "radiance" and meta["source_hint"] else []), ncols=1)

    # plot 1
    doc.heading("Calibration spectrum and reference")
    a1 = doc.plot(115, right_pad=44)
    if n and np.isfinite(C).any():
        shade(a1)
        a1.plot(wl, C, color=BLUE, lw=1.2)
        _style_axes(a1, "wavelength (nm)", "C_cal (counts/s)", BLUE)
        a2 = a1.twinx()
        a2.plot(wl, E, color=ORANGE, lw=1.2)
        a2.tick_params(labelsize=7)
        a2.set_ylabel(f"E_ref ({(record.get('reference') or {}).get('unit', '')})",
                      fontsize=7.5, color=ORANGE)
    else:
        doc.nodata(a1)

    # plot 2
    doc.heading("Result (valid pixels only)")
    a3 = doc.plot(115)
    res = K if kind == "absolute" else S
    if n and np.isfinite(res).any():
        shade(a3)
        a3.plot(wl, np.where(valid, res, np.nan), color=BLUE, lw=1.3)
        _style_axes(a3, "wavelength (nm)", f"K ({unit})" if kind == "absolute" else "S (relative)")
    else:
        doc.nodata(a3)

    # plot 3
    doc.heading("Checks (dashed: +/- threshold band; red ticks: masked pixels)")
    a4 = doc.plot(95)
    with np.errstate(divide="ignore", invalid="ignore"):
        r_rep, r_half = rep / C, half / C
    has_rep = bool(n and np.isfinite(r_rep[valid]).any())
    has_half = bool(n and np.isfinite(r_half[valid]).any())
    if has_rep or has_half:
        shade(a4)
        if has_rep:
            a4.plot(wl, np.where(valid, r_rep, np.nan), color=BLUE, lw=1, label="C_repeat / C_cal")
        if has_half:
            a4.plot(wl, np.where(valid, r_half, np.nan), color=ORANGE, lw=1, label="C_half / C_cal")
        for key, c, on in (("stability_threshold_pct", BLUE, has_rep),
                           ("itime_threshold_pct", ORANGE, has_half)):
            th = chk.get(key)
            if th is not None and on:
                for s in (1, -1):
                    a4.axhline(1 + s * float(th) / 100, color=c, ls="--", lw=0.8)
        if failpx.any():
            a4.plot(wl[failpx], np.full(int(failpx.sum()), 0.02), "|", color=RED, ms=7,
                    transform=a4.get_xaxis_transform(), label="masked pixel")
        a4.legend(fontsize=7, loc="upper right")
        _style_axes(a4, "wavelength (nm)", "ratio")
    else:
        doc.nodata(a4, "no check spectra")

    # check table
    doc.heading("Checks")
    skipped = chk.get("itime_pass") is None
    rows = [["Stability (p95)", _num(chk.get("stability_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("stability_threshold_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("stability_delay_s"), "delay {:.3g} s", ""),
             _status(chk.get("stability_pass"))],
            ["Integration time", _num(chk.get("itime_crosscheck_pct"), "{:.2f} %", "n/a"),
             _num(chk.get("itime_threshold_pct"), "{:.2f} %", "n/a"), "t/2 vs t",
             ("skipped", DARK) if skipped else _status(chk.get("itime_pass"))]]
    pl = record.get("plausibility") or {}
    if pl:
        met, lim = pl.get("metrics") or {}, pl.get("limits") or {}
        res_txt = ("passed", DARK) if pl.get("pass") else (
            ("FAILED (saved on request)", RED) if pl.get("override") else ("FAILED", RED))
        rows.append(["Broadband / coverage",
                     _num(None if met.get("coverage") is None else 100 * met["coverage"],
                          "{:.0f} %", "n/a"),
                     _num(None if lim.get("min_coverage") is None
                          else 100 * lim["min_coverage"], ">= {:.0f} %", "n/a"),
                     f"gain span {_num(met.get('gain_span'), '{:.1f}')}x (hint, limit "
                     f"{_num(lim.get('max_gain_span'), '{:.0f}')}x), roughness "
                     f"{_num(None if met.get('roughness') is None else 100 * met['roughness'], '{:.1f}')} %",
                     res_txt])
    doc.table(["Check", "Deviation", "Threshold", "Note", "Result"], rows, [2.2, 1.4, 1.4, 4.0, 1.8])

    # per-pixel checks
    pcs = record.get("pixel_checks") or {}
    if pcs:
        names = {"stability": "Stability (repeat)", "itime": "Integration time (t/2)"}
        prow = []
        for kd in ("stability", "itime"):
            pc = pcs.get(kd)
            if not isinstance(pc, dict):
                continue
            prow.append([names[kd], _num(pc.get("n_tested"), "{:.0f}", "n/a"),
                         _num(pc.get("n_fail"), "{:.0f}", "n/a"),
                         _num(None if pc.get("fail_frac") is None else 100 * pc["fail_frac"],
                              "{:.2f} %", "n/a"),
                         _num(pc.get("k"), "{:.3g}", "n/a"),
                         _num(pc.get("threshold_pct"), "{:.3g} %", "n/a")])
        if prow:
            doc.heading("Per-pixel checks (failing pixels are masked in the result)")
            doc.table(["Check", "Pixels tested", "Masked", "Fraction", "k (sigma)", "Threshold"],
                      prow, [3, 1.6, 1.3, 1.3, 1.3, 1.4])

    # cause model
    cause_blocks = [(t, (record.get("causes") or {}).get(k)) for k, t in
                    (("stability", "Stability (repeat measurement)"),
                     ("itime", "Integration time cross check (t/2)"))]
    cause_blocks = [(t, c) for t, c in cause_blocks if isinstance(c, dict)]
    if cause_blocks:
        doc.heading("Cause analysis")
        for t, c in cause_blocks:
            doc.para(t, 7.5, MUTED, bold=True)
            doc.bullets(_cause_lines(c))

    # dark drift
    dd = record.get("dark_drift") or []
    if dd:
        doc.heading(f"Dark drift (dark sequence: {meta['dark_sequence'] or 'n/a'})")
        drows = []
        fills = []
        for d in dd:
            warn = bool(d.get("warn"))
            drows.append([_num(None if d.get("integration_us") is None
                               else d["integration_us"] / 1000, "{:.4g} ms", "n/a"),
                          _num(d.get("mean_counts"), "{:+.2f}", "n/a"),
                          _num(d.get("p95_abs_counts"), "{:.2f}", "n/a"),
                          _num(d.get("sigma_counts"), "{:.2f}", "n/a"),
                          _num(d.get("pct_of_peak"), "{:.3f} %", "n/a"),
                          _num(d.get("seconds"), "{:.1f} s", "n/a"),
                          ("WARN", RED) if warn else ("ok", DARK)])
            fills.append(WARN_FILL if warn else None)
        doc.table(["t_int", "Mean (counts)", "p95 |d| (counts)", "Noise (counts)",
                   "% of peak", "Time between", "Status"], drows,
                  [1.3, 1.5, 1.6, 1.5, 1.3, 1.5, 1.1], row_fill=fills)

    # plausibility hints / failures
    if pl:
        if pl.get("failures"):
            doc.heading("Plausibility failures")
            doc.bullets([_msg(f) for f in pl["failures"]], color=RED)
        if pl.get("hints"):
            doc.heading("Hints (informational, not failures)")
            doc.bullets([_msg(h) for h in pl["hints"]])

    # verification
    ver = record.get("verification")
    if isinstance(ver, dict):
        _verification_pdf(doc, np, ver)

    # warnings / profiles
    doc.heading("Warnings")
    doc.bullets([str(w) for w in (record.get("warnings") or [])])
    doc.heading("Saved profiles")
    doc.bullets([f"{p.get('id', '')}  type={p.get('type', '')}  v{p.get('version', '')}"
                 for p in (record.get("profiles") or [])])
    return doc.save(path)


def _band_rows(bands):
    rows, fills = [], []
    for b in bands or []:
        warn = bool(b.get("warn"))
        rows.append([_band_text(b), _num(b.get("n"), "{:.0f}", ""),
                     _num(b.get("mean_pct"), "{:+.2f} %", "n/a"),
                     _num(b.get("max_abs_pct"), "{:.2f} %", "n/a"),
                     ("WARN", RED) if warn else ("ok", DARK)])
        fills.append(WARN_FILL if warn else None)
    return rows, fills


def _verification_pdf(doc, np, ver: dict):
    ov = ver.get("overall") or {}
    doc.heading("Verification")
    doc.kv([("Result", ("passed" if ver.get("pass") else "FAILED") if ver.get("pass") is not None else "n/a"),
            ("Warn threshold", _num(ver.get("warn_pct"), "{:.3g} %", "n/a")),
            ("Normalised at", " - ".join(_num(x, "{:.0f}") for x in (ver.get("norm_range_nm") or [])) + " nm"),
            ("Band width", _num(ver.get("band_nm"), "{:.0f} nm", "n/a")),
            ("Reference", ver.get("reference_name") or "-"),
            ("Profile", ver.get("profile_label") or "-"),
            ("Measurement", ver.get("measurement_id") or "-"),
            ("t_int", _num(ver.get("integration_us"), "{:.0f} us", "n/a")),
            ("Overall RMS", _num(ov.get("rms_pct"), "{:.3g} %", "n/a")),
            ("Mean |dev|", _num(ov.get("mean_abs_pct"), "{:.3g} %", "n/a")),
            ("Max |dev|", _num(ov.get("max_abs_pct"), "{:.3g} %", "n/a"))])
    cur = ver.get("curve") or {}
    cw = np.array([np.nan if v is None else float(v) for v in (cur.get("wl") or [])], float)
    cd = np.array([np.nan if v is None else float(v) for v in (cur.get("deviation_pct") or [])], float)
    if len(cw) and len(cw) == len(cd) and np.isfinite(cd).any():
        doc.para("Deviation of the calibrated spectrum from the reference (relative)", 7.5, MUTED)
        a = doc.plot(100)
        a.plot(cw, cd, color=BLUE, lw=1)
        wp = ver.get("warn_pct")
        if wp is not None:
            for s in (1, -1):
                a.axhline(s * float(wp), color=ORANGE, ls="--", lw=0.8)
        a.axhline(0, color="#888888", lw=0.6)
        _style_axes(a, "wavelength (nm)", "deviation (%)")
    rows, fills = _band_rows(ver.get("bands"))
    if rows:
        doc.para("Per-band deviation (relative, normalised)", 7.5, MUTED, bold=True)
        doc.table(["Band", "Pixels", "Mean", "Max |dev|", "Status"], rows,
                  [2.5, 1, 1.5, 1.5, 1.2], row_fill=fills)
    ab = ver.get("absolute")
    if isinstance(ab, dict):
        aov = ab.get("overall") or {}
        doc.para(f"Absolute deviation: RMS {_num(aov.get('rms_pct'), '{:.3g} %', 'n/a')}, "
                 f"mean |dev| {_num(aov.get('mean_abs_pct'), '{:.3g} %', 'n/a')}, "
                 f"max |dev| {_num(aov.get('max_abs_pct'), '{:.3g} %', 'n/a')}", 7.5, DARK, bold=True)
        rows, fills = _band_rows(ab.get("bands"))
        if rows:
            doc.table(["Band", "Pixels", "Mean", "Max |dev|", "Status"], rows,
                      [2.5, 1, 1.5, 1.5, 1.2], row_fill=fills)


def write_protocol(record: dict, pdf_path: str | None = None,
                   csv_path: str | None = None) -> list[str]:
    out = []
    if csv_path:
        out.append(write_protocol_csv(record, csv_path))
    if pdf_path:
        out.append(write_protocol_pdf(record, pdf_path))
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Linearity protocol
# ══════════════════════════════════════════════════════════════════════════
def _lin_meta(record: dict) -> list[tuple[str, str]]:
    r = record or {}
    m = r.get("model") or {}
    lamp = r.get("lamp") or {}
    vrc = m.get("valid_range_counts") or []
    vrf = m.get("valid_range_frac") or []
    out = [
        ("started", r.get("started") or ""), ("finished", r.get("finished") or ""),
        ("device", _g(r, "device", "backend", default="")),
        ("serial", _g(r, "device", "serial", default="")),
        ("mode", r.get("mode") or ""),
        ("factory_linearisation", _onoff(r.get("factory_nl"))),
        ("ref_us", _num(r.get("ref_us"), "{:.0f}")),
        ("integration_us", ",".join(_num(t, "{:.0f}") for t in (r.get("integration_us") or []))),
        ("model_type", m.get("type") or ""), ("degree", _num(m.get("degree"), "{:.0f}")),
        ("coeffs", ",".join(_num(c, "{:.8g}") for c in (m.get("coeffs") or []))),
        ("full_scale_counts", _num(m.get("full_scale"), "{:.0f}")),
        ("valid_range_counts", " - ".join(_num(x, "{:.1f}") for x in vrc)),
        ("valid_range_pct_fs", " - ".join(_num(100 * float(x), "{:.2f}") for x in vrf)),
        ("rms_before_pct", _num(m.get("rms_before_pct"), "{:.4g}")),
        ("rms_after_pct", _num(m.get("rms_after_pct"), "{:.4g}")),
        ("max_deviation_pct", _num(m.get("max_deviation_pct"), "{:.4g}")),
        ("lamp_drift_pct", _num(lamp.get("drift_pct"), "{:.4g}")),
        ("saved_id", _g(r, "saved", "id", default="")),
        ("saved_created", _g(r, "saved", "created", default="")),
        ("activated", _yesno(r.get("activated"), "n/a")),
    ]
    for k, v in sorted((r.get("settings") or {}).items()):
        out.append((f"setting_{k}", str(v)))
    for i, d in enumerate(r.get("dark_drift") or [], 1):
        out.append((f"dark_drift_{i}",
                    f"t_int_us={_num(d.get('integration_us'), '{:.0f}')} "
                    f"mean_counts={_num(d.get('mean_counts'), '{:+.3f}')} "
                    f"p95_abs_counts={_num(d.get('p95_abs_counts'), '{:.3f}')} "
                    f"sigma_counts={_num(d.get('sigma_counts'), '{:.3f}')} "
                    f"seconds={_num(d.get('seconds'), '{:.1f}')}"))
    for i, w in enumerate(r.get("warnings") or [], 1):
        out.append((f"warning_{i}", str(w)))
    return out


def _curve_cols(record: dict):
    cv = (record or {}).get("curve") or {}
    return cv.get("x_counts") or [], cv.get("f_counts") or [], cv.get("deviation_pct") or []


def write_linearity_csv(record: dict, path: str) -> str:
    lines = ["# linearity protocol"]
    for k, v in _lin_meta(record):
        lines.append(f"# {k};{_csv_clean(v)}")
    xs, fs_, dv = _curve_cols(record)
    lines.append("# table;curve")
    lines.append(";".join(LINEARITY_CURVE_COLUMNS))
    for i in range(max(len(xs), len(fs_), len(dv))):
        lines.append(";".join(_cell(c[i] if i < len(c) else None) for c in (xs, fs_, dv)))
    lines.append("# table;steps")
    lines.append(";".join(LINEARITY_STEP_COLUMNS))
    for s in (record or {}).get("steps") or []:
        lines.append(";".join(_cell(s.get(k)) if k != "role" else str(s.get(k) or "")
                              for k in LINEARITY_STEP_COLUMNS))
    return _write_lines(path, lines)


def write_linearity_pdf(record: dict, path: str) -> str:
    import numpy as np

    r = record or {}
    meta = dict(_lin_meta(r))
    m = r.get("model") or {}
    fs_counts = m.get("full_scale")
    doc = _Doc("Linearity protocol")
    doc.title_block("Linearity protocol")
    mode = r.get("mode") or ""
    mode_txt = {"correction": "correction (creates a linearity correction)",
                "check": "check only (no correction is created)"}.get(mode, mode or "n/a")
    doc.kv([("Started", meta["started"]), ("Finished", meta["finished"]),
            ("Device", meta["device"]), ("Serial", meta["serial"] or "-"),
            ("Mode", mode_txt), ("Factory linear.", meta["factory_linearisation"]),
            ("Reference t_int", _num(r.get("ref_us"), "{:.0f} us", "n/a")),
            ("Activated", meta["activated"])])
    sett = r.get("settings") or {}
    doc.kv([("Settings", ", ".join(f"{k}={v}" for k, v in sorted(sett.items())) or "-"),
            ("Integration times", ", ".join(_num(t / 1000, "{:.4g}") for t in
                                            (r.get("integration_us") or [])) + " ms")], ncols=1)

    steps = r.get("steps") or []
    doc.heading("Steps")
    rows = []
    for s in steps:
        rows.append([str(s.get("role") or ""),
                     _num(None if s.get("t_us") is None else s["t_us"] / 1000, "{:.4g}", "n/a"),
                     _num(s.get("t_us"), "{:.0f}", "n/a"),
                     _num(s.get("peak_pct_fs"), "{:.1f}", "n/a"),
                     _num(s.get("lamp"), "{:.4f}", "n/a"),
                     _num(s.get("dev_raw_pct"), "{:+.3f}", "n/a"),
                     _num(s.get("dev_corr_pct"), "{:+.3f}", "n/a")])
    if rows:
        doc.table(["Role", "t_int (ms)", "t_int (us)", "Peak (% FS)", "Lamp factor",
                   "Dev. raw (%)", "Dev. corrected (%)"], rows, [1.1, 1.2, 1.2, 1.2, 1.3, 1.3, 1.6])
    else:
        doc.para("no steps recorded", 7.5, MUTED)

    xs, fc, dv = _curve_cols(r)
    X = np.array([np.nan if v is None else float(v) for v in xs], float)
    F = np.array([np.nan if v is None else float(v) for v in fc], float)
    D = np.array([np.nan if v is None else float(v) for v in dv], float)
    vrc = m.get("valid_range_counts")
    fsv = float(fs_counts) if fs_counts else None

    doc.heading("Deviation curve h(N) - 1")
    a = doc.plot(110, top_pad=24)
    if len(X) and len(D) == len(X) and np.isfinite(D).any():
        a.plot(X, D, color=BLUE, lw=1.3)
        a.axhline(0, color="#888888", lw=0.6)
        if vrc and len(vrc) == 2:
            a.axvspan(vrc[0], vrc[1], color="#eef6ee", alpha=0.7, lw=0)
        _style_axes(a, "counts", "h(N) - 1 (%)")
        if fsv:
            sx = a.secondary_xaxis("top", functions=(lambda v: v / fsv * 100,
                                                     lambda p: p * fsv / 100))
            sx.set_xlabel("% FS", fontsize=7.5)
            sx.tick_params(labelsize=7)
    else:
        doc.nodata(a)

    doc.heading("Correction f(N) - N")
    a = doc.plot(100)
    if len(X) and len(F) == len(X) and np.isfinite(F).any():
        a.plot(X, F - X, color=ORANGE, lw=1.3)
        a.axhline(0, color="#888888", lw=0.6)
        _style_axes(a, "counts (measured)", "f(N) - N (counts)")
    else:
        doc.nodata(a)

    # lamp + dark drift
    lamp = r.get("lamp") or {}
    lv = lamp.get("levels_rel") or []
    doc.heading("Lamp drift and dark drift")
    doc.para(f"Lamp drift over the run: {_num(lamp.get('drift_pct'), '{:.3f} %', 'n/a')} "
             f"(reference step interleaved, corrected)", 7.5)
    if lv:
        a = doc.plot(70)
        a.plot(range(1, len(lv) + 1), [100 * (float(v) - 1) if v is not None else np.nan for v in lv],
               "o-", color=BLUE, lw=1, ms=3)
        a.axhline(0, color="#888888", lw=0.6)
        _style_axes(a, "reference step #", "lamp (% vs start)")
    dd = r.get("dark_drift") or []
    if dd:
        drows = [[_num(None if d.get("integration_us") is None else d["integration_us"] / 1000,
                       "{:.4g} ms", "n/a"),
                  _num(d.get("mean_counts"), "{:+.2f}", "n/a"),
                  _num(d.get("p95_abs_counts"), "{:.2f}", "n/a"),
                  _num(d.get("sigma_counts"), "{:.2f}", "n/a"),
                  _num(d.get("seconds"), "{:.1f} s", "n/a")] for d in dd]
        doc.table(["t_int", "Mean (counts)", "p95 |d| (counts)", "Noise (counts)", "Time between"],
                  drows, [1.2, 1.4, 1.6, 1.4, 1.4])
    else:
        doc.para("no dark drift recorded", 7.5, MUTED)

    # model
    doc.heading("Model")
    vrf = m.get("valid_range_frac") or []
    doc.kv([("Type", m.get("type") or "-"), ("Degree", _num(m.get("degree"), "{:.0f}", "-")),
            ("Full scale", _num(fs_counts, "{:.0f} counts", "-")),
            ("Valid range", (" - ".join(_num(x, "{:.0f}") for x in vrc) + " counts") if vrc else "-"),
            ("Valid range (% FS)",
             (" - ".join(_num(100 * float(x), "{:.1f}") for x in vrf) + " % FS") if vrf else "-"),
            ("RMS before", _num(m.get("rms_before_pct"), "{:.3g} %", "-")),
            ("RMS after", _num(m.get("rms_after_pct"), "{:.3g} %", "-")),
            ("Max deviation", _num(m.get("max_deviation_pct"), "{:.3g} %", "-"))])
    doc.kv([("Coefficients", ", ".join(_num(c, "{:.6g}") for c in (m.get("coeffs") or [])) or "-")],
           ncols=1)

    doc.heading("Warnings")
    doc.bullets([str(w) for w in (r.get("warnings") or [])])
    doc.heading("Saved record")
    sv = r.get("saved")
    if sv:
        doc.para(f"id {sv.get('id', '')}, mode {sv.get('mode', '')}, created "
                 f"{sv.get('created', '')}; activated: {_yesno(r.get('activated'), 'n/a')}")
    else:
        doc.para("not saved", 7.5, MUTED)
    return doc.save(path)


def write_linearity_protocol(record: dict, pdf_path: str | None = None,
                             csv_path: str | None = None) -> list[str]:
    out = []
    if csv_path:
        out.append(write_linearity_csv(record, csv_path))
    if pdf_path:
        out.append(write_linearity_pdf(record, pdf_path))
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Derivation protocol (DerivationWorkflow.record())
# ══════════════════════════════════════════════════════════════════════════
DERIVATION_CSV_COLUMNS = ["Wavelength_nm", "C_known_cps", "C_known_repeat_cps",
                          "C_known_used_cps", "C_new_cps", "S_known", "S_new", "Valid",
                          "Drift_pct", "Drift_fail"]
_DERIV_SIDES = (("known", "Known optics"), ("new", "New optics"))


def default_derivation_basename(record: dict) -> str:
    record = record or {}
    parts = ["derivation", _slug(_g(record, "device", "backend"), "device"),
             _slug(_g(record, "device", "serial"), "noserial"),
             _slug(_g(record, "optics", "name"), "optics"),
             _slug(record.get("type"), "type"), _stamp(record)]
    return re.sub(r"[^A-Za-z0-9_-]", "", "_".join(parts))


def _pct(v, fmt="{:.2f}"):
    return _num(None if v is None else 100 * float(v), fmt)


def _exposure_text(side: dict) -> str:
    a = side.get("auto_exposure")
    if a is None:
        return ""
    txt = "auto exposure" if a else "fixed"
    if a and side.get("converged") is False:
        txt += " (target band not reached)"
    return txt


def _limit_rows(r: dict) -> list[tuple[str, str]]:
    """Configurable test limits of the run (from record["settings"])."""
    st = (r or {}).get("settings") or {}
    return [("itime_match_tol_pct", _num(st.get("itime_match_tol_pct"), "{:.3g}")),
            ("dark_bright_frac", _num(st.get("dark_bright_frac"), "{:.3g}"))]


def _limit_text(r: dict) -> str:
    st = (r or {}).get("settings") or {}
    return (f"same integration time within {_num(st.get('itime_match_tol_pct'), '{:.3g}', 'n/a')} %; "
            f"dark unusually bright above {_num(st.get('dark_bright_frac'), '{:.3g}', 'n/a')} x light level")


def _deriv_metadata(record: dict) -> list[tuple[str, str]]:
    r = record or {}
    acq, chk = r.get("acquisition") or {}, r.get("checks") or {}
    kp = r.get("known_profile") or {}
    dr = r.get("drift") or {}
    sides = r.get("sides") or {}
    vr = r.get("valid_range_nm")
    gs = r.get("gain_span")
    gs_s = (" - ".join(_num(x, "{:.4g}") for x in gs) if isinstance(gs, (list, tuple))
            else _num(gs, "{:.4g}"))
    m = [("started", r.get("started") or ""), ("finished", r.get("finished") or ""),
         ("device", _g(r, "device", "backend", default="")),
         ("serial", _g(r, "device", "serial", default="")),
         ("new_optics_name", _g(r, "optics", "name", default="")),
         ("new_optics_input", _g(r, "optics", "input", default="")),
         ("new_optics_notes", _g(r, "optics", "notes", default="")),
         ("known_profile_id", kp.get("id") or ""),
         ("known_profile_version", _num(kp.get("version"), "{:.0f}")),
         ("known_profile_type", kp.get("type") or ""),
         ("known_optics_name", _g(kp, "optics", "name", default="")),
         ("known_optics_input", _g(kp, "optics", "input", default="")),
         ("known_reference_name", _g(kp, "reference", "name", default="")),
         ("type", r.get("type") or ""),
         ("quantity", r.get("quantity") or ""),
         ("source_hint", r.get("source_hint") or "")]
    for s, _label in _DERIV_SIDES:
        sd = sides.get(s) or {}
        t = sd.get("integration_us")
        pk, pr = sd.get("peak_frac"), sd.get("peak_frac_repeat")
        lin = sd.get("linearity") if isinstance(sd.get("linearity"), dict) else {}
        m += [(f"t_int_{s}_us", _num(t, "{:.0f}")),
              (f"t_int_{s}_ms", _num(None if t is None else float(t) / 1000, "{:.4g}")),
              (f"t_int_{s}_mode", _exposure_text(sd)),
              (f"peak_{s}_pct_fs", _pct(pk, "{:.1f}"))]
        if s == "known":
            m.append(("peak_known_repeat_pct_fs", _pct(pr, "{:.1f}")))
        m += [(f"saturated_pixels_{s}", _num(sd.get("n_saturated"), "{:.0f}")),
              (f"dark_sequence_{s}", sd.get("dark_sequence") or ""),
              (f"dark_light_same_t_int_{s}", _yesno(sd.get("itime_match"))),
              (f"linearity_correction_{s}", _onoff(sd.get("linearity_text"))),
              (f"linearity_id_{s}", lin.get("id") or "")]
    m += [("averages_N", _num(acq.get("averages"), "{:.0f}")),
          ("nl_correction", _nl_text(acq)),
          ("drift_pct_p95", _num(dr.get("p95_pct"), "{:.3f}")),
          ("drift_pct_mean", _num(dr.get("mean_pct"), "{:.3f}")),
          ("drift_pct_median", _num(dr.get("median_pct"), "{:.3f}")),
          ("drift_threshold_pct", _num(dr.get("threshold_pct"), "{:.3f}")),
          ("drift_pass", _yesno(dr.get("pass"), "")),
          ("drift_masked_pixels", _num(dr.get("n_fail_pixels"), "{:.0f}")),
          ("drift_pixels_tested", _num(dr.get("n_tested"), "{:.0f}")),
          ("drift_seconds", _num(dr.get("seconds"), "{:.1f}")),
          # weight w of the repeat in C_known_used = (1-w)·C_known + w·C_known_repeat
          ("drift_interp_weight", _num(dr.get("interp_weight"), "{:.3f}")),
          ("drift_cause", dr.get("cause") or ""),
          ("valid_range_nm", "" if not vr else " - ".join(_num(x, "{:.1f}") for x in vr)),
          ("valid_segments_nm", _segments_text(r.get("valid_segments_nm"))),
          ("gain_span", gs_s), ("n_valid", _num(r.get("n_valid"), "{:.0f}")),
          ("unit", r.get("unit") or ""),
          ("wl_fingerprint", r.get("wl_fingerprint") or "")]
    m += _limit_rows(r)
    if r.get("drift_status"):
        m.append(("drift_status", str(r["drift_status"].get("message") or "")))
    for key in ("itime_match_known", "itime_match_new",
                "linearity_consistent", "broadband_pass"):
        if chk.get(key) is not None:
            m.append((f"check_{key}", _yesno(chk.get(key))))
    for i, d in enumerate(r.get("dark_drift") or [], 1):
        m.append((f"dark_drift_{i}",
                  f"side={d.get('side', '')} "
                  f"t_int_us={_num(d.get('integration_us'), '{:.0f}')} "
                  f"mean_counts={_num(d.get('mean_counts'), '{:+.3f}')} "
                  f"pct_of_peak={_num(d.get('pct_of_peak'), '{:.3f}')} "
                  f"warn={_yesno(d.get('warn'), 'no')}"))
    pl = r.get("plausibility") or {}
    if pl:
        met, lim = pl.get("metrics") or {}, pl.get("limits") or {}
        m += [("plausibility_pass", _yesno(pl.get("pass"))),
              ("plausibility_override", _yesno(pl.get("override"), "no")),
              ("coverage_pct", _pct(met.get("coverage"), "{:.1f}")),
              ("min_coverage_pct", _pct(lim.get("min_coverage"), "{:.1f}")),
              ("max_gain_span", _num(lim.get("max_gain_span"), "{:.4g}")),
              ("roughness_pct", _pct(met.get("roughness"), "{:.2f}")),
              ("max_roughness_pct", _pct(lim.get("max_roughness"), "{:.2f}"))]
        for i, f in enumerate(pl.get("failures") or [], 1):
            m.append((f"plausibility_failure_{i}", _msg(f)))
        for i, h in enumerate(pl.get("hints") or [], 1):
            m.append((f"plausibility_hint_{i}", _msg(h)))
    for i, w in enumerate(r.get("warnings") or [], 1):
        m.append((f"warning_{i}", str(w)))
    for i, p in enumerate(r.get("profiles") or [], 1):
        m.append((f"profile_{i}", f"{p.get('id', '')} type={p.get('type', '')} "
                                  f"version={p.get('version', '')}"))
    return m


def _dcell(v) -> str:
    """CSV cell: empty for missing / non-finite values (never 'nan')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return repr(f) if f == f and abs(f) != float("inf") else ""


def write_derivation_csv(record: dict, path: str) -> str:
    sp = _spectra(record)
    wl = sp.get("wl") or []
    valid = sp.get("valid")
    lines = ["# derivation protocol"]
    for k, v in _deriv_metadata(record):
        lines.append(f"# {k};{_csv_clean(v)}")
    lines.append(";".join(DERIVATION_CSV_COLUMNS))

    def col(key, i):
        c = sp.get(key)
        return c[i] if c is not None and i < len(c) else None
    for i in range(len(wl)):
        ok = True if valid is None or i >= len(valid) else bool(valid[i])
        s_new = col("S", i) if ok else None
        lines.append(";".join([
            _dcell(wl[i]), _dcell(col("C_known", i)), _dcell(col("C_known_repeat", i)),
            _dcell(col("C_known_used", i)), _dcell(col("C_new", i)), _dcell(col("S_known", i)), _dcell(s_new),
            _dcell(ok if valid is not None else None),
            _dcell(col("drift_pct", i)), _dcell(col("drift_fail", i))]))
    return _write_lines(path, lines)


def _norm_max(np, a, valid=None):
    a = np.asarray(a, float)
    ref = a[valid] if valid is not None and valid.any() else a
    ref = ref[np.isfinite(ref)]
    m = float(np.max(np.abs(ref))) if ref.size else 0.0
    return a / m if m > 0 else a


def write_derivation_pdf(record: dict, path: str) -> str:
    import numpy as np

    record = record or {}
    sp = _spectra(record)
    wl = np.array([np.nan if v is None else float(v) for v in (sp.get("wl") or [])], float)
    n = len(wl)
    ck, cr, cn = (_arr(np, sp.get(k), n) for k in ("C_known", "C_known_repeat", "C_new"))
    sk, sn = _arr(np, sp.get("S_known"), n), _arr(np, sp.get("S"), n)
    dp = _arr(np, sp.get("drift_pct"), n)
    vl, df = sp.get("valid"), sp.get("drift_fail")
    valid = np.zeros(n, bool) if vl is None else np.array(
        [bool(v) for v in vl] + [False] * max(0, n - len(vl)), bool)[:n]
    failpx = np.zeros(n, bool) if df is None else np.array(
        [bool(v) for v in df] + [False] * max(0, n - len(df)), bool)[:n]
    segs = record.get("valid_segments_nm") or []
    meta = dict(_deriv_metadata(record))
    sides = record.get("sides") or {}
    dr = record.get("drift") or {}
    chk = record.get("checks") or {}
    acq = record.get("acquisition") or {}

    def shade(ax):
        if n and segs:
            ax.axvspan(np.nanmin(wl), np.nanmax(wl), color=GREY, alpha=0.6, lw=0)
            for lo, hi in segs:
                ax.axvspan(float(lo), float(hi), color="#eef6ee", alpha=1.0, lw=0)

    doc = _Doc("Derivation protocol")
    doc.title_block("Derivation protocol (optics derived from a known profile)")
    doc.kv([("Started", meta["started"]), ("Finished", meta["finished"]),
            ("Device", meta["device"]), ("Serial", meta["serial"] or "-"),
            ("New optics", f"{meta['new_optics_name']} ({meta['new_optics_input']})"),
            ("Known profile", f"{meta['known_profile_id'] or '-'} v{meta['known_profile_version'] or '-'}"),
            ("Known optics", f"{meta['known_optics_name']} ({meta['known_optics_input']})"),
            ("Type / unit", f"{_type_text(meta['type'], meta['quantity'])} / "
                            f"{_unit_text(meta['unit'])}"),
            ("Averages N", meta["averages_N"]), ("NL correction", meta["nl_correction"]),
            ("Valid range", f"{meta['valid_range_nm']} nm" if meta["valid_range_nm"] else "-"),
            ("Gain span (hint)", meta["gain_span"] or "-"),
            ("Valid pixels", meta["n_valid"] or "-")])
    doc.kv([("New optics notes", meta["new_optics_notes"] or "-"),
            ("Valid segments", (meta["valid_segments_nm"] + " nm") if meta["valid_segments_nm"] else "-"),
            ("WL fingerprint", meta["wl_fingerprint"] or "-"),
            ("Limits", _limit_text(record))], ncols=1)
    doc.heading("Source")
    doc.para(meta["source_hint"] or "-")

    # per-side acquisition
    doc.heading("Acquisition per optics")
    kn, nw = sides.get("known") or {}, sides.get("new") or {}

    def both(fn):
        return [fn(kn), fn(nw)]
    rows = [
        ["Integration time"] + both(lambda s: _num(
            None if s.get("integration_us") is None else s["integration_us"] / 1000,
            "{:.4g} ms", "n/a")),
        ["Setting"] + both(lambda s: _exposure_text(s) or "n/a"),
        ["Level, light capture"] + both(lambda s: _pct(s.get("peak_frac"), "{:.1f} % FS") or "n/a"),
        ["Level, repeat"] + [_pct(kn.get("peak_frac_repeat"), "{:.1f} % FS") or "n/a", "-"],
        ["Saturated pixels"] + both(lambda s: _num(s.get("n_saturated"), "{:.0f}", "n/a")),
        ["Dark sequence"] + both(lambda s: s.get("dark_sequence") or "n/a"),
        ["Dark and light at the same integration time"] + both(
            lambda s: _status(s.get("itime_match"), "yes", "NO")),
        ["Linearity correction"] + both(lambda s: _onoff(s.get("linearity_text"))),
        ["NL correction (device)", _nl_text(acq), _nl_text(acq)],
    ]
    doc.table(["Parameter", "Known optics", "New optics"], rows, [3.2, 2.4, 2.4])

    # plot 1: count rates, normalised
    doc.heading("Count rates (normalised to the maximum)")
    a1 = doc.plot(115)
    if n and np.isfinite(ck).any():
        shade(a1)
        a1.plot(wl, _norm_max(np, ck, valid), color=BLUE, lw=1.2, label="C_known")
        if np.isfinite(cr).any():
            a1.plot(wl, _norm_max(np, cr, valid), color=DARK, lw=0.8, ls="--",
                    label="C_known (repeat)")
        a1.plot(wl, _norm_max(np, cn, valid), color=ORANGE, lw=1.2, label="C_new")
        a1.legend(fontsize=7, loc="upper right")
        _style_axes(a1, "wavelength (nm)", "C (normalised)")
    else:
        doc.nodata(a1)

    # plot 2: S
    doc.heading("Sensitivity (valid pixels only)")
    a2 = doc.plot(115)
    if n and np.isfinite(sn).any():
        shade(a2)
        a2.plot(wl, np.where(valid, sk, np.nan), color=BLUE, lw=1.2, label="S_known")
        a2.plot(wl, np.where(valid, sn, np.nan), color=ORANGE, lw=1.3, label="S_new")
        a2.legend(fontsize=7, loc="upper right")
        _style_axes(a2, "wavelength (nm)", "S (relative)")
    else:
        doc.nodata(a2)

    # source drift
    doc.heading("Source drift (repeat with the known optics; dashed: +/- limit; "
                "red ticks: masked pixels)")
    a3 = doc.plot(95)
    if n and np.isfinite(dp).any():
        shade(a3)
        a3.plot(wl, dp, color=BLUE, lw=1, label="drift per pixel")
        th = dr.get("threshold_pct")
        if th is not None:
            for s in (1, -1):
                a3.axhline(s * float(th), color=ORANGE, ls="--", lw=0.8)
            lo, hi = a3.get_ylim()
            a3.set_ylim(min(lo, -1.2 * float(th)), max(hi, 1.2 * float(th)))
        a3.axhline(0, color="#888888", lw=0.6)
        if failpx.any():
            a3.plot(wl[failpx], np.full(int(failpx.sum()), 0.02), "|", color=RED, ms=7,
                    transform=a3.get_xaxis_transform(), label="masked pixel")
        a3.legend(fontsize=7, loc="upper right")
        _style_axes(a3, "wavelength (nm)", "drift (%)")
    else:
        doc.nodata(a3, "no repeat measurement")

    doc.heading("Checks")
    if dr:
        drows = [[_num(dr.get("p95_pct"), "{:.2f} %", "n/a"),
                  _num(dr.get("mean_pct"), "{:+.2f} %", "n/a"),
                  _num(dr.get("threshold_pct"), "{:.2f} %", "n/a"),
                  _status(dr.get("pass")),
                  f"{_num(dr.get('n_fail_pixels'), '{:.0f}', 'n/a')} of "
                  f"{_num(dr.get('n_tested'), '{:.0f}', 'n/a')}",
                  _num(dr.get("seconds"), "{:.0f} s", "n/a")]]
        doc.table(["Drift (p95)", "Mean", "Limit", "Result", "Masked pixels", "Duration"],
                  drows, [1.4, 1.2, 1.2, 1.2, 1.6, 1.2],
                  row_fill=[WARN_FILL if dr.get("pass") is False else None])
        ds = record.get("drift_status") or {}
        if ds.get("message"):
            doc.para(str(ds["message"]), 8.5, RED)
        doc.para(f"Cause: {dr.get('cause') or 'n/a'}")
    else:
        doc.para("Source drift was not checked (no repeat with the known optics).",
                 7.5, RED)
    pl = record.get("plausibility") or {}
    crow = [["Dark and light, known optics", _status(chk.get("itime_match_known"), "yes", "NO")],
            ["Dark and light, new optics", _status(chk.get("itime_match_new"), "yes", "NO")],
            ["Linearity correction unchanged", _status(chk.get("linearity_consistent"), "yes", "NO")]]
    if pl:
        crow.append(["Broadband / coverage",
                     ("passed", DARK) if pl.get("pass") else (
                         ("FAILED (saved on request)", RED) if pl.get("override")
                         else ("FAILED", RED))])
    doc.table(["Check", "Result"], crow, [4, 2])

    dd = record.get("dark_drift") or []
    if dd:
        doc.heading("Dark drift (dark before / after, per optics)")
        rws, fills = [], []
        for d in dd:
            warn = bool(d.get("warn"))
            rws.append([dict(_DERIV_SIDES).get(d.get("side"), str(d.get("side") or "")),
                        _num(None if d.get("integration_us") is None
                             else d["integration_us"] / 1000, "{:.4g} ms", "n/a"),
                        _num(d.get("mean_counts"), "{:+.2f}", "n/a"),
                        _num(d.get("pct_of_peak"), "{:.3f} %", "n/a"),
                        ("WARN", RED) if warn else ("ok", DARK)])
            fills.append(WARN_FILL if warn else None)
        doc.table(["Optics", "t_int", "Mean (counts)", "% of peak", "Status"], rws,
                  [2, 1.4, 1.6, 1.4, 1.2], row_fill=fills)
    if pl:
        if pl.get("failures"):
            doc.heading("Plausibility failures")
            doc.bullets([_msg(f) for f in pl["failures"]], color=RED)
        if pl.get("hints"):
            doc.heading("Hints (informational, not failures)")
            doc.bullets([_msg(h) for h in pl["hints"]])
    doc.heading("Warnings")
    doc.bullets([str(w) for w in (record.get("warnings") or [])])
    doc.heading("Saved profiles")
    doc.bullets([f"{p.get('id', '')}  type={p.get('type', '')}  v{p.get('version', '')}"
                 for p in (record.get("profiles") or [])])
    return doc.save(path)


def write_derivation_protocol(record: dict, pdf_path: str | None = None,
                              csv_path: str | None = None) -> list[str]:
    out = []
    if csv_path:
        out.append(write_derivation_csv(record, csv_path))
    if pdf_path:
        out.append(write_derivation_pdf(record, pdf_path))
    return out


# ══════════════════════════════════════════════════════════════════════════
#  Sphere characterisation protocol (radiance reference)
# ══════════════════════════════════════════════════════════════════════════
def default_sphere_basename(record: dict) -> str:
    record = record or {}
    parts = ["sphere", _slug(_g(record, "sphere", "name"), "sphere"),
             _slug(_g(record, "device", "serial"), "noserial"), _stamp(record)]
    return re.sub(r"[^A-Za-z0-9_-]", "", "_".join(parts))


def _sphere_limit_rows(r: dict) -> list[tuple[str, str]]:
    """All configurable limits of the run (record["settings"]) with their values."""
    st = (r or {}).get("settings") or {}
    out = []
    for k, v in st.items():
        if isinstance(v, (list, tuple)):
            out.append((k, " - ".join(_num(x, "{:.4g}") for x in v)))
        else:
            out.append((k, _num(v, "{:.6g}")))
    return out


def _range_text(x) -> str:
    return "" if not x else " - ".join(_num(v, "{:.1f}") for v in x)


def _sphere_metadata(record: dict) -> list[tuple[str, str]]:
    r = record or {}
    sp, cp_ = r.get("sphere") or {}, r.get("cc_profile") or {}
    acq, dr, ov = r.get("acquisition") or {}, r.get("drift") or {}, r.get("overall") or {}
    un, src = r.get("uncertainty") or {}, r.get("source") or {}
    m = [("started", r.get("started") or ""), ("finished", r.get("finished") or ""),
         ("device", _g(r, "device", "backend", default="")),
         ("serial", _g(r, "device", "serial", default="")),
         ("sphere_name", sp.get("name") or ""), ("sphere_serial", sp.get("serial") or ""),
         ("port_radius_mm", _num(sp.get("port_radius_mm"), "{:.4g}")),
         ("port_diameter_mm", _num(sp.get("port_diameter_mm"), "{:.4g}")),
         ("edge_depth_mm", _num(sp.get("edge_depth_mm"), "{:.4g}")),
         ("cc_ref_offset_mm", _num(sp.get("cc_ref_offset_mm"), "{:.4g}")),
         ("u_radius_mm", _num(sp.get("u_radius_mm"), "{:.4g}")),
         ("u_distance_mm", _num(sp.get("u_distance_mm"), "{:.4g}")),
         ("u_cc_pct", _num(sp.get("cc_uncertainty_pct"), "{:.4g}")),
         ("sphere_notes", sp.get("notes") or ""),
         ("cc_profile_id", cp_.get("id") or ""),
         ("cc_profile_version", _num(cp_.get("version"), "{:.0f}")),
         ("cc_optics_name", _g(cp_, "optics", "name", default="")),
         ("cc_optics_input", _g(cp_, "optics", "input", default="")),
         ("cc_reference_name", _g(cp_, "reference", "name", default="")),
         ("cc_valid_range_nm", _range_text(cp_.get("valid_range_nm"))),
         ("quantity", r.get("quantity") or ""),
         ("unit", r.get("unit") or ""), ("irradiance_unit", r.get("irradiance_unit") or ""),
         ("source_hint", r.get("source_hint") or ""),
         ("formula", r.get("formula") or ""),
         ("t_int_us", _num(acq.get("integration_us"), "{:.0f}")),
         ("averages_N", _num(acq.get("averages"), "{:.0f}")),
         ("dark_sequence", acq.get("dark_sequence") or ""),
         ("nl_correction", _nl_text(acq)),
         ("wl_fingerprint", r.get("wl_fingerprint") or ""),
         ("valid_range_nm", _range_text(r.get("valid_range_nm"))),
         ("method", r.get("method") or ""),
         ("d0_mm", _num(r.get("d0_mm"), "{:+.4f}")),
         ("d0_plausible", _yesno(r.get("d0_plausible"), "")),
         ("d0_max_mm", _num(r.get("d0_max_mm"), "{:.4g}")),
         ("consistency_spread_pct", _num(ov.get("spread_pct"), "{:.4g}")),
         ("consistency_spread_nominal_pct", _num(ov.get("spread_nominal_pct"), "{:.4g}")),
         ("consistency_max_pct", _num(r.get("consistency_max_pct"), "{:.4g}")),
         ("consistency_pass", _yesno(r.get("consistency_pass"), "")),
         ("fit_rms_pct", _num(ov.get("fit_rms_pct"), "{:.4g}")),
         ("drift_pct_p95", _num(dr.get("p95_pct"), "{:.3f}")),
         ("drift_pct_mean", _num(dr.get("mean_pct"), "{:+.3f}")),
         ("drift_limit_pct", _num(dr.get("limit_pct"), "{:.3f}")),
         ("drift_pass", _yesno(dr.get("pass"), "")),
         ("drift_seconds", _num(dr.get("seconds"), "{:.1f}"))]
    for key in ("u_rel_r_pct", "u_rel_d_pct", "u_rel_spread_median_pct", "u_rel_drift_pct",
                "u_rel_cc_pct", "u_rel_median_pct", "u_rel_max_pct", "U_rel_median_pct_k2"):
        m.append((key, _num(un.get(key), "{:.4g}")))
    for i, p in enumerate(r.get("positions") or [], 1):
        m.append((f"position_{i}",
                  f"height_mm={_num(p.get('height_mm'), '{:.4g}')} "
                  f"d_mm={_num(p.get('d_mm'), '{:.4f}')} "
                  f"d_eff_mm={_num(p.get('d_eff_mm'), '{:.4f}')} "
                  f"peak_pct_fs={_pct(p.get('peak_frac'), '{:.1f}')} "
                  f"captured={p.get('captured') or ''}"))
    for i, b in enumerate(r.get("bands") or [], 1):
        m.append((f"band_{i}",
                  f"{_num(b.get('lo'), '{:.0f}')}-{_num(b.get('hi'), '{:.0f}')} nm "
                  f"n={_num(b.get('n'), '{:.0f}')} "
                  f"spread_pct={_num(b.get('spread_pct'), '{:.3f}')} "
                  f"spread_nominal_pct={_num(b.get('spread_nominal_pct'), '{:.3f}')} "
                  f"d0_mm={_num(b.get('d0_mm'), '{:+.3f}')} "
                  f"u_rel_pct={_num(b.get('u_rel_pct'), '{:.3f}')} "
                  f"pass={_yesno(b.get('pass'), 'n/a')}"))
    m += _sphere_limit_rows(r)
    if src:
        m += [("result_source_id", src.get("id") or ""),
              ("result_source_name", src.get("name") or ""),
              ("result_source_unit", src.get("unit") or ""),
              ("result_source_quantity", src.get("quantity") or ""),
              ("result_source_valid_min_nm", _num(src.get("valid_min_nm"), "{:.1f}")),
              ("result_source_valid_max_nm", _num(src.get("valid_max_nm"), "{:.1f}"))]
    for i, w in enumerate(r.get("warnings") or [], 1):
        m.append((f"warning_{i}", str(w)))
    return m


def write_sphere_csv(record: dict, path: str) -> str:
    sp = _spectra(record)
    wl = sp.get("wl") or []
    E, Li = sp.get("E") or [], sp.get("L_i") or []
    lines = ["# sphere characterisation protocol"]
    for k, v in _sphere_metadata(record):
        lines.append(f"# {k};{_csv_clean(v)}")
    cols = ["wavelength_nm"] + [f"E_{i}_uW_cm2_nm" for i in range(1, len(E) + 1)]
    cols += ["E_repeat_uW_cm2_nm"] + [f"L_{i}_uW_cm2_sr_nm" for i in range(1, len(Li) + 1)]
    cols += ["L_fit", "L_mean", "L_used", "spread_pct", "u_rel_pct", "valid"]
    lines.append(";".join(cols))

    def at(a, i):
        return a[i] if a is not None and i < len(a) else None
    for i in range(len(wl)):
        row = [_dcell(wl[i])] + [_dcell(at(e, i)) for e in E]
        row.append(_dcell(at(sp.get("E_repeat"), i)))
        row += [_dcell(at(x, i)) for x in Li]
        row += [_dcell(at(sp.get("L_fit"), i)), _dcell(at(sp.get("L_mean"), i)),
                _dcell(at(sp.get("L_smoothed"), i)), _dcell(at(sp.get("spread_pct"), i)),
                _dcell(at(sp.get("u_rel_pct"), i)), _dcell(at(sp.get("valid"), i))]
        lines.append(";".join(row))
    return _write_lines(path, lines)


def write_sphere_pdf(record: dict, path: str) -> str:
    import numpy as np

    record = record or {}
    sp = _spectra(record)
    wl = np.array([np.nan if v is None else float(v) for v in (sp.get("wl") or [])], float)
    n = len(wl)
    meta = dict(_sphere_metadata(record))
    unit_l = _unit_text(record.get("unit"))
    e_unit_l = _unit_text(record.get("irradiance_unit"))
    sph, cpr = record.get("sphere") or {}, record.get("cc_profile") or {}
    dr, ov = record.get("drift") or {}, record.get("overall") or {}
    un, src = record.get("uncertainty") or {}, record.get("source") or {}
    pos = record.get("positions") or []
    E = [_arr(np, e, n) for e in (sp.get("E") or [])]
    Li = [_arr(np, x, n) for x in (sp.get("L_i") or [])]
    Er, Lu = _arr(np, sp.get("E_repeat"), n), _arr(np, sp.get("L_smoothed"), n)
    if not np.isfinite(Lu).any():
        Lu = _arr(np, sp.get("L"), n)
    spread, urel = _arr(np, sp.get("spread_pct"), n), _arr(np, sp.get("u_rel_pct"), n)
    vr = record.get("valid_range_nm")
    cmap = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#17becf", "#e377c2"]

    def lab(i):
        p = pos[i] if i < len(pos) else {}
        return f"{i + 1}: h = {_num(p.get('height_mm'), '{:.4g}', '?')} mm"

    doc = _Doc("Sphere characterisation protocol")
    doc.title_block("Sphere characterisation (radiance reference)")
    t_ms = None if not meta["t_int_us"] else float(meta["t_int_us"]) / 1000
    doc.kv([("Started", meta["started"]), ("Finished", meta["finished"]),
            ("Device", meta["device"]), ("Serial", meta["serial"] or "-"),
            ("Quantity / unit", f"{record.get('quantity') or '-'} / {unit_l or '-'}"),
            ("Irradiance unit", e_unit_l or "-"),
            ("Integration time", _num(t_ms, "{:.4g} ms", "-")),
            ("Averages N", meta["averages_N"]), ("NL correction", meta["nl_correction"]),
            ("Dark sequence", meta["dark_sequence"] or "-"),
            ("Valid range", f"{meta['valid_range_nm']} nm" if meta["valid_range_nm"] else "-"),
            ("WL fingerprint", meta["wl_fingerprint"] or "-")])

    doc.heading("Sphere and geometry")
    doc.kv([("Sphere", meta["sphere_name"] or "-"),
            ("Identifier", meta["sphere_serial"] or "-"),
            ("Port radius", _num(sph.get("port_radius_mm"), "{:.4g} mm", "-")),
            ("Port diameter", _num(sph.get("port_diameter_mm"), "{:.4g} mm", "-")),
            ("Knife-edge to housing top", _num(sph.get("edge_depth_mm"), "{:.4g} mm", "-")),
            ("CC reference plane", _num(sph.get("cc_ref_offset_mm"),
                                        "{:+.4g} mm (rel. housing top)", "-")),
            ("u(r)", _num(sph.get("u_radius_mm"), "{:.4g} mm", "-")),
            ("u(d)", _num(sph.get("u_distance_mm"), "{:.4g} mm", "-")),
            ("u(CC)", _num(sph.get("cc_uncertainty_pct"), "{:.4g} %", "-")),
            ("Notes", sph.get("notes") or "-")])
    doc.heading("CC profile")
    doc.kv([("Profile", f"{cpr.get('id') or '-'} v{meta['cc_profile_version'] or '-'}"),
            ("Optics", f"{meta['cc_optics_name'] or '-'} ({meta['cc_optics_input'] or '-'})"),
            ("Reference", meta["cc_reference_name"] or "-"),
            ("Valid range", f"{meta['cc_valid_range_nm']} nm" if meta["cc_valid_range_nm"] else "-")])
    doc.heading("Model")
    doc.para(record.get("formula") or "-")
    if record.get("source_hint"):
        doc.para(str(record["source_hint"]))

    doc.heading("Positions")
    rows = []
    for i, p in enumerate(pos, 1):
        rows.append([str(i), _num(p.get("height_mm"), "{:.4g}", "-"),
                     _num(p.get("d_mm"), "{:.4f}", "-"), _num(p.get("d_eff_mm"), "{:.4f}", "-"),
                     _pct(p.get("peak_frac"), "{:.1f} % FS") or "-", p.get("captured") or "-"])
    doc.table(["#", "Height (mm)", "d (mm)", "d_eff (mm)", "Level", "Captured"], rows,
              [0.5, 1.2, 1.2, 1.2, 1.2, 3.2])

    doc.heading("Fit and consistency")
    doc.kv([("Offset d0", _num(record.get("d0_mm"), "{:+.3f} mm", "n/a")),
            ("d0 plausible", _yesno(record.get("d0_plausible"))),
            ("Limit |d0|", _num(record.get("d0_max_mm"), "{:.4g} mm", "n/a")),
            ("Method", {"fit": "fit (d0 applied)", "mean": "mean over distances (nominal)"}
             .get(record.get("method"), record.get("method") or "n/a")),
            ("Spread overall", _num(ov.get("spread_pct"), "{:.3f} %", "n/a")),
            ("Spread, nominal geometry", _num(ov.get("spread_nominal_pct"), "{:.3f} %", "n/a")),
            ("Limit spread", _num(record.get("consistency_max_pct"), "{:.4g} %", "n/a")),
            ("Consistency", _status(record.get("consistency_pass"))[0])])
    brows, bfill = [], []
    for b in record.get("bands") or []:
        ok = b.get("pass")
        brows.append([f"{_num(b.get('lo'), '{:.0f}', '?')}-{_num(b.get('hi'), '{:.0f}', '?')} nm",
                      _num(b.get("n"), "{:.0f}", "-"), _num(b.get("spread_pct"), "{:.3f}", "-"),
                      _num(b.get("spread_nominal_pct"), "{:.3f}", "-"),
                      _num(b.get("d0_mm"), "{:+.3f}", "-"), _num(b.get("u_rel_pct"), "{:.3f}", "-"),
                      _status(ok)])
        bfill.append(WARN_FILL if ok is False else None)
    if brows:
        doc.table(["Band", "Pixels", "Spread (%)", "Spread nominal (%)", "d0 (mm)",
                   "u_rel (%)", "Result"], brows, [1.8, 0.9, 1.1, 1.5, 1.1, 1.1, 1.1],
                  row_fill=bfill)

    doc.heading("Source drift (repeat of the first distance)")
    if dr:
        doc.table(["Drift (p95)", "Mean", "Limit", "Result", "Duration"],
                  [[_num(dr.get("p95_pct"), "{:.2f} %", "n/a"),
                    _num(dr.get("mean_pct"), "{:+.2f} %", "n/a"),
                    _num(dr.get("limit_pct"), "{:.2f} %", "n/a"), _status(dr.get("pass")),
                    _num(dr.get("seconds"), "{:.0f} s", "n/a")]], [1.4, 1.2, 1.2, 1.2, 1.2],
                  row_fill=[WARN_FILL if dr.get("pass") is False else None])
    else:
        doc.para("Source drift was not checked (no repeat of the first distance).", 7.5, RED)

    doc.heading("Uncertainty budget (relative, k = 1)")
    urows = [["Port radius u(r)", _num(un.get("u_rel_r_pct"), "{:.3f}", "-")],
             ["Distance u(d)", _num(un.get("u_rel_d_pct"), "{:.3f}", "-")],
             ["Spread over the distances (median)",
              _num(un.get("u_rel_spread_median_pct"), "{:.3f}", "-")],
             ["Source drift", _num(un.get("u_rel_drift_pct"), "{:.3f}", "-")],
             ["CC calibration", _num(un.get("u_rel_cc_pct"), "{:.3f}", "-")],
             ["Combined, median", _num(un.get("u_rel_median_pct"), "{:.3f}", "-")],
             ["Combined, maximum", _num(un.get("u_rel_max_pct"), "{:.3f}", "-")],
             ["Expanded U, median (k = 2)", _num(un.get("U_rel_median_pct_k2"), "{:.3f}", "-")]]
    doc.table(["Component", "Value (%)"], urows, [4, 1.5])

    doc.heading("Limits")
    doc.table(["Setting", "Value"], [[k, v or "-"] for k, v in _sphere_limit_rows(record)],
              [3, 2])

    doc.heading("Result source")
    if src:
        doc.kv([("ID", src.get("id") or "-"), ("Name", src.get("name") or "-"),
                ("Unit", _unit_text(src.get("unit")) or "-"),
                ("Quantity", src.get("quantity") or "-"),
                ("Valid range", f"{_num(src.get('valid_min_nm'), '{:.1f}', '-')} - "
                                f"{_num(src.get('valid_max_nm'), '{:.1f}', '-')} nm")])
    else:
        doc.para("Not saved as a reference source.")

    def shade(ax):
        if n and vr and len(vr) == 2 and None not in vr:
            ax.axvspan(np.nanmin(wl), vr[0], color=GREY, alpha=0.6, lw=0)
            ax.axvspan(vr[1], np.nanmax(wl), color=GREY, alpha=0.6, lw=0)

    doc.heading(f"Irradiance E per distance ({e_unit_l})")
    a1 = doc.plot(115)
    if n and any(np.isfinite(e).any() for e in E):
        shade(a1)
        for i, e in enumerate(E):
            a1.plot(wl, e, color=cmap[i % len(cmap)], lw=1.1, label=lab(i))
        if np.isfinite(Er).any():
            a1.plot(wl, Er, color=DARK, lw=0.8, ls="--", label="repeat of 1")
        a1.legend(fontsize=7, loc="upper right")
        _style_axes(a1, "wavelength (nm)", f"E ({e_unit_l})")
    else:
        doc.nodata(a1)

    doc.heading(f"Radiance per distance L_i and used L ({unit_l})")
    a2 = doc.plot(115)
    if n and (any(np.isfinite(x).any() for x in Li) or np.isfinite(Lu).any()):
        shade(a2)
        for i, x in enumerate(Li):
            a2.plot(wl, x, color=cmap[i % len(cmap)], lw=0.8, alpha=0.8, label=f"L_{lab(i)}")
        a2.plot(wl, Lu, color="black", lw=1.4, label="L (used)")
        a2.legend(fontsize=7, loc="upper right")
        _style_axes(a2, "wavelength (nm)", f"L ({unit_l})")
    else:
        doc.nodata(a2)

    doc.heading("Relative spread and combined uncertainty (%)")
    a3 = doc.plot(95)
    if n and (np.isfinite(spread).any() or np.isfinite(urel).any()):
        shade(a3)
        a3.plot(wl, spread, color=BLUE, lw=1.1, label="spread over the distances")
        a3.plot(wl, urel, color=ORANGE, lw=1.1, label="u_rel (k = 1)")
        lim = record.get("consistency_max_pct")
        if lim is not None:
            a3.axhline(float(lim), color=RED, ls="--", lw=0.8, label="spread limit")
        a3.legend(fontsize=7, loc="upper right")
        _style_axes(a3, "wavelength (nm)", "%")
    else:
        doc.nodata(a3)

    doc.heading("Warnings")
    doc.bullets([str(w) for w in (record.get("warnings") or [])])
    return doc.save(path)


def write_sphere_protocol(record: dict, pdf_path: str | None = None,
                          csv_path: str | None = None) -> list[str]:
    out = []
    if csv_path:
        out.append(write_sphere_csv(record, csv_path))
    if pdf_path:
        out.append(write_sphere_pdf(record, pdf_path))
    return out
