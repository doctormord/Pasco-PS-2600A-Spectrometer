---
name: stability-measurement
description: Plan and run a measurement series to assess the stability or drift of a light source (CCT, lux, peak height) with the PS-2600A spectrometer, using background series jobs. Use for warm-up, drift and long-term stability questions.
allowed-tools: mcp__plugin_ps2600-spectrometer_spectrometer__* Read Write
---

# Stability measurement

## Plan

1. Ask: what quantity matters (CCT, lux, peak intensity), total duration, resolution in time. Interval and count follow: `count = duration / interval_s`. Typical: warm-up of an LED 30-60 min at 10-30 s; short-term noise 100 measurements at 0 s. Series are capped by the server (up to 100000 measurements) and kept in memory (the server keeps only the most recent measurements), so for long runs use intervals of 10 s or more.
2. Fix the exposure once: `auto_expose`, then pass this t_int explicitly (not `"auto"`) so all points are comparable. Choose frames so that one measurement is shorter than the interval. Use `dark="stored"` after one `capture_dark` at that t_int. Pick a t_int that leaves headroom for intensity increases (peak_frac about 0.6).
3. Start: `start_series(count, interval_s, integration_ms, frames, dark, label)`; keep the returned `job_id`.

## Follow up

- `job_status(job_id)` periodically (not more often than the interval). While running it shows progress; when done it contains a summary: up to 50 entries (id, cct, lux, peak_frac, peak_nm) or - for more - min/max/mean statistics of cct, lux and peak_frac.
- Drift: compare first and last values; relative drift = (last - first) / first; also peak-to-peak and mean. For more detail use `get_spectrum(measurement_id)` of the first and last id, or `analyze_color` on single points.
- Abort criteria (call `cancel_job` and tell the user why): `peak_frac` leaves 0.1-0.95, saturation appears (n_saturated > 0), device disconnected / job failed, value drift beyond the limit the user set, or the user asks. Measurements already stored are kept.
- Do not change settings (`set_acquisition`) while the series runs; the device is under control of the job.

## Report

Duration, interval, count, t_int, frames, dark, calibration profile; table of start / end / min / max / mean / drift for CCT (K), lux (lx, only with absolute calibration) and peak_frac; remarks on warm-up behaviour (time to reach a stable plateau) and any aborts.
