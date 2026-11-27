---
name: light-source-characterization
description: Characterize a light source with the PS-2600A spectrometer - spectrum, CCT, Duv, CRI (Ra, R9), illuminance and peaks - including data quality checks and plausibility limits. Use when the user wants to measure or evaluate a lamp, LED or luminaire.
allowed-tools: mcp__plugin_ps2600-spectrometer_spectrometer__* Read Write
---

# Light source characterization

## Procedure

1. `status`; connect if needed (`list_devices`, `connect`). Note the active calibration (`calibration_status`): label, type, valid_range_nm.
2. Set-up: stable source (LEDs: warm up, constant drive), fixed geometry, no stray light. Ask for the input optic (CC, sphere, bare fibre) and distance.
3. Exposure: `auto_expose`, then `measure(frames=10)` to average noise (more frames for weak sources). Dark: `dark="auto"`; for best results `capture_dark` at the final t_int first, then `dark="stored"`.
4. Evaluate: the `measure` result already has the key figures; use `analyze_color(measurement_id)` for the full set (Ri, SDCM, S/P, purity) and `find_peaks` for line/LED peaks. `get_spectrum(measurement_id, max_points=64)` for a shape overview, `save_full=True` for the CSV.
5. Report: `create_report(kind="color", format="pdf", measurement_id=...)`.

## Quality gate (do this first)

- `n_saturated == 0` - otherwise shorten t_int and repeat.
- `peak_frac` 0.3 to 0.9 - otherwise adjust exposure.
- Source emission inside `valid_range_nm` - colour metrics outside it are not calibrated.
- Check `warnings` of the result.

## Plausibility limits (flag anything outside)

- CCT: incandescent about 2700-3200 K, warm-white LED 2700-3500 K, neutral 3500-5000 K, daylight/cool 5000-7000 K. A value far outside 1500-20000 K means a bad measurement.
- Duv: for white light usually within +/-0.006; beyond +/-0.02 the CCT is not meaningful.
- Ra: 0-100; typical LED 70-98. R9 can be strongly negative for LEDs with weak deep red.
- x, y: inside the spectrum locus; white light near the Planckian locus (y about 0.33-0.41 for 3000-6500 K).
- Illuminance (lux) is only available with an absolute calibration or a configured `radiometric_calibration_factor`. `lux: null` means not available. Never estimate lux from counts. With a radiance calibration (`quantity: radiance`, unit `.../sr/nm`) the photometric result is luminance in cd/m2 (`luminance_cd_m2`) and lux is not available.
- Compare peak wavelengths with the expected emitter (blue LED pump about 440-460 nm, phosphor broad band 500-650 nm).

State results with units: CCT in K, Duv dimensionless, wavelengths in nm, lux in lx or luminance in cd/m2.
