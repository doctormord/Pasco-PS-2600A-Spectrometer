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
    if v is None:
        return none
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return str(v)


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
        ("stability_pct", _num(chk.get("stability_pct"), "{:.3f}")),
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
            ("Type", meta["type"]), ("Unit", meta["unit"]),
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
            ("WL fingerprint", meta["wl_fingerprint"] or "-")], ncols=1)

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
