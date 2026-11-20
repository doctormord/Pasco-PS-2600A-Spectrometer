---
name: filter-transmission
description: Measure the spectral transmission of an optical filter or sample with the PS-2600A spectrometer (reference without filter, then with filter, same settings) and report it. Use for filter, glass or coating transmission measurements.
allowed-tools: mcp__plugin_ps2600-spectrometer_spectrometer__* Read Write
---

# Filter transmission

Transmission is T(lambda) = sample(lambda) / reference(lambda) with identical conditions, so everything except the filter must stay unchanged.

## Procedure

1. `status`; connect if needed. Use a stable, broadband source covering the band of interest (check `valid_range_nm`).
2. Fix the exposure ONCE with the filter removed: `auto_expose` (target 70-85 % of full scale). Then keep this integration time for both measurements; do not use `integration_ms="auto"` for the sample.
3. Dark: `capture_dark` at that t_int (user step: block the light; `continue_job` only after the user confirms), then use `dark="stored"` for both measurements.
4. Reference: `measure(integration_ms=<t_int>, frames=10, dark="stored", label="reference")` without the filter. Check `n_saturated == 0` and `peak_frac` 0.3-0.9.
5. Sample: put the filter into the beam (same position, perpendicular), `measure(..., label="<filter name>")` with the same t_int, frames and dark. A dark filter may give a low `peak_frac`; raise `frames` instead of the t_int.
6. `analyze_filter(reference_id, sample_id)`: metric rows (mean transmission, band edges, ...) and a transmission preview.
7. Report: `create_report(kind="filter", format="pdf", reference_id, sample_id)`; optionally `export_measurement` of both spectra.

## Checks

- Reference and sample must have the same unit, t_int and dark. Different settings invalidate T.
- Transmission above 100 % or negative values mean drift, a changed set-up or too little signal; repeat the reference.
- Where the reference is near zero (outside the source's emission) T is meaningless; restrict the evaluation to the band where the reference has enough signal.
- A drifting lamp: repeat the reference after the sample and compare (`peak_frac` / peak height).
- Document filter name/serial, thickness, angle of incidence, light path and the calibration profile.
