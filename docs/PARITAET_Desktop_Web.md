# Funktionsparität Desktop (PyQt-App) ↔ Web-Oberfläche

Stand: 2026-11-20, Basis: Commit 92f82b7 (Branch `feature/api-mcp`).

Zweck: vollständige Funktionsmatrix zwischen der Desktop-App (`gui_dashboard.py`, `gui_cie_tab.py`, `gui_filter_tab.py`, `gui_calibration.py`) und der Web-Oberfläche (`static/index.html`, `static/app.js`, `static/styles.css`, `web_server.py`). Jeder Eintrag wurde im Code nachgeprüft. Die Matrix ist Grundlage für Phase 4 des Plans (`docs/PLAN_api_mcp.md`): Lücken der Kategorien *Messung/Einstellung* und *Kalibrierung* werden im Web geschlossen, *nur Darstellung* wird geschlossen, wo der Aufwand gering ist, der Rest bleibt als bewusst offen markiert. Abschnitt 13 („Lücken nach Kategorie“) listet alle Lücken mit Umsetzungsvorschlag, Abschnitt 14 („Unterschiede in der Logik“) die Stellen, an denen Desktop und Web dieselbe Funktion unterschiedlich rechnen.

Legende Status: **gleich** (Funktion und Verhalten übereinstimmend), **Lücke Web** (nur Desktop), **Lücke Desktop** (nur Web), **abweichend** (beides vorhanden, aber unterschiedlich bedient oder berechnet). Kategorien: *Messung/Einstellung*, *Kalibrierung*, *nur Darstellung*.

Allgemein: Einstellungen werden in beiden Oberflächen über denselben Config-Mechanismus (`app_config.Config`, `spectrometer_config.json`) abgelegt. Das Web schreibt über WebSocket `set_config` bzw. `POST /api/config` serverseitig in dieselbe Config; Theme und alle Einstellungen gelten damit global für alle Web-Clients.

## 1. Verbindung / Gerät

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Backend wählen | Toolbar `combo_backend` (aus `dm.list_backends()`) | Toolbar `#backend-select`; `GET /api/backends`, hello-Nachricht | gleich | Messung/Einstellung | Config `selected_backend` (Web setzt sie bei Connect). |
| Geräteliste / Scan | `Scan USB`, `combo_devices` | `#btn-scan`, `#device-select`; `GET /api/devices?backend=` | gleich | Messung/Einstellung | |
| Verbinden / Trennen | `Connect`/`Disconnect` (`connect_device`, `disconnect_device`) | `#btn-connect`; `POST /api/connect`, `/api/disconnect`, WS `connect`/`disconnect` | abweichend | Messung/Einstellung | Logik doppelt. Unterschiede: siehe Abschnitt 14 (B-05 `wl_offset`/`flip`, B-08 Belichtungsgrenzen). Fehler: Desktop `QMessageBox`, Web Toast. |
| Autoconnect beim Start | nein | ja, Lifespan verbindet das erste Gerät des ersten Backends (`web_server._lifespan`) | abweichend | Messung/Einstellung | Siehe B-10. |
| Verbindungsanzeige | `ConnDot`, Live-Pill (`● <Hz>`/`● Idle`) | `#conn-dot`, `#livepill` | gleich | nur Darstellung | |
| Verbindungsverlust | Callback, `QMessageBox` „Connection Lost“, Auto-Trennung | `on_connection_lost` → `_disconnect` im Event-Loop; Frames bleiben aus, WS-Nachrichten `status`/`error` werden vom Client nicht ausgewertet | abweichend | Messung/Einstellung | Web zeigt keinen expliziten Hinweis (kein Dialog/Toast für den Verlust selbst). |
| Beenden | `EXIT`-Button (Titelleiste) | — | Lücke Web | nur Darstellung | Nicht sinnvoll im Web; bewusst nicht vorgesehen. |
| Steuerelemente ohne Gerät deaktiviert | `control_widget`/`header_controls` disabled; `START` disabled | `btn-pause` wirkt nur bei `hardwareConnected`; übrige Bedienelemente bleiben aktiv | abweichend | nur Darstellung | |
| Versionsanzeige | Statusleiste `v<version>` (`version.app_version`) | — | Lücke Web | nur Darstellung | Siehe P-20. |
| Fensterlayout/Geometrie | `main_window_geometry`, `calibration_dialog_geometry` | — (responsives Layout, `#sidebar-toggle` auf Mobilgeräten) | abweichend | nur Darstellung | Nicht übertragbar. |

## 2. Erfassung

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Start/Pause | `button_pause` (`▶ START`/`■ PAUSE`, Header der Sidebar) | `#btn-pause` (Hero); WS `pause` | gleich | Messung/Einstellung | Pausenzustand wird nicht persistiert. |
| Belichtung (ms) | Spinbox `spinbox_exposure`, 0,1–10000 ms, 2 Nachkommastellen, Grenzen werden beim Verbinden auf Gerätegrenzen gesetzt; Config `exposure_ms` | `#cfg-exposure_ms`, fest `min=1 max=2500 step=50`; Server klemmt auf Gerätegrenzen | abweichend | Messung/Einstellung | Siehe P-08, B-08. |
| Belichtung zurücksetzen | Button `20 ms` | `#btn-exp-reset` (`20 ms`) | gleich | Messung/Einstellung | |
| Auto-Belichtung | `ToggleSwitch`; Config `auto_exposure`; Rückmeldung per `on_auto_exp` aktualisiert Spinbox | `.toggle[data-key=auto_exposure]`; Rückmeldung per WS-`config`-Nachricht (`exposure_ms`) | abweichend | Messung/Einstellung | Manuelle Belichtungsänderung schaltet im Desktop Auto-Belichtung aus, im Web nicht (B-08). |
| Mittelung (Frames) | `spinbox_averaging` 1–100; Config `frames_to_average` | `#cfg-frames_to_average` 1–100 | gleich | Messung/Einstellung | |
| Fast Preview | Toggle; Config `fast_preview_enabled` | `.toggle[data-key=fast_preview_enabled]` | abweichend | Messung/Einstellung | Beim Ausschalten stellt der Desktop die Hauptbelichtung sofort wieder her; das Web setzt nur das Flag (B-08). |
| Short frame % | `spinbox_fp_short_pct` 1–50; Config `fast_preview_short_pct` | `input[data-key=fast_preview_short_pct]` 1–50 | gleich | Messung/Einstellung | |
| Glättung/Fusion ein | Sidebar „Smoothing“ → `Enable smoothing`; Config `fusion_enabled` | Sidebar „Smoothing · 05“ | gleich | Messung/Einstellung | Gemeinsame Klasse `fusion.SpectrumFusion`, getrennte Instanzen. |
| Smoothing % | `spinbox_fusion_smoothing` 0–99; Config `fusion_smoothing` (0–1) | `data-key=fusion_smoothing`, `data-scale=0.01` | gleich | Messung/Einstellung | |
| Peak sensitivity (σ) | 0,5–20, Schritt 0,5; Config `fusion_change_sigma` | `data-key=fusion_change_sigma` | gleich | Messung/Einstellung | |
| Output rate (Hz) | 0–120, 0 = „Auto“; Config `fusion_emit_hz` | `data-key=fusion_emit_hz` 0–120 | gleich | Messung/Einstellung | |
| Fusion-Rauschmodell | Config `fusion_noise_floor`, `fusion_noise_gain`, `fusion_emit_max_hz`, `fusion_emit_min_hz` | gleiche Config-Schlüssel | gleich | Messung/Einstellung | Kein Bedienelement in beiden Oberflächen (nur Config-Datei). |
| Frames/s-Anzeige | Statusleiste `Frames / s`, Live-Pill | Hero `FRAMES / S`, Live-Pill | gleich | nur Darstellung | |
| Belichtungs-/Dunkelwert-Anzeige | Statusleiste `Integration`, `Dark` | Hero `EXPOSURE`, `DARK LVL`/`OB MEAN`/`BASELINE`; Statusleiste `Integration`, `Dark`/`Baseline` | abweichend | nur Darstellung | Das Web benennt den Wert geräteabhängig (`supports_ob`); der Desktop immer „Dark“. |

## 3. Verarbeitung

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Dunkelkorrektur ein/aus | Toggle `Dark correction`; Config `dark_correction` | `.toggle[data-key=dark_correction]` | gleich | Messung/Einstellung | Rechenweg unterschiedlich: B-04, B-07. |
| Dunkelmodus `optical_black` | Combo „Optical Black (live)“; Config `dark_mode` | `#cfg-dark_mode` „Optical Black“ | abweichend | Messung/Einstellung | Konstanten für Geräte ohne OB unterschiedlich (B-04). |
| Dunkelmodus `parametric` | „Parametric (Bias+Rate)“; Bias/Rate-Felder nur im Modus aktiv | `Optical`/`Parametric`; Felder `cfg-dark_bias_adc`, `cfg-dark_rate_adc_per_s` immer editierbar | abweichend | Messung/Einstellung | Config `dark_bias_adc`, `dark_rate_adc_per_s`. Das Web blendet Bias/Rate in der Statusleiste für Geräte ohne OB aus, der Desktop nicht. |
| Dunkelmodus `stored` | „Stored dark (per pixel)“ (`processing.DARK_MODE_STORED`) | „Stored dark (per pixel)“ | gleich | Kalibrierung | Auswahl in beiden vorhanden; Erfassen nur im Desktop (siehe Kalibrierung, P-09). |
| Despeckle (Hot-pixel filter) | Toggle `hot_pixel_filter`; Fensterbreite `smoothing_width` 3–15 (Label „Smoothing width px“) | Toggle `hot_pixel_filter`; `cfg-smoothing_width` (Label „Smoothing px“) | gleich | Messung/Einstellung | Gleiche Wirkung; Desktop-Legacy-Pfad rechnet inline, Web über `processing.apply_despeckle` (B-07). |
| Räumliche Glättung | Toggle `spatial_smoothing`; `spatial_smoothing_width` 3–51 | gleiche Schlüssel | gleich | Messung/Einstellung | `processing.apply_spatial_smoothing`. |
| Response-Kompensation (Schalter) | kein Schalter; Zustand folgt der Profilwahl (`calprof.set_active_selection` setzt `spectral_response_compensation`) | Toggle `spectral_response_compensation` (`index.html`) | Lücke Desktop | Messung/Einstellung | Der Web-Schalter hat keine Wirkung auf die Verarbeitung: `processing.pipeline_kwargs` setzt `response_compensation=legacy is not None` und liest die Config nicht. Siehe P-25. |
| Anzeigeeinheit counts / counts/s | kein Bedienelement; Config `display_unit` wirkt (`needs_shared_pipeline`) | Select `#cfg-display_unit` im Abschnitt „Calibration“ | Lücke Desktop | Messung/Einstellung | Siehe P-22. |
| Bestrahlungsstärkeeinheit | kein Bedienelement; Config `irradiance_unit` | Select `#cfg-irradiance_unit` (`uW/cm2/nm`, `W/m2/nm`) | Lücke Desktop | Messung/Einstellung | Siehe P-22. Web ruft nach Änderung `state.calib.refresh()`; der Desktop-`CalibrationRuntime` wird nicht aktualisiert, weil es keinen Auslöser gibt. |
| Antwortkorrektur-Profil (Auswahl) | Combo `combo_response` im Abschnitt „Processing“; Bestätigungsdialog bei geänderter Wellenlängen-/Linearisierungsbedingung | Select `#cal-select` im Abschnitt „Calibration“; `POST /api/calibration/select`, bei Konflikt HTTP 409 mit `confirm` | gleich | Kalibrierung | Siehe Abschnitt 11. |
| Wellenlängenabhängige Skalierung der Anzeige | `_apply_unit_to_axis` (Achsentitel je Einheit) | `updateYAxisLabel`, `unit_label` aus Frame | gleich | nur Darstellung | |

## 4. Anzeige

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| X-Achse (Min/Max nm) | `spinbox_x_min/max` 100–1500; Config `x_min_nm`, `x_max_nm`; Abbruch bei min ≥ max | `#cfg-x_min_nm`, `#cfg-x_max_nm` 100–1500 | gleich | nur Darstellung | |
| Zoom/Pan | pyqtgraph (Maus) | Mausrad-Zoom auf X, Doppelklick = Reset (`wheelZoomPlugin`) | abweichend | nur Darstellung | |
| Auto-Y | Toggle `auto_y`; sichtbarer X-Bereich, Hysterese (Anpassung nur bei >100 % oder <70 %) | Toggle `auto_y`; gesamtes Spektrum, jedes Frame | abweichend | nur Darstellung | B-09. |
| Y max / Snap | `spinbox_y_max` 10–65000; `▲` (Snap auf Frame-Maximum, schaltet Auto-Y aus); Config `y_max_adc` | `#cfg-y_max_adc`; `#btn-y-snap` | gleich | nur Darstellung | |
| Referenzspektren | Combo `combo_reference` (Config `reference_library`), 90 % von Y max, gestrichelt | `#cfg-reference_library`; `GET /api/references`, `/api/reference` | gleich | nur Darstellung | Gemeinsame Bibliothek (`load_reference_library`). |
| Peaks (Toolbar) | `● PEAKS`; Config `show_peaks` | `#btn-peaks`; Config `show_peaks` | gleich | Messung/Einstellung | Berechnung unterschiedlich: B-06. |
| Messcursor (Toolbar) | `⇿ MEASURE`: zwei verschiebbare Linien, Config `measure_mode`, `measure_a_nm`, `measure_b_nm`; Anzeige „A · B · Δλ · Δy ADC“ | `#btn-measure`; gleiche Config-Schlüssel; gleiche Anzeige | gleich | nur Darstellung | Beide Oberflächen zeigen „ADC“ fest, auch bei counts/s oder Irradianz (B-12). |
| Live-Auslesung | Toolbar PIXEL / WAVELENGTH / INTENSITY (Szene und Heatmap) | `#ro-pixel`, `#ro-wave`, `#ro-int` (Szene, Heatmap, Filter) | gleich | nur Darstellung | Einheit fest „ADC“ (B-12). |
| Out-of-spec-Schattierung | rote Bänder + Grenzlinien aus `backend.spec_range_nm` | `outOfSpecPlugin` aus `spec_min`/`spec_max` im Frame | gleich | nur Darstellung | Config `<backend>_spec_min_nm/_max_nm`. |
| Gültiger Bereich (Profil) | graue Bänder außerhalb `valid_range_nm`, zusätzlich graue Lücken zwischen `valid_segments_nm` | `validRangePlugin`: nur äußere Grenzen; Lücken erscheinen als Unterbrechung der Kurve (`null`) | abweichend | nur Darstellung | Siehe P-19. |
| Statusleiste | `Frames / s`, `Integration`, `Dark`, `Bias`, `Rate`, `Status`, Sensor-Chip, Version | `Integration`, `Dark`, `Bias`, `Rate`, `Backend`, `WS`, Sensor-Chip | abweichend | nur Darstellung | Sensor-Chip in beiden aus `ob_mean` (gleiche Schwellen 1/4 ADC). Version nur Desktop; `Backend`/`WS` nur Web. |
| Theme | `DARK`/`LIGHT`; Config `theme` | `#theme-dark`/`#theme-light`; Config `theme` | gleich | nur Darstellung | |
| Gradient-Füllung der Kurve | `WavelengthFillItem` | `makeSpectrumFill` | gleich | nur Darstellung | |

## 5. Scope-Overlays

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Freeze | `❄ Freeze`/`✕ Frozen` (`toggle_freeze`), graue Kurve, absolute Werte | `#btn-freeze` | gleich | nur Darstellung | Rein clientseitig, nicht persistiert. |
| Diff | `Δ Diff` (live − freeze, magenta) | `#btn-diff` | gleich | nur Darstellung | Erfordert Freeze. |
| Peak-Hold | `⎍ Peak-hold` (gelb); jedes Umschalten setzt zurück | `#btn-peakhold` | gleich | nur Darstellung | |
| Persist | `≈ Persist`, `SCOPE_PERSIST_TRAILS` Spuren | `#btn-persist`, `PERSIST_N = 6` | gleich | nur Darstellung | Spurenzahl in beiden 6 (`SCOPE_PERSIST_TRAILS`, `PERSIST_N`). |
| CSV-Hintergrund | `CSV bg…`, `✕` (`_load_two_column_csv`), auf 90 % von Y max normiert | `#csvbg-file`, `#btn-csvbg-clear` (`parseTwoColCsv`) | gleich | nur Darstellung | |

## 6. Heatmap

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Darstellung | Tab „Time-Lapse Heatmap“, Inferno, Ursprung unten, gemeinsame Pegel `(0, ideal_heatmap_max)` | Tab „Heatmap“, Canvas, Inferno, jüngste Zeile unten, Normierung je Zeile mit Y max des jeweiligen Frames | abweichend | nur Darstellung | B-03. |
| Fadenkreuz / Auslesung | `handle_heatmap_mouse_movement` | `mousemove` im Canvas, Beschriftung `nm · ADC · −n frames` | gleich | nur Darstellung | |
| Puffergröße | Spinbox `Buffer` (Schritt 10, 10–100000); Config `heatmap_buffer_size` (Standard 100); Änderung leert den Puffer | fest `HM_ROWS = 240` (Client); serverseitig `HEATMAP_HISTORY_SIZE = 100` für `/api/heatmap` und CSV | Lücke Web | Messung/Einstellung | P-01, B-03. |
| Messintervall | Spinbox `Log every` (ms, 0 = jedes Frame); Config `heatmap_measure_interval_ms` | — (jedes Frame) | Lücke Web | Messung/Einstellung | P-02. |
| Zeitspanne | Label `Span ≈ …` | — | Lücke Web | nur Darstellung | In P-02 enthalten. |
| Clear | `⟲ Clear` | — | Lücke Web | Messung/Einstellung | P-04. Im Web nur Seitenneuladen. |
| Autosave | Checkbox `Autosave to disk`; Config `heatmap_autosave_enabled`, `heatmap_autosave_dir`, `heatmap_autosave_fsync_s`; `HeatmapAutosaveWriter` | — | Lücke Web | Messung/Einstellung | P-03. |
| Rewind/Scrub | Slider, DVR-Nachführung, Anzeige „T-x s ago“, Spektrum des gewählten Frames (`plot_hm_scrub`) | — | Lücke Web | nur Darstellung | P-05. |
| Live/Pin | `● Live` (Pin des aktuellen Frames bzw. Rückkehr zu Live) | — | Lücke Web | nur Darstellung | P-05. |
| Export frame | `Export frame` (ein Frame als CSV, Spalten Wavelength_nm; ADC_Counts) | — | Lücke Web | Messung/Einstellung | P-06. |
| Export CSV | `Save CSV` im Heatmap-Tab: Auswahl „Visible window“/„Full buffer“, nur gefüllte Frames, Kopf `Time_Frame` | `Save CSV` → `GET /api/export/csv?tab=heatmap`: immer alle 100 Serverframes (auch ungefüllte Nullen), Kopf `Frame`; stammt aus einem anderen Puffer als die Anzeige | abweichend | Messung/Einstellung | P-06, B-03. |
| Pufferbehandlung bei Gerätewechsel | Puffer wird auf neue Pixelzahl neu angelegt | `_apply_device_params` legt Serverpuffer neu an; Clientpuffer hält Zeilen mit eigener Wellenlängenachse | abweichend | nur Darstellung | |

## 7. Color-Tabs

Desktop: Tab „Color · CCT / CRI“ (`gui_cie_tab.CIETab`) mit Unter-Tabs Overview, CIE 1931, Color Rendering, TM-30 / CQS. Web: Tab „Color · CRI“ mit Unter-Tabs „Overview · CRI“ und „TM-30 · CQS“; CIE-Diagramm, CRI-Radar, -Balken und -Farbmuster liegen im Web in der Overview.

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| CCT, Duv, Ra | Overview-Karten | `#c-cct`, `#c-duv`, `#c-ra` | gleich | nur Darstellung | Werte aus `color_science.measure_all`; Berechnungskette B-01, B-02. |
| Illuminance (lx), Foot-candle (fc) | Karten `Illuminance`, `Foot-candle`; nur mit `radiometric_calibration_factor` > 0 oder absolutem Profil | — (Karten fehlen; `color.lux`/`color.fc` stehen im Frame, ohne Konfig-Faktor immer `NaN`/`null`) | Lücke Web | Kalibrierung | P-17, B-01. |
| x, y, u′, v′, SDCM, Peak λ, Dominant λ, Purity, FWHM, Centroid λ, S/P, Red/Green/Blue | Overview- und Chromaticity-Karten | Karten `#c-x … #c-b` | gleich | nur Darstellung | |
| Central λ | Karte `Central λ` (`central_nm`) | — | Lücke Web | nur Darstellung | P-18. |
| u, v (CIE 1960) | Karten `u (CIE 1960)`, `v (CIE 1960)` | — | Lücke Web | nur Darstellung | P-18. |
| CIE-1931-Diagramm | Unter-Tab „CIE 1931“: Spektralkurve, Planckscher Ort, Isotherme, 5-SDCM-MacAdam-Ellipse | `#cie-canvas`: Hufeisen, Planckscher Ort, Messpunkt | abweichend | nur Darstellung | Ellipse fehlt im Web (P-18). |
| CRI R1–R15 (Balken, Radar) | Unter-Tab „Color Rendering“ | `#cri-bars`, `#cri-radar` | gleich | nur Darstellung | |
| R9-Karte | Karte `R9 (saturated red)` | — (R9 nur als Balken) | Lücke Web | nur Darstellung | P-18. |
| Referenz-/Testfarbmuster | Swatches mit ΔE*ab | `#cri-patches` (Hover ΔE*ab) | gleich | nur Darstellung | |
| TM-30 / CQS | Karten Rf, Rg, Qa, Qf, Qg, CCT, Duv; Farbvektorgrafik, 99 Proben, Rcs,hj; Drossel 1,2 s, Hintergrund-Thread, nur wenn Unter-Tab sichtbar | gleiche Karten/Grafiken; `POST /api/tm30`, Drossel 1,2 s clientseitig; HTTP 503 ohne `colour-science` | gleich | nur Darstellung | Eingangsspektrum unterschiedlich aufbereitet: B-13. |
| Bericht PDF/PNG/CSV | Exportleiste `⤓ PDF`, `⤓ PNG`, `⤓ CSV` (`cs.render_color_report`, `cs.color_report_rows`) mit `self._calib` als Lux-Faktor | `#btn-color-pdf/png/csv` → `POST /api/color/report` ohne Lux-Faktor | abweichend | nur Darstellung | B-01. |
| Colorimetry-CSV (Schnellexport) | — | `#btn-cie-csv`, clientseitig aus dem letzten Frame, Trennzeichen `;`, ohne Lux/TM-30 | Lücke Desktop | nur Darstellung | Zusätzliches Web-Format; kein Desktop-Äquivalent. Siehe P-24 (Zusammenführung der Berichtsformate). |
| Aktualisierungsrate | jeder 5. Frame (`UPDATE_EVERY_N_FRAMES = 5`) | serverseitig jeder 8. Fusionsframe (`color_metrics_period_frames = 8`), unabhängig vom sichtbaren Tab | abweichend | nur Darstellung | B-02. |

## 8. Filter-Tab

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Baseline setzen | `◎ Set baseline`; mittelt `filter_baseline_frames` (Standard 16) | `#btn-filter-baseline`; dieselbe Config | gleich | Messung/Einstellung | Anzeige „Averaging… n/N“ in beiden. |
| Baseline löschen | `Clear` | `#btn-filter-clear` | gleich | Messung/Einstellung | |
| Transmission/Metriken | `filter_analysis.analyze_filter`, Karten + Notizen | `POST /api/filter`, `analyze_filter`, `metrics_rows` | gleich | nur Darstellung | Metrik-Drosselung: Desktop jeder N-te Frame (`UPDATE_EVERY_N_FRAMES`), Web 700 ms. |
| Anzeigeglättung | Config `filter_display_smoothing` (kein Bedienelement) | gleiche Config (kein Bedienelement) | gleich | nur Darstellung | Metriken bleiben auf Rohdaten. |
| Plot | 0–110 %, Kantenmarken, 50-%-Linie | Transmission 0–110 %, Kantenmarken, Mittenlinie | gleich | nur Darstellung | |
| Bericht | `⤓ Save report`: PDF (Rückfall: PNG + CSV); `⤓ CSV` | `#btn-filter-pdf`, `-png`, `-csv` → `POST /api/filter/report` | abweichend | nur Darstellung | Das Web bietet PDF und PNG direkt; der Desktop nur PDF (PNG nur als Rückfall). Siehe P-24. |

## 9. Replay

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Replay-Tab (CSV-Wasserfall abspielen) | — (nur `make_figs.py` erwähnt „replay“, kein Bedienelement) | Tab „Replay“: `Load CSV…` (Heatmap-Export oder 2-Spalten-Spektrum), Play/Pause, Scrub-Slider, Geschwindigkeit 0,5×–10×, Loop | Lücke Desktop | nur Darstellung | Rein clientseitig. Siehe P-23. |

## 10. Export

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Spektrum-CSV | `Save CSV` (Header-Kommentare, `ADC_Counts` bzw. `Value_<unit>`, Profilangaben, `;`) | `Save CSV` → `GET /api/export/csv?tab=spectrum` | gleich | Messung/Einstellung | Gleicher Aufbau; Download im Browser statt Datei im Exportordner. |
| Heatmap-CSV | siehe Abschnitt 6 | siehe Abschnitt 6 | abweichend | Messung/Einstellung | P-06. |
| PNG | `Save PNG` greift das gesamte Widget des aktiven Tabs (auch Color, Filter) | `Save PNG`: nur Canvas (Scope-Plot, Heatmap, CIE-Diagramm); Filter-Tab speichert den Scope-Canvas | abweichend | nur Darstellung | P-21. |
| Berichte | Color: PDF/PNG/CSV; Filter: PDF, CSV | Color: PDF/PNG/CSV; Filter: PDF/PNG/CSV | abweichend | nur Darstellung | Abschnitte 7, 8; P-24. |
| Zwischenablage | `Copy` (Tab-getrennt, einheitenabhängige Kopfzeile, NaN-sicher) | `Copy spectrum to clipboard`: Kopf immer `Intensity_ADC`, `toFixed` auf `null` löst Fehler aus | abweichend | Messung/Einstellung | B-12. |
| Exportverzeichnis | Abschnitt „DATA“: `Choose export folder…`; Config `export_dir` (leer = Programmordner); Rückfall und Warnung (`export_paths.ensure_export_dir`) | — (Browser-Download) | Lücke Web | Messung/Einstellung | P-07. |
| Dateinamen-Vorlage | Textfeld `edit_export_template`; Config `export_filename_template` (Standard `{kind}_{datetime}`); Token `{kind} {device} {serial} {wl0} {wl1} {date} {time} {datetime} {n}`; Live-Vorschau | — (feste Namen `spectrum_<ts>.csv` u. a.) | Lücke Web | Messung/Einstellung | P-07. |
| Bericht-Metadaten | `report_meta` (Spectrometer, Serial, Date/time, Exposure, Dark/baseline, Peak ADC) | `_report_meta` (gleiche Felder) | gleich | nur Darstellung | Doppelte Implementierung; Peak-ADC unterschiedlich ermittelt (Desktop `current_averaged_pixels`, Web Anfragepayload bzw. `current_pixels`). |

## 11. Kalibrierung

Desktop: Abschnitt „Processing“ (Antwortkorrektur-Combo, `✕`, Status, `Capture dark`, `Calibration…`) und Dialog „Device calibration“ (`gui_calibration.CalibrationDialog`) mit den Tabs Wavelength, Spectral calibration, Profiles, Lamps, Response (legacy). Web: Abschnitt „Calibration“ (Einheiten, Profil-Select, Status, Delete/Export/Import, Copy).

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Profilwahl (None / Device default / Profil) | Combo `combo_response`; Config `response_correction_active` (je Gerät) | `#cal-select`; `GET /api/calibration/profiles`, `POST /api/calibration/select` | gleich | Kalibrierung | Bestätigung bei `wl_changed`/`nl_changed` in beiden (Web: HTTP 409 + `confirm`). |
| Ältere Versionen anzeigen | Profiles-Tab `All versions` | `#cal-include-old` (`include_old`) | gleich | Kalibrierung | |
| Profilstatus / Warnungen | Label `label_calib_status` (Profil, gültiger Bereich/Segmente, Plausibilität, Meldung, Dunkelquelle, `dark_warning`) | `#cal-status` (Profil, Meldung, gültiger Bereich, Dunkelquelle, `dark_warning`) | abweichend | nur Darstellung | Das Web zeigt Segmente und Plausibilitätswarnung nicht (`implausible` steht im Status-Payload). Siehe P-16. |
| Profiltabelle | Profiles-Tab: Spalten Profile, Status, Valid range, Unit, Created; Sortierung, `[active]`, `not plausible` | — (nur Select; Zusatzfelder im API-Payload vorhanden) | Lücke Web | nur Darstellung | P-16. |
| Profil löschen | `✕` (Combo) und Profiles-Tab `Delete`; Rückfall auf None, falls aktiv | `#cal-delete` → `POST /api/calibration/profile/delete` | gleich | Kalibrierung | |
| Profil exportieren / importieren | Profiles-Tab `Export…`, `Import…` | `#cal-export` (`GET …/profile/export`), `#cal-import` (`POST …/profile/import`) | gleich | Kalibrierung | |
| Profil auf dieses Gerät kopieren | Profiles-Tab `Copy profile to this device…` | `#cal-other` + `#cal-copy` (`POST …/profile/copy`) | gleich | Kalibrierung | |
| Referenzquellen auflisten | Lamps-Tab: Tabelle (Name, Serial, Unit, Valid range, Optic, Built-in), Plot der gewählten Quelle | `GET /api/calibration/references` vorhanden, kein Bedienelement | Lücke Web | Kalibrierung | P-12. |
| Referenzquellen importieren/entfernen | Lamps-Tab `Import…` (`_LampImportDialog`), `Remove` (nicht für Built-ins) | — | Lücke Web | Kalibrierung | P-12. |
| Dunkelspektren erfassen | Sidebar `Capture dark` (aktuelle Integrationszeit, `calibration_capture_frames` Frames, Abdeckhinweis bei Geräten ohne Shutter; `cc.capture_dark`/`cc.save_dark`) und Wizard-Schritt „Dark spectra“ | — | Lücke Web | Kalibrierung | P-09. |
| Dunkelspektren auflisten | kein eigener Dialog | `GET /api/calibration/darks` vorhanden, kein Bedienelement | Lücke Web | Kalibrierung | P-13 (Desktop zeigt sie ebenfalls nicht tabellarisch). |
| Kalibrierlauf (Lampe) | Tab „Spectral calibration“, Modus „Calibrate against a reference lamp“: Optics, Notes, Input optic, Type (relative/absolute), Reference; Integration Auto/Fixed, Frames; Schritte Selection, Wavelength check, Integration time, Dark spectra, Calibration + stability, Integration cross check, Compute, Save + protocol; Abbruch; Plausibilitätsdialog | — | Lücke Web | Kalibrierung | P-10. Gemeinsame Logik liegt bereits Qt-frei in `calibration_workflow.py`/`calibration_capture.py`. |
| Kalibrierlauf (Ableitung) | Modus „Derive from other optics“: Known, New optics, Input, Notes; Schritte Selection, Integration time, Known optics (dark + light), New optics (dark + light), Compute, Save (`S_new = S_known · C_new / C_known`) | — | Lücke Web | Kalibrierung | P-10. |
| Wellenlängenkalibrierung | Tab „Wavelength“: Lampe (`calprof.CALIBRATION_LINES`), Grad 2–3, Multi-Lampen-Modus, `Capture & detect peaks`, Linientabelle (Pixel, bekannte nm), `Fit & apply`, `Back up & confirm`, `Save to device`; Live-Vorschau der Achse | — | Lücke Web | Kalibrierung | P-11. Config `pasco_wl_poly_coeffs`, `lr2t_wl_poly_coeffs`, `ocean_wl_poly_coeffs`. |
| Protokoll (PDF/CSV) | automatisch je abgeschlossener Berechnung (`calibration_report.write_protocol`), Ablage in `<export_dir>/calibration` bzw. Benutzerdatenordner, Button `Open folder` | — | Lücke Web | Kalibrierung | P-14. |
| Legacy-Antwortkorrektur | Tab „Response (legacy)“: Referenz (Capture live, Load CSV, Save CSV), Messung (Capture live), `Compute correction`, Profilname, `Save profile` | — | Lücke Web | Kalibrierung | P-15. |
| Dialoggeometrie / Theme | `place_window`, `calibration_dialog_geometry` | — | Lücke Web | nur Darstellung | Nicht übertragbar. |

## 12. Einstellungen

| Funktion | Desktop | Web | Status | Kategorie | Bemerkung |
|---|---|---|---|---|---|
| Theme | `DARK`/`LIGHT`, Config `theme` | `#theme-dark`/`#theme-light`, Config `theme` | gleich | nur Darstellung | Web: serverweit gespeichert. |
| Peak-Parameter | Sidebar „Peak detection“: `peak_savgol_window` 5–101, `peak_savgol_order` 1–6, `peak_prominence` 0–5000, `peak_min_distance_px` 1–500, `peak_max_count` 1–12 | Sidebar „Peak detection · 05“: dieselben Schlüssel (Web: `peak_prominence` ohne Obergrenze, `peak_min_distance_px` ohne Obergrenze) | gleich | Messung/Einstellung | `peak_min_height` (Standard 15) hat in keiner Oberfläche ein Bedienelement. |
| Fusion-Parameter | siehe Abschnitt 2 | siehe Abschnitt 2 | gleich | Messung/Einstellung | |
| `radiometric_calibration_factor` | Config (Standard 0 = unkalibriert); gelesen in `CIETab.update_measurement` | Config vorhanden; wird in der Web-Berechnung nicht gelesen | abweichend | Kalibrierung | Kein Bedienelement; B-01, P-17. |
| Filter-Parameter | Config `filter_baseline_frames`, `filter_display_smoothing` | gleiche Schlüssel | gleich | Messung/Einstellung | Kein Bedienelement. |
| Kalibrier-Erfassungsparameter | Config `calibration_capture_frames` u. a. (`calibration_*`) | — (kein Zugriff, kein Bedienelement) | Lücke Web | Kalibrierung | Gehört zu P-09/P-10. |
| Gerätespezifika LR-2T | Config `lr2t_wl_offset_nm`, `lr2t_flip_pixels`, `lr2t_min/max_integration_us`, `lr2t_spec_min/max_nm`; beim Verbinden vom Dashboard direkt in den Treiber geschrieben | gleiche Config; beim Verbinden nicht in den Treiber geschrieben | abweichend | Messung/Einstellung | B-05. Kein Bedienelement. |
| Gerätespezifika Ocean (HDX) | Config `ocean_*` (Offset, NL-Korrektur, AE-Ziel, Integrationsgrenzen, Spec-Bereich) | gleich | gleich | Messung/Einstellung | Treiber liest die Config selbst; kein Bedienelement. |
| Gerätespezifika PASCO | Config `pasco_wl_poly_coeffs`, `pasco_spec_min/max_nm` | gleich | gleich | Messung/Einstellung | Treiber liest die Config selbst. |
| Gerätespezifika Demo | Config `demo_*` | gleich | gleich | Messung/Einstellung | Kein Bedienelement. |

## 13. Lücken nach Kategorie

Richtung: **W** = Funktion fehlt im Web (Lücke Web), **D** = Funktion fehlt im Desktop (Lücke Desktop). Das Web kommuniziert über WebSocket `/ws/stream` (`set_config`, `pause`, `connect`) und REST `/api/...`; neue Endpunkte entstehen unter `/api/v1` und nutzen die gemeinsame Engine. Für Lücken der Richtung D wird der Vorschlag für die Desktop-Seite angegeben.

### Messung/Einstellung

| Nr. | Lücke | Richtung | Umsetzungsvorschlag | Status Umsetzung |
|---|---|---|---|---|
| P-01 | Heatmap-Puffergröße einstellbar (`heatmap_buffer_size`, Schritt 10; Änderung leert den Puffer) | W | Puffer in die Engine verlegen (ein Puffer für Anzeige und Export). `GET/PUT /api/v1/heatmap` bzw. `set_config`-Schlüssel `heatmap_buffer_size`; Web-Steuerelement „Buffer“ in der Heatmap-Leiste, Client hält keinen eigenen Puffer mehr (`HM_ROWS` entfällt). | offen |
| P-02 | Heatmap-Messintervall (`heatmap_measure_interval_ms`) und Span-Anzeige | W | Entkopplung des Zeitraffers in der Engine (Drosselung beim Einfügen in den Puffer); `set_config` mit `heatmap_measure_interval_ms`; Span-Label clientseitig aus Puffergröße, Intervall und `fps` berechnen. | offen |
| P-03 | Heatmap-Autosave (`heatmap_autosave_enabled`, `_dir`, `_fsync_s`) | W | Engine-Funktion mit `HeatmapAutosaveWriter` (serverseitig, `autosave.py` ist Qt-frei); `POST /api/v1/heatmap/autosave` (start/stop) mit Rückgabe des Dateipfads; Web-Checkbox „Autosave to disk“ mit Pfadanzeige. Hinweis: Datei liegt auf dem Server. | offen |
| P-04 | Heatmap leeren (`⟲ Clear`) | W | `POST /api/v1/heatmap/clear` bzw. WS-Nachricht `heatmap_clear`; Button in der Heatmap-Leiste. | offen |
| P-06 | Heatmap-Export: ein Frame, Auswahl sichtbar/gesamt, nur gefüllte Frames, einheitlicher Puffer | W | `GET /api/v1/heatmap/export?scope=visible\|full\|frame&index=…` aus dem Engine-Puffer (löst auch den Widerspruch zwischen Anzeigepuffer und `/api/export/csv?tab=heatmap`); `/api/export/csv` bleibt für Kompatibilität bestehen und liest denselben Puffer. | offen |
| P-07 | Exportverzeichnis und Dateinamen-Vorlage (`export_dir`, `export_filename_template`, Token) | W | Serverseitig: `export_paths.build_filename` für `Content-Disposition`-Dateinamen aller Downloads verwenden (Einstellung im Web-Abschnitt „Data“ mit Live-Vorschau über `GET /api/v1/export/filename-preview`); `export_dir` gilt für serverseitig gespeicherte Dateien (Autosave, Berichte, Kalibrierprotokolle, Jobs). Browser-Downloads bleiben Downloads. | offen |
| P-08 | Belichtungsgrenzen des Geräts im Bedienelement (Web fest 1–2500 ms, Schritt 50; Desktop gerätespezifisch, 2 Nachkommastellen) | W | `min_integration_us`/`max_integration_us` in `/api/status` und hello-Nachricht liefern; Web-Feld setzt `min`/`max`/`step` beim Verbinden. | offen |
| P-22 | Auswahl Anzeigeeinheit (`display_unit`: counts / counts/s) und Bestrahlungsstärkeeinheit (`irradiance_unit`) fehlt im Desktop | D | Zwei Comboboxen im Abschnitt „Processing“ des Desktops (Config-Schlüssel gleich); `CalibrationRuntime.refresh()` nach Änderung von `irradiance_unit` aufrufen. Web unverändert. | offen |
| P-25 | Web-Schalter „Response compensation“ (`spectral_response_compensation`) ohne Wirkung | D | Schalter im Web entfernen (Zustand folgt der Profilwahl, wie im Desktop); alternativ als Anzeige „aktiv“ belassen. | offen |

### Kalibrierung

| Nr. | Lücke | Richtung | Umsetzungsvorschlag | Status Umsetzung |
|---|---|---|---|---|
| P-09 | Gespeichertes Dunkelspektrum erfassen (`Capture dark`) | W | `POST /api/v1/dark` als Job (Frame-Tap über `calibration_capture.capture_dark`), Schritt `waiting_for_user` für das Abdecken bei Geräten ohne Shutter; Web-Button „Capture dark“ im Abschnitt „Processing“. | offen |
| P-10 | Kalibrierlauf: Lampenkalibrierung und Ableitung (Assistent mit Schritten, Benutzeraktionen, Plausibilitätsprüfung, Abbruch) | W | Job-API `POST /api/v1/calibration/run` mit `waiting_for_user` und `POST /api/v1/jobs/{id}/continue`; Ablauf aus `calibration_workflow.py` unverändert; Web-Dialog mit Formular (Optics, Notes, Input, Type, Reference, Integration, Frames), Schrittliste, Ergebnis-/Plausibilitätsanzeige mit Bestätigung des Speicherns trotz Warnung. | offen |
| P-11 | Wellenlängenkalibrierung (Linienlampe erfassen, Peaks zuordnen, Fit, Backup, auf Gerät speichern) | W | `POST /api/v1/calibration/wavelength/capture`, `…/fit`, `…/save` auf der Engine (Logik in `calibration_profiles`/`calibration_utils`); Web-Dialog mit Peak-Plot, editierbarer Linientabelle und Speichern-Bestätigung (Backup-Hinweis); Rückgabe der neuen Achse an alle Clients. | offen |
| P-12 | Referenzquellen verwalten (Liste, Plot, Import, Entfernen) | W | `GET /api/calibration/references` existiert; ergänzen `POST /api/v1/calibration/references` (Import, Multipart/JSON) und `DELETE …/{id}` (nicht für Built-ins); Web-Tabelle mit Plot. Hinweis: Löschen wird nicht als MCP-Werkzeug angeboten. | offen |
| P-13 | Dunkelspektren auflisten und verwalten | W | `GET /api/calibration/darks` existiert; Web-Tabelle (Integrationszeit, Frames, Mittelwert, Datum) im Kalibrierbereich; Löschen optional. Im Desktop ebenfalls nur über den Assistenten sichtbar. | offen |
| P-14 | Kalibrierprotokoll (PDF/CSV) abrufen | W | Protokolle als Job-Ergebnis über `GET /api/v1/jobs/{id}/artifacts/{name}`; Web-Link „Protocol“ nach Abschluss; „Open folder“ entfällt (Server-Ordner). | offen |
| P-15 | Legacy-Antwortkorrektur (Referenz/Messung → Kompensation → Profil speichern) | W | Entscheidung erforderlich: bewusst offen lassen (Legacy, im Desktop selbst als überholt markiert) oder über denselben Job-Mechanismus wie P-10 anbieten. Standardvorschlag: bewusst offen, in der Matrix dokumentieren. | offen |
| P-17 | Illuminance (lx) und Foot-candle (fc) im Color-Tab (Karten, Berichte), inkl. Auswertung von `radiometric_calibration_factor` im Nicht-Absolut-Fall | W | In der Engine denselben Lux-Pfad wie der Desktop verwenden (`processing.photometric_inputs` + `radiometric_calibration_factor`); Karten `Illuminance`, `Foot-candle` mit „—“ ohne Kalibrierung ergänzen; Berichtsendpunkt erhält den Faktor aus der Engine. Siehe B-01. | offen |

### nur Darstellung

| Nr. | Lücke | Richtung | Umsetzungsvorschlag | Status Umsetzung |
|---|---|---|---|---|
| P-05 | Heatmap Rewind/Scrub, Live/Pin, Spektrum des gewählten Frames, „T-x s ago“ | W | Setzt P-01 voraus (Puffer mit Zeitstempeln in der Engine). Client holt Spalten über `GET /api/v1/heatmap/frame?index=` oder hält die zuletzt gesendeten Frames; Slider wie im Desktop mit DVR-Nachführung (`_hm_scrub_advance`). | offen |
| P-16 | Profiltabelle (Status, gültiger Bereich/Segmente, Einheit, Erstellt, Plausibilität, aktiv) und vollständiger Status (Segmente, `implausible`) | W | Daten sind bereits in `GET /api/calibration/profiles` (`items`, `profiles`) und im Frame-Status enthalten; Tabellenansicht im Web-Kalibrierbereich, Status-Zeile um Segmente und Plausibilitätswarnung ergänzen. | offen |
| P-18 | Color-Tab: `Central λ`, u/v (CIE 1960), R9-Karte, 5-SDCM-Ellipse im CIE-Diagramm; Aufteilung in Unter-Tabs „CIE 1931“/„Color Rendering“ | W | Felder `central_nm`, `u60`, `v60` liegen im Frame-Payload (`color`); Karten und Ellipsenzeichnung im Canvas ergänzen; Unter-Tab-Aufteilung optional (Einzelkarten genügen). | offen |
| P-19 | Graue Lücken zwischen gültigen Segmenten (`valid_segments_nm`) im Scope | W | Das Frame-Payload enthält `valid_segments_nm`; `validRangePlugin` um Segmentlücken erweitern. | offen |
| P-20 | Versionsanzeige in der Statusleiste | W | `version.app_version()` in `/api/status` aufnehmen; Label in `.statusbar`. | offen |
| P-21 | PNG-Export: ganze Ansicht inkl. Achsen, Color-Tab und Filter-Tab | W | Plot-Canvas mit Achsenbeschriftung zusammensetzen (Offscreen-Canvas) und für Filter den Filter-Canvas wählen; alternativ serverseitig über die vorhandenen Berichts-Endpunkte. | offen |
| P-23 | Replay-Tab (CSV-Wasserfall abspielen) fehlt im Desktop | D | Optional im Desktop als Tab oder Dialog umsetzen; Format identisch (Heatmap-CSV `Time_Frame;λ…` bzw. `Frame;λ…`, 2-Spalten-Spektrum). Kein Handlungsbedarf für das Web. | offen |
| P-24 | Berichtsformate vereinheitlichen: Filter-PNG/CSV direkt im Desktop; Color-CSV-Schnellexport (`#btn-cie-csv`) nur im Web | D | Desktop: Filter-Bericht mit Format-Auswahl (PDF/PNG/CSV). Web: `#btn-cie-csv` entfernen oder auf `/api/color/report?format=csv` abbilden (gleiche Zeilen wie Desktop, `cs.color_report_rows`). | offen |

Anzahl nach Kategorie: Messung/Einstellung 9 (P-01, P-02, P-03, P-04, P-06, P-07, P-08, P-22, P-25), Kalibrierung 8 (P-09 bis P-15, P-17), nur Darstellung 8 (P-05, P-16, P-18 bis P-21, P-23, P-24). Gesamt 25.

Nicht als Lücke geführt (bewusst nicht übertragbar): `EXIT`-Button, Fenstergeometrie, Dialoggeometrie, Mobil-Sidebar `CTRL`, Statusleistenfelder `Backend`/`WS`.

## 14. Unterschiede in der Logik (Befund)

Hier stehen Funktionen, die in beiden Oberflächen vorhanden sind, aber unterschiedlich rechnen oder sich unterschiedlich verhalten. Die gemeinsame Engine (Phase 1) beseitigt sie; dieser Abschnitt listet nur auf.

| Nr. | Gegenstand | Desktop | Web |
|---|---|---|---|
| B-01 | Farbmetrik / Lux-Kalibrierfaktor | `CIETab.update_measurement`: bei absolutem Profil Spektrum in W/(m²·nm) mit `lux_calibration = 1.0` (`processing.photometric_inputs`), sonst `Config.radiometric_calibration_factor`; Lux/fc nur bei Faktor > 0; Berichte (PDF/PNG/CSV) erhalten `self._calib`. | `_fused_emit`: `cs.measure_all(wl, spd, lux_calibration=lux_cal)` nur im Absolutfall, sonst Aufruf ohne Faktor (Standard 0.0 → Lux/fc immer `NaN`); `radiometric_calibration_factor` wird nie gelesen. `POST /api/color/report` und `/api/tm30` erhalten die angezeigten Werte in der Anzeigeeinheit (z. B. µW/(cm²·nm), nicht umgerechnet) und ohne Faktor; der Bericht zeigt Lux als „(uncal.)“. Die Karten `Illuminance`/`Foot-candle` fehlen im Web. |
| B-02 | Drosselung der Farbmetrik | Overview/Chromaticity/Rendering: jeder 5. Frame, im GUI-Thread; TM-30/CQS im Hintergrund-Thread, 1,2 s, nur bei sichtbarem Unter-Tab. | Serverseitig jeder 8. Fusionsframe, unabhängig davon, ob ein Client den Color-Tab zeigt (Rechenlast im Erfassungspfad); TM-30 per `POST /api/tm30`, clientseitig 1,2 s. |
| B-03 | Heatmap-Puffer | Länge `heatmap_buffer_size` (Standard 100, Schritt 10), Einfügen nur im Messintervall `heatmap_measure_interval_ms`, Zeitstempel je Spalte, gemeinsame Farbpegel `(0, ideal_heatmap_max)`, Pufferleerung bei Größenänderung. | Anzeige: clientseitig `HM_ROWS = 240`, jedes Frame, Normierung je Zeile mit dem damaligen Y max, kein Zeitstempel, Verlust beim Neuladen der Seite. Server: separater Puffer mit `HEATMAP_HISTORY_SIZE = 100` für `/api/heatmap` (vom Client nicht genutzt) und CSV-Export; `/api/status.heatmap_size` meldet 100, die Anzeige hat 240. |
| B-04 | Dunkelkorrektur bei Geräten ohne Optical Black | Legacy-Pfad (`process_new_spectrum`): `dark = 1.00818 · ob_mean − 0.6377` (PASCO-Konstanten) für jedes Gerät, auch bei LR-2T, HDX und Demo (`ob_mean` ist dort ein Basislinien-Schätzwert). V2-Pfad desselben Dashboards: `(1.0, 0.0)` bei `SUPPORTS_OB = False`. | `state.ob_slope/ob_offset = (1.0, 0.0)` bei `supports_ob = False`, sonst PASCO-Konstanten; einheitlich über `processing.process_frame`. |
| B-05 | `wl_offset` / `flip` beim Verbinden | `connect_device` setzt vor `backend.connect()` mit `hasattr`-Prüfung `wl_offset_nm = lr2t_wl_offset_nm` und `flip_pixels = lr2t_flip_pixels` (gerätespezifischer Sonderfall außerhalb des Treibers). | `_connect` setzt beides nicht; es gelten die Treiberstandardwerte (`flip_pixels = False`, Offset 0). Hinweis zur Prüfung: `_device_lr2t.wavelength_array` addiert `lr2t_wl_offset_nm` zusätzlich aus der Config; mit dem Desktop-Setzen von `wl_offset_nm` kann der Offset auf der Cal-Datei-Achse doppelt wirken. |
| B-06 | Peak-Erkennung | Eigene Implementierung `_update_peaks` im Dashboard (nur bei `show_peaks`; im V2-Pfad `peak_detection=False`): Polynomgrad `min(order, window − 1)`, Y-Werte der Marker aus `current_averaged_pixels` (können `NaN` sein), Auswahl nach Prominenz, Beschriftung mit Wellenlänge. | `processing.detect_peaks` über `process_frame`: Polynomgrad `max(1, min(order, window − 1))`, Ergebnis im Frame (`peaks`); das Feld `prominence` enthält den Wert des geglätteten Signals, nicht die Prominenz. `peak_min_height` (Standard 15) in beiden aus der Config. |
| B-07 | Verarbeitungspfade | Zwei Pfade: Legacy inline (Dunkel, Despeckle, räumliche Glättung, Response-Gain; nach Dunkelabzug `np.maximum(0, …)`) und, falls `needs_shared_pipeline`, `processing.process_frame`. Despeckle-Code im Dashboard dupliziert `processing.apply_despeckle`. | Ein Pfad: immer `processing.process_frame` (`pipeline_kwargs`). |
| B-08 | Belichtung / Auto-Belichtung | Manuelle Belichtungsänderung schaltet Auto-Belichtung aus (Backend-Flag, Toggle, Config). Ausschalten von Fast Preview stellt sofort die Hauptbelichtung wieder her. Grenzen: Spinbox wird beim Verbinden auf `min/max_integration_us` des Geräts gesetzt. | Belichtungsänderung lässt `auto_exposure` unverändert (Auto-Belichtung überschreibt sie). Fast-Preview-Aus setzt nur das Flag. Server klemmt auf Gerätegrenzen, das Eingabefeld ist fest 1–2500 ms mit Schritt 50. Auto-Belichtung klemmt zusätzlich im Callback und meldet per WS-`config`-Nachricht. |
| B-09 | Auto-Y / Y-Skalierung | Sichtbarer X-Bereich, Hysterese (Neuskalierung nur bei Überschreitung oder < 70 %), Untergrenze `Y_AXIS_BOTTOM_MARGIN_ADC`; bei Einheiten ungleich counts untere Grenze 0; Diff-Overlay erweitert die Untergrenze. | Gesamtes Spektrum plus Overlays, Neuberechnung bei jedem Frame (`setData`), Untergrenze `min(−20, min · 1,1)`. |
| B-10 | Autoconnect | Kein Autoconnect. | Beim Serverstart wird das erste gefundene Gerät des ersten Backends verbunden (`_lifespan`). |
| B-11 | Konfiguration / Zustand | Zustand je Fenster (Heatmap, Overlays, Fusion-Instanz, `CalibrationRuntime`) im Dashboard. | Globaler Zustand im Prozess (`ServerState`): alle Clients teilen Theme, Config, Pause, Gerät; Overlays, Freeze, Heatmap-Anzeige sind je Client. |
| B-12 | Einheiten in Auslesung, Messcursor und Zwischenablage | Auslesung und Messcursor zeigen fest „ADC“; Zwischenablage-Kopf einheitenabhängig (`Value_<unit>`) und NaN-sicher. | Auslesung und Messcursor zeigen fest „ADC“; Zwischenablage-Kopf immer `Intensity_ADC`, maskierte Werte (`null`) lösen einen Fehler in `toFixed` aus. |
| B-13 | Eingangsspektrum für Filter, TM-30 und Berichte | Dashboard übergibt `disp` (NaN → 0) bzw. für Farbmetrik `spd` in W/(m²·nm). | Der Client übergibt die Frame-Werte (`null` bei maskierten Pixeln, Anzeigeeinheit) an `/api/filter`, `/api/tm30`, `/api/color/report`. |
| B-14 | Berichtsmetadaten | `report_meta` im Dashboard; Peak-ADC aus `current_averaged_pixels`. | `_report_meta` im Server (doppelte Implementierung); Peak-ADC aus Anfragedaten oder `current_pixels`. |
| B-15 | Wirkungsloser Web-Schalter | Kein Schalter; `spectral_response_compensation` wird durch die Profilwahl gesetzt und im Legacy-Pfad gelesen (`is_response_comp_enabled`). | Schalter schreibt `spectral_response_compensation`, die Pipeline liest den Wert nicht (`response_compensation = legacy is not None`); nur der CSV-Kopf `# Response compensation` zeigt den Config-Wert. |
