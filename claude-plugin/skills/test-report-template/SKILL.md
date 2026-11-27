---
name: test-report-template
description: Structure, table templates and mandatory fields for a spectrometer test report (Pruefbericht) in German and English. Use when writing or checking a test report for light source or filter measurements.
allowed-tools: mcp__plugin_ps2600-spectrometer_spectrometer__* Read Write
---

# Test report template

Write the report as Markdown. Factual, technical tone. Fill every mandatory field; write "not stated" (de: "nicht angegeben") instead of guessing. Pull values from the tool results (`measure`, `analyze_color`, `status`, `calibration_status`).

## Outline (de)

```
# Prüfbericht Spektralmessung
## 1 Kenndaten
| Feld | Wert |
|---|---|
| Datum / Uhrzeit (mit Zeitzone) | 2026-11-20 14:03:12 +01:00 |
| Prüfer / Auftrag | ... |
| Prüfling | Bezeichnung, Seriennummer |
| Messgerät | Backend, Seriennummer |
| Optik / Eingang | Name, CC / Kugel / blank |
| Kalibrierprofil | Label, Typ, Gültigkeitsbereich (nm), Zustand |
## 2 Messbedingungen
Integrationszeit (ms), Mittelung (Frames), Dunkelmodus / Dunkelquelle, Einheit, Umgebung, Aufwärmzeit
## 3 Ergebnisse
| Größe | Wert | Einheit | Anforderung | Ergebnis |
|---|---|---|---|---|
| CCT | | K | | |
| Duv | | - | | |
| Ra / R9 | | - | | |
| Beleuchtungsstärke | | lx | | |
| Peak / dominante Wellenlänge | | nm | | |
| Halbwertsbreite | | nm | | |
## 4 Filter (nur bei Transmissionsmessung)
| Größe | Wert | Einheit |
|---|---|---|
| mittlere Transmission im Band | | % |
| Kantenwellenlänge(n) | | nm |
## 5 Warnungen und Abweichungen
Sättigung (n_saturated), Signalpegel (peak_frac), Messwerte außerhalb des Gültigkeitsbereichs, fehlende Kalibrierung, Übersteuerungen durch den Benutzer
## 6 Anhänge
Messungs-IDs, CSV- und PDF-Pfade (create_report, export_measurement)
## 7 Bewertung
Nur mit vorgegebenen Kriterien: Kriterium, Messwert, Abstand, bestanden / nicht bestanden
```

## Outline (en)

Same structure: 1 Identification (date/time with time zone, test item, instrument + serial, optics / input, calibration profile with type and valid range), 2 Measurement conditions (t_int in ms, frames averaged, dark mode / source, unit), 3 Results (table: quantity, value, unit, requirement, verdict), 4 Filter transmission (if applicable), 5 Warnings and deviations, 6 Attachments, 7 Assessment (only against given criteria).

## Mandatory fields

Date and time with time zone; instrument and serial number; optics / input; calibration profile (label, type, valid range); t_int, frames, dark mode and source; results with units; warnings and deviations; measurement ids and attachment paths.

## Rules

- Lux / illuminance only with absolute irradiance calibration (or a configured radiometric factor); otherwise state "not available". With an absolute radiance calibration state luminance in cd/m2 instead (no lux). Always state the measurement quantity.
- Quote the unit of every value; wavelengths in nm, CCT in K, t_int in ms.
- Round sensibly (CCT to 10 K, Duv to 4 decimals, Ra to 1 decimal); state the number of frames averaged as basis for the repeatability.
- Never state a verdict without a stated criterion.
