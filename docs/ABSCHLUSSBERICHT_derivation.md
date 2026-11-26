# Abschlussbericht – Ableitung mit Integrationszeit je Seite, Sättigungsschwelle, Projektregeln

Stand: 2026-11-26 (Zeitzone Europe/Berlin, UTC+01:00) · Branch `feature/derivation-itime` (von `future`) · kein Push

Grundlage: `CLAUDE.md`, `docs/BEFUNDE_hardwaretest.md`, Punkte 16, 18 und 19.

## 1. Umgesetzte Punkte

### Punkt 19 – Projektregeln
- `CLAUDE.md` unverändert committet (Commit „Point 19“).

### Punkt 18 – Sättigungsschwelle
- Neue Konfigurationsgröße `saturation_warn_frac` (Standard 0.90, auf 0,5…1,0 begrenzt) in `app_config.py` und `spectrometer_config.example.json`.
- `processing.saturation_warn_frac()` liest den Wert. `processing.exposure_level()` liefert zusätzlich `warn_frac`, `warn_pct_fs` und den Anzeigezustand `state` (`ok` / `warn` / `danger`).
- Desktop (`gui_dashboard.py`) und Web (`static/app.js`) zeigen die Aussteuerung nur noch aus `state` bzw. `warn_pct_fs` an. Die festen Werte 85 % (Desktop) und 90 % (Web) sind entfallen. Der Tooltip nennt die Schwelle.
- Kalibrierabläufe:
  - Das Auto-Belichtungs-Zielband (`calibration_target_frac_hi`) wird in `WorkflowConfig` auf `saturation_warn_frac` begrenzt.
  - Erreicht die Rohspitze einer Hellmessung die Schwelle, erscheint eine Warnung (Spektralkalibrierung und Ableitung).
- Auto-Belichtung:
  - Die Auto-Belichtung bei Messungen (`engine.measure`) verwendet dasselbe begrenzte Zielband.
  - Die Live-Auto-Belichtung (`engine.auto_expose`, `/api/v1/auto-exposure`, MCP) ist ebenfalls auf die Schwelle begrenzt.
- Der Text des MCP-Werkzeugs `measure` nennt `saturation_warn_frac` statt „0.3–0.9“.

### Punkt 16 – Ableitung einer Optik aus einer anderen
Kern (`calibration_workflow.DerivationWorkflow`, Qt-frei):
- **Integrationszeit je Seite.** `set_integration(which, integration_us=None)` mit `which` = `known` oder `new`. Ohne Vorgabe erfolgt Auto-Belichtung mit montierter Optik auf `calibration_target_frac_lo/hi`, sonst gilt der feste Wert. Der Legacy-Aufruf `set_integration(us)` setzt beide Seiten gleich.
- **Rechnung.** S_new = S_known · C_new / C_known mit Zählraten (counts/s) nach Linearisierung und Dunkelabzug. Die Linearitätskorrektur wirkt wie in der Spektralkalibrierung im Erfassungspfad (`calibration_capture`).
  - Der Linearitätszustand wird je Messung festgehalten und auf Gleichheit geprüft (`linearity_consistent`).
- **Dunkel je Seite.** Dunkel vor und nach bei der eigenen Integrationszeit, zeitlich interpoliert (Block-Dunkel wie in der Spektralkalibrierung).
  - Prüfung „Dunkel und Hell bei gleicher Integrationszeit“ je Seite (`itime_match_known/new`).
  - Dunkeldrift je Seite mit der bestehenden Warnschwelle.
- **Wiederholung der bekannten Seite.** Am Ende wird die bekannte Optik erneut gemessen.
  - Quellendrift pro Pixel (Rausch-Toleranz k·σ plus Grenzwert) und gesamt (p95, Median, Maximum, Mittel), Grenzwert `calibration_derivation_drift_pct` (Standard 2,0 %).
  - Pixel, die die Prüfung nicht bestehen, werden maskiert und begrenzen den gültigen Bereich.
  - Ist die Prüfung nicht bestanden, gilt derselbe Status wie bei einer nicht bestandenen Stabilitätsprüfung: Speichern nur nach Bestätigung (Desktop) bzw. mit `save_on_failed_checks` (API). Ein Ergebnis, das durch die Maskierung nicht mehr plausibel ist, wird als „nicht plausibel“ behandelt.
- **Protokolldatensatz.** `record()` (Art `derivation_run`) enthält beide Seiten mit Integrationszeit, Auto/fest, Aussteuerung (Hell und Wiederholung), Dunkelfolge, Linearitätskorrektur, Drift, Dunkeldrift, Prüfungen und Spektren.
- **Gemeinsame Texte.** `DERIVATION_SOURCE_HINT` (Quellenhinweis) und `DERIVATION_STEPS` (Schrittfolge) für Desktop und API.

Desktop (`gui_calibration.py`):
- Getrennte Integrationszeit (Auto oder ms) für die bekannte und die neue Optik.
- Quellenhinweis im Formular und im ersten Dialog.
- Schritte in der Reihenfolge: bekannte Optik, Integrationszeit, Dunkel, Hell, Umbau, neue Optik, Integrationszeit, Dunkel, Hell, Dunkel danach, Umbau zurück, Wiederholung, Dunkel danach, Berechnung, Speichern, Protokoll.
- Abdecken- und Freigeben-Dialoge erscheinen nur ohne Shutter.
- Die Ergebnisanzeige zeigt je Seite Integrationszeit, Aussteuerung, Linearitätskorrektur sowie Drift und Dunkeldrift.

Web/API:
- `POST /api/v1/calibration/derivations` als Job mit `waiting_for_user`. Aktionen in dieser Reihenfolge: `mount_known`, `cover`, `uncover`, `swap_optics`, `cover`, `uncover`, `cover`, `swap_back`, `cover`, `uncover`. Die Abdecken-Schritte entfallen bei Geräten mit Shutter.
- Neue Parameter `integration_known_ms`, `integration_new_ms` (Zahl > 0 oder `"auto"`) und `save_on_failed_checks`. `integration_ms` bleibt erhalten und gilt für beide Seiten, wenn keine seitenbezogenen Werte gesetzt sind.
- `GET /api/v1/calibration/runs/{job}/protocol` liefert auch das Protokoll von Ableitungsjobs.
- Web-Dialog mit getrennten Feldern, Quellenhinweis, Schrittliste und Ergebnisanzeige (beide Integrationszeiten, Aussteuerungen, Drift, Dunkeldrift, Protokoll-Links).

Protokoll (`calibration_report.write_derivation_protocol`): PDF und CSV, siehe Abschnitt 4.

## 2. Entscheidungen

1. **Dunkel nach der Hellmessung je Seite.** Die vorgegebene Schrittfolge ist um „Dunkel danach“ für die neue Optik (vor dem Rückbau) und für die bekannte Optik (nach der Wiederholung) erweitert. So umschließt jede Seite ihre Hellmessungen mit Dunkel vor und nach, wie in der Spektralkalibrierung. Die Dunkelmessungen danach sind im Kern optional; ohne sie wird das Dunkel davor verwendet.
2. **Auto-Belichtung bei freiem Lichtweg.** Die Auto-Belichtung einer Seite läuft nach dem Montieren der Optik und vor dem Dunkel, also bei freiem Lichtweg. Die Hinweistexte sagen das ausdrücklich.
3. **Bekannte Zählrate auf den Zeitpunkt der neuen Messung interpoliert.** Die Zählrate der bekannten Optik wird zwischen erster Messung und Wiederholung linear auf die Mitte der Messung mit der neuen Optik interpoliert (`C_known_used`). Das kompensiert eine lineare Quellendrift unterhalb des Grenzwerts. Die Driftprüfung bewertet die unkorrigierte Abweichung zwischen erster Messung und Wiederholung.
4. **Gesamtdrift über alle geprüften Pixel.** Die Gesamtdrift (p95) wird über alle geprüften Pixel gebildet, nicht nur über die, die die Einzelprüfung bestanden haben. Bei gleichmäßiger Drift fallen zuerst die signalstarken Pixel heraus; die verbleibenden rauschstarken Pixel würden die Drift sonst verdecken. Die Stabilitätsprüfung der Spektralkalibrierung ist unverändert, siehe Abschnitt 6.
5. **Grenzwert 2,0 %.** `calibration_derivation_drift_pct` steht auf 2,0 % und liegt damit über der Stabilitätsschwelle von 1,0 %. Zwischen erster Messung und Wiederholung liegen zwei Umbauten, also Minuten statt Sekunden.
6. **Anzeigezustand „danger“ nur bei Sättigung.** „danger“ gilt nur noch bei erreichter Sättigung (`n_saturated` > 0 bzw. Spitze ≥ Sättigungsniveau). Die feste Desktop-Grenze von 98 % FS und die Web-Grenze von 100 % sind entfallen.
7. **Nicht umgestellte Werte.**
   - Die Treiber-internen Notfallschwellen der Geräte-Auto-Belichtung (`_device_lr2t.py`, `_device_ocean.py`, `_device_pasco.py`: 0,92 × Sättigung; Demo 0,98 × FS) sind keine Warnschwellen und bleiben unverändert.
   - `linearity_frac_hi` (0,90) bleibt eigenständig: Die Linearitätskalibrierung braucht bewusst Stufen bis knapp unter die Sättigung.
8. **Demo-Backend.**
   - Neues Attribut `source_scale` (Durchlass der simulierten Optik).
   - `demo_min_integration_us` und `demo_max_integration_us` dürfen jetzt 10 µs bis 10 s betragen. Die Standardwerte (1 ms bis 1000 ms) sind unverändert.
   - Die Wartezeit zwischen Frames ist unterbrechbar, damit `stop()` bei langen Integrationszeiten zuverlässig endet.
9. **Ergebnisfeld `integration_us`.** Das Job-Ergebnis enthält weiterhin `integration_us`: die Integrationszeit der neuen Optik, bzw. die gemeinsame Zeit, wenn beide Seiten gleich sind. Im gespeicherten Profil bleibt `acquisition.integration_us` die Integrationszeit der neuen Optik.

## 3. Abweichungen vom Auftrag

- **Keine fachlichen Abweichungen.**
- **Commit-Zuschnitt.** Einige Dateien enthalten Änderungen zu Punkt 16 und Punkt 18 zugleich. Sie sind dem Commit des Punkts zugeordnet, der die Datei überwiegend betrifft:
  - `app_config.py` und `spectrometer_config.example.json` enthalten beide neuen Schlüssel und liegen im Commit zu Punkt 18.
  - Die Anpassung der Aussteuerungsanzeige in `static/app.js` liegt im Commit zu Punkt 16.
- **Kein PDF-Build des Handbuchs.** `docs/manual/manual.tex` ist ergänzt; ein PDF-Build war nicht möglich, weil `pdflatex` nicht installiert ist. `docs/manual/manual.pdf` ist daher nicht aktualisiert.

## 4. Schnittstellen- und Exportformat-Änderungen

1. **Neu: Ableitungsprotokoll-CSV** `derivation_<Gerät>_<Seriennr>_<Optik>_relative_<Zeit>.csv` (UTF-8).
   - Kopf: Zeilen `# key;value`, unter anderem `started`/`finished` (ISO mit Zeitzone), `t_int_known_us`, `t_int_new_us`, `t_int_*_mode`, `peak_known_pct_fs`, `peak_known_repeat_pct_fs`, `peak_new_pct_fs`, `dark_sequence_*`, `dark_light_same_t_int_*`, `linearity_correction_known/new`, `nl_correction`, `drift_pct_p95`, `drift_pct_mean`, `drift_pct_median`, `drift_threshold_pct`, `drift_pass`, `drift_masked_pixels`, `drift_pixels_tested`, `drift_seconds`, `drift_interp_weight`, `drift_cause`, Dunkeldrift je Seite, Plausibilität, gültiger Bereich.
   - Spalten: `Wavelength_nm;C_known_cps;C_known_repeat_cps;C_known_used_cps;C_new_cps;S_known;S_new;Valid;Drift_pct;Drift_fail`.
   - Maskierte und nicht endliche Werte sind leere Zellen. Dazu ein PDF-Protokoll mit denselben Angaben und Diagrammen (C normiert, S_known/S_new, Drift pro Pixel mit ±Grenzwert).
2. **Bestehende Protokolle (Spektral- und Linearitätskalibrierung).** `_num()` schreibt nicht endliche Zahlen jetzt als leere Zelle bzw. als den jeweiligen Ersatztext statt „nan“.
3. **Live-Frames (`/api/...` WebSocket und `/api/v1`).** Das Objekt `exposure` hat die neuen Felder `warn_frac`, `warn_pct_fs` und `state`. Die bestehenden Felder sind unverändert.
4. **`POST /api/v1/calibration/derivations`.**
   - Neue Felder `integration_known_ms`, `integration_new_ms`, `save_on_failed_checks`.
   - Werte ≤ 0 werden mit 422 abgewiesen. Das ist strenger als bisher: Das Modell hat solche Werte vorher nicht geprüft.
   - Das Job-Ergebnis hat die neuen Felder `integration_known_us`, `integration_new_us`, `peak_frac_known/new/repeat`, `drift`, `checks`, `dark_drift`, `source_hint`, `protocol`, `protocol_message`.
   - Neuer Status `not_saved_checks_failed`.
   - Neue Benutzeraktionen `swap_back` und `mount_known`. `mount_known` gab es zwar schon, die Reihenfolge ist aber erweitert.
5. **Gespeicherte abgeleitete Profile.**
   - `acquisition` hat zusätzlich `integration_known_us`, `integration_new_us`, `linearity_known`, `peak_frac_known/new/repeat`, `dark_sequence` (je Seite), `snr_k`, `max_gain`, `smooth_px`.
   - `checks` hat zusätzlich `drift_*`, `itime_match_*`, `linearity_consistent`, `dark_drift_counts`.
   - `derived_from` hat zusätzlich `known_version`.
   - Ältere Profile bleiben lesbar.
6. **Konfiguration.** Neue Schlüssel `saturation_warn_frac` (0.90) und `calibration_derivation_drift_pct` (2.0).

## 5. Testergebnisse

- `python -m pytest -q` (vollständig, `PS2600A_DATA_DIR` durch `tests/conftest.py` auf Temp) nach den Review-Korrekturen: **409 passed** in zwei aufeinanderfolgenden Läufen (je ca. 10 min), kein Absturz.
- Neue Tests:
  - `tests/test_derivation_workflow.py`: 1:1000 ohne Sättigung und ohne SNR-Maskierung, Quellendrift erkannt, Unabhängigkeit von den Integrationszeiten, Dunkel je Seite, spektrale Transmission, Grenzwert aus der Konfiguration, Zustand nach geänderter Integrationszeit, Randfälle von `set_integration`.
  - `tests/test_derivation_report.py`
  - `tests/test_gui_derivation.py`
  - `tests/test_api_derivation.py`
  - `tests/test_saturation_threshold.py`: unter anderem Desktop und Web mit demselben Wert und Zustand.
- **Demo-Durchlauf** über `/api/v1` mit `PS2600A_DATA_DIR` auf einem Temp-Verzeichnis, Demo-Backend mit `source_scale` 1 bzw. 1e-3 (Verhältnis 1:1000):
  - Integrationszeiten: bekannte Optik 775 µs, neue Optik 771 097 µs (Verhältnis 995).
  - Aussteuerung: 76,8 % / 77,7 % / 76,8 % FS (bekannt / neu / Wiederholung).
  - Drift: p95 0,13 %, Mittel +0,01 % über 44 s, bestanden, 0 maskierte Pixel.
  - Gültiger Bereich 380–780 nm, plausibel, Status `saved`, Protokoll PDF und CSV geschrieben.
- **Synthetische Gegenprobe mit 5 % Quellendrift:** Drift p95 5,6 % > 2 %, Prüfung nicht bestanden, signalstarke Pixel maskiert, Ergebnis nicht plausibel; es wird nichts gespeichert.

## 6. Review (review-opus) und Behandlung

Kritisch und wichtig (alle behoben):
1. **Absturz der vollen Testsuite** („access violation“ in einem Desktop-Test) bei noch laufendem Demo-Thread; trat in Teilmengen nicht auf.
   - Maßnahmen: Tooltip der Aussteuerung wird nur noch bei Änderung gesetzt statt bei jedem Frame. Der Demo-Thread wartet zwischen Frames unterbrechbar, und `stop()` wartet mindestens eine Integrationszeit.
   - Hinweis: Mein eigener voller Lauf vor dem Review brach nicht ab (404 passed, 1 instabiler Test, siehe unten). Die Ursache ist nicht abschließend eingegrenzt; Nach den Korrekturen liefen zwei volle Läufe hintereinander ohne Absturz durch (409 passed).
2. **Veraltete Zählraten nach erneutem `set_integration`.** Folgen waren ein Absturz in `compute()` oder eine Rechnung mit alten Raten.
   - Korrektur: Eine neue Integrationszeit verwirft die Messungen der Seite und alle daraus berechneten Größen. `compute()` stützt sich auf die vorhandenen Messungen. Tests ergänzt.
3. **Job-Ergebnisfeld `integration_us` entfallen.** Wieder geliefert, siehe Entscheidung 9.
4. **Protokoll nicht nachrechenbar.** Spalte `C_known_used_cps` und Kopfzeile `drift_interp_weight` ergänzt.
5. **Abschlussbericht fehlte.** Dieses Dokument.

Gering:
- Behoben:
  - NaN der Driftwerte wird zu None.
  - Protokoll ohne „nan“.
  - Gleicher Vergleich `>=` an der Warnschwelle im Assistenten.
  - Docstring und Web-Hinweistext zur Aktivierung bei fehlgeschlagenen Prüfungen.
  - Dunkeldrift im Web-Ergebnis.
  - Hinweistexte der API zum freien Lichtweg und zum letzten Freigeben.
  - Legacy-Erkennung mit `numbers.Real` (auch numpy-Zahlen).
  - `auto=False` ohne Wert wird abgewiesen.
  - Neues Dunkel davor setzt das Dunkel danach zurück.
  - Live-Auto-Belichtung auf die Schwelle begrenzt.
- Nicht behoben (bekannte Einschränkungen):
  - Der Quellenhinweis steht in `static/index.html` als Textkopie von `DERIVATION_SOURCE_HINT`. Im Job kommt er aus dem Kern (Aufforderung `mount_known`, Ergebnisfeld `source_hint`).
  - In den Abbruchpfaden des Desktop-Assistenten bleibt der Schritt „Protocol“ in der Schrittliste auf „waiting“, obwohl das Protokoll geschrieben wird.
  - Driftprüfung und Interpolation wirken gleichzeitig; siehe Entscheidung 3 (die Interpolation ist eine Restkorrektur unterhalb des Grenzwerts).

Außerdem behoben:
- Ein Desktop-Test zur nicht bestandenen Driftprüfung war instabil, weil eine echte Drift von 2,5 % je nach Rauschen entweder nur die Driftprüfung auslöste oder das Ergebnis unplausibel machte. Der Test erzwingt jetzt das Prüfergebnis, damit er nur den Bedienpfad prüft. Die Drifterkennung selbst ist in `tests/test_derivation_workflow.py` abgesichert.
- Gefunden wurde dabei die Gesamtdrift über nur die bestandenen Pixel (Entscheidung 4).

## 7. Bekannte Einschränkungen

- **Starke Drift.** Bei starker gleichmäßiger Quellendrift (≳ 3 %) maskiert die Pixelprüfung fast alle signalstarken Pixel. Das Ergebnis ist dann nicht plausibel (Status `not_saved_implausible`) oder die Rechnung bricht mit „No overlap …“ ab, statt `not_saved_checks_failed` zu liefern. Das entspricht der Vorgabe („Pixel begrenzen den gültigen Bereich bzw. Ergebnis nicht plausibel“).
- **Sättigung in der Wiederholung.** Wird die Quelle zwischen Messung und Wiederholung deutlich heller, kann die Wiederholung sättigen, und der Lauf endet mit einer Fehlermeldung.
- **Hardwaretest steht aus.** Die Ableitung ist nur mit dem Demo-Backend und synthetischen Daten geprüft. Der Gerätetest ist in `docs/BEFUNDE_hardwaretest.md` (Abschnitt „Hardwaretest ausstehend“) eingetragen.
- **Demo-Mindestzeit.** `demo_fullscale_ms` wird im Demo-Treiber auf mindestens 1 ms begrenzt. Tests mit 1:1000 brauchen daher Integrationszeiten bis etwa 0,8 s; die neuen Testdateien laufen zusammen etwa 3 Minuten.
- **Handbuch-PDF.** Siehe Abschnitt 3.

## 8. Widersprüche zwischen CLAUDE.md und Code (nur gemeldet, CLAUDE.md nicht geändert)

1. **„Maskierte Pixel … nie stillschweigend mit Gain 1“.** Altprofile mit `apply = legacy_gain` (migrierte v1-Tabellen, `calibration_profiles.APPLY_LEGACY_GAIN`) verwenden außerhalb ihres Bereichs weiterhin Gain 1. Sie sind in der Auswahl als „legacy“ gekennzeichnet und als bekannte Optik einer Ableitung gesperrt, werden aber angewendet.
2. **„Neue Prüfkriterien und Grenzwerte sind konfigurierbar“.** Nicht konfigurierbar sind die Toleranz „gleiche Integrationszeit“ (`calibration_capture.ITIME_REL_TOL` = 0,5 %) und der Faktor der Warnung „Dunkel ungewöhnlich hell“ (0,5 × Hellspitze). Beide Werte bestanden schon vorher und werden in der Ableitung wiederverwendet.
3. **Agentendefinitionen.**
   - `review-opus` verweist auf „den Plan (`docs/PLAN_*.md`)“ und auf `git diff origin/HEAD...HEAD`; CLAUDE.md sieht das Review gegen CLAUDE.md und den Auftrag vor, und der Standard-Branch ist `future`. Im Review-Auftrag wurde deshalb ausdrücklich gegen `future` und gegen CLAUDE.md und die Befundpunkte geprüft.
   - `impl-sonnet` nennt `docs/PLAN_radiometrische_kalibrierung.md` als Grundlage.
   - Die Agentendateien liegen unter `.claude/` und sind laut CLAUDE.md nicht zu committen; sie wurden nicht geändert.
4. **„Es gibt keinen exe-Build“.** `docs/PLAN_api_mcp.md` und `docs/TEST_api_mcp.md` (Abschnitt E) beschreiben noch einen PyInstaller-Build. Es handelt sich um historische Dokumente; der Code enthält keinen Build mehr.
5. **„Nie Laufzeitdaten lesen“.** Ein Hilfsskript eines Subagenten lief mit `PS2600A_DATA_DIR` auf Temp, aber ohne `PS2600A_SKIP_MIGRATION`. Die einmalige Migration hat dabei Laufzeitdateien aus dem Programmordner `C:\ps2600-git` (ältere, nicht versionierte Dateien) in das Temp-Verzeichnis kopiert. Die Originale wurden nicht verändert, `%APPDATA%` wurde nicht berührt. Prüfskripte sollten zusätzlich `PS2600A_SKIP_MIGRATION=1` setzen; `tests/conftest.py` tut das bereits.
