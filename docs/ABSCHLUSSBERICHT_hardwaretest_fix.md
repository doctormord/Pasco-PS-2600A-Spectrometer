# Abschlussbericht: Umsetzung der Befunde aus dem Hardwaretest

Branch `fix/hardware-test` (von `future` @ 91d533b), Stand 2026-11-25. Kein Push.
Grundlage: `docs/BEFUNDE_hardwaretest.md` (15 Punkte). Regeln: `docs/PLAN_api_mcp.md` §2.

## Commits

| Commit | Inhalt |
|---|---|
| Block A | Kalibrierablauf, Punkte 1–6 |
| Block B | PDF-Protokoll und gemeinsame Exportfunktion, Punkte 7–8 |
| Block C | Darstellungsarten, Punkte 9–13 |
| Block D | Repo und Benutzerdatenverzeichnis, Punkte 14–15 |
| Review | Behebung der Review-Befunde (review-opus) sowie Doku: Handbuch, Testcheckliste, dieser Bericht |

## Umgesetzte Punkte

1. **Gain-Spanne nur Hinweis**
   - `broadband_check` meldet die Spanne als `hints`; Grenze `calibration_max_gain_span` = 200×.
   - Eine alte Konfiguration mit dem Standardwert 40× wird einmalig auf 200× migriert (`config_version`).
   - Linienquellen erkennt der Check weiterhin über Abdeckung, Rauigkeit und isolierte Spitzen.
2. **Pixelprüfung (Kreuz- und Stabilitätsprüfung)** – `calibration_profiles.pixel_check`
   - Toleranz je Pixel: `k·√(σ_ref²+σ_test²) + Schwelle·|C|`; σ aus Dunkel- und Schrotrauschen der tatsächlichen Aufnahmen.
   - Durchfallende Pixel werden maskiert (`compute_calibration(pixel_ok=…)`) und begrenzen damit die gültigen Segmente.
   - Das Aggregat (p95) wird wie bisher berechnet, jetzt aber nur über die bestandenen Pixel.
3. **Dunkelmessung vor und nach jeder Hellmessung**
   - Dunkel bei t und t/2 vor den Hellmessungen, erneut danach (`capture_darks_after`).
   - Abzug je Pixel, zeitlich linear interpoliert auf die Mitte der Hellmessung (`calibration_capture.interpolate_dark`, Varianz wird mitgeführt).
   - Drift wird je t_int ausgewiesen (Counts, % des Peaks); Warnung oberhalb `calibration_dark_drift_warn_pct` und > 3σ.
   - Benutzerschritte cover/uncover in Desktop-Assistent, Job-API und Web.
4. **Ursachenmodell** – `check_causes`
   - Gewichteter Fit `N_test = a + s·x + c·x²/FS` über alle getesteten Pixel, mit einem robusten Refit.
   - Deutung: a = Dunkeldrift (Counts), s = Zeitversatz (µs) bei t/2 bzw. Lampendrift bei der Wiederholung, c = Nichtlinearität (% je FS).
   - Eine Ursache wird nur gemeldet, wenn sie signifikant ist und mindestens den Mindestbetrag erreicht.
   - Die Meldung mit Ursache und Betrag erscheint in Dialog, Job-Ergebnis, Web und Protokoll.
   - Bei der Kreuzprüfung wird eine in der Stabilitätsprüfung gefundene Lampendrift herausgerechnet.
   - Testdaten: drei synthetische Fälle mit je einer Ursache plus ein Fall ohne Ursache.
5. **Linearitätskalibrierung** – `calibration_linearity.py`, Reiter „Linearity“ (Desktop), Web-Panel, API, MCP
   - Messablauf:
     - 6–10 Stufen bei ca. 10–90 % FS (gedeckelt unter der Sättigung),
     - Dunkel bei allen t_int vor und nach den Stufen, zeitlich interpoliert,
     - Referenzstufe (≈ 50 % FS) am Anfang, nach jeder 3. Stufe und am Ende; daraus wird die Lampendrift herausgerechnet,
     - Aufnahme mit umgangener Korrektur (roh).
   - Modell:
     - `N/(t·L) = R_p·h(N)` mit `h = exp(Σ d_j (N/FS)^j)` und h(0) = 1; die Pixelrate R_p wird je Pixel eliminiert.
     - Korrektur `counts_true = N/h(N)`, gespeichert als Polynom und als monotone Stützstellentabelle.
     - Gespeichert mit Gültigkeitsbereich und Datum, je Seriennummer.
   - Anwendung (Begründung im Moduldocstring):
     - je Frame direkt nach dem Treiber in `BaseSpectrometer._dispatch_frame`, also vor Frame-Taps, Fusion, Mittelung, Fast-Preview-Skalierung und Dunkelabzug,
     - auf das Signal über der Frame-Basislinie (`b + f(raw−b)`); Dunkelframes durchlaufen dieselbe Korrektur,
     - gesättigte Pixel bleiben roh.
   - HDX (Werkskorrektur aktiv): nur Kontrollmessung, nie aktivierbar, keine doppelte Korrektur.
   - Profile speichern den Linearitätszustand (`acquisition.linearity`). Bei Abweichung erscheint ein Hinweis in der Statusmeldung und im Exportkopf.
   - Protokoll als PDF und CSV.
6. **Verifikation nach dem Speichern** (`engine.verify_profile`)
   - Die Referenzquelle wird mit dem neuen Profil durch die Live-Pipeline gemessen (Frame-Tap, `process_frame` mit denselben Parametern, gespeicherter Dunkel).
   - Abweichung normiert auf 550–650 nm, je 50-nm-Band und gesamt (rms, max); bei Absolutprofilen zusätzlich unnormiert.
   - Ergebnis in Dialog, Job und Protokoll.
   - Referenz-Overlay im Scope ein-/ausschaltbar, auf die aktuelle Darstellung skaliert (Desktop und Web).
7. **PDF-Protokoll** mit Fließlayout
   - Text- und Zellumbruch, gemessene Zeilenhöhen, Seitenumbruch, keine Kürzungen.
   - NL- und Linearitätskorrektur ausdrücklich „on“/„off“/„n/a“.
   - Neue Abschnitte: Dunkeldrift, Pixelprüfung, Ursachen, Hinweise, Verifikation.
   - Ein pdfplumber-Test prüft auf Überlappung von Wortboxen, auf Text außerhalb der Seitenränder und auf Text außerhalb von Tabellenzellen.
8. **Gemeinsame Exportfunktion** (`engine.export_snapshot` / `spectrum_csv` / `export_heatmap_csv` / `export_heatmap_frame_csv`), genutzt von Desktop und Web
   - UTF-8 mit BOM.
   - Kopf mit den tatsächlich angewendeten Einstellungen: Gerät, Seriennummer, Zeit mit Zeitzone, t_int, Mittelung, Glättung, angewendeter Dunkelmodus, NL-/Linearitätskorrektur, Profil-ID/Name/Version/Typ, Darstellungsart, Einheit, Segmente.
   - Maskierte Pixel als leere Zelle, im Kopf dokumentiert.
   - „Response compensation“ steht nur noch im Kopf, wenn die Legacy-Verstärkung tatsächlich aktiv ist.
9. **Darstellungsarten** Raw ADC, Corrected counts, Normalised, Count rate und Absolute (`processing.display_transform`)
   - Corrected counts = korrigierte Rate × t_int · 1/min(Faktor); damit gilt im empfindlichsten Pixel Corrected = Raw (netto). Standard bei aktivem Profil („auto“).
   - Absolute ist nur mit Absolutprofil verfügbar; nicht verfügbare Arten fallen zurück.
   - Konfigurierbar über `display_mode`, API `PUT /acquisition` und MCP `set_acquisition`.
   - CIE, CRI, Filter und Spektrum-Export verwenden immer die korrigierten Daten.
10. **Aussteuerung**: Balken „Peak NN % FS“ und blasse Rohkurve auf zweiter Achse (Desktop und Web), Grundlage `processing.exposure_level`.
11. **Y-Grenzen je Darstellungsart** (`y_limits_by_mode`, `heatmap_limits_by_mode`)
    - Gemeinsames Schema für Desktop und Web.
    - Beim Wechsel der Darstellungsart wird einmal automatisch skaliert bzw. die gespeicherte manuelle Grenze wiederhergestellt.
    - Die Heatmap folgt derselben Logik und wird beim Wechsel von Darstellungsart oder Einheit geleert.
12. **Messwertanzeige** mit aktiver Einheit (`processing.format_value`), „–“ für maskierte Pixel (Desktop und Web).
13. **Kalibrierdialog**: Der Ergebnisbereich ist ein eigener Splitter-Bereich (mindestens 6 Zeilen, scrollt) und wird nicht mehr vom Diagramm abgeschnitten; ein Layouttest prüft das.
14. **Repo**: `PS-2600A_Pro.spec` entfernt; INSTALL nur noch Start über Python und `requirements.txt`.
15. **Benutzerdatenverzeichnis** (`data_paths.py`)
    - Pfade: `%APPDATA%\PS-2600A`, `~/Library/Application Support/PS-2600A`, `$XDG_CONFIG_HOME/ps-2600a`.
    - Überschreibbar über `--data-dir` (auch beim Headless-Start) und `PS2600A_DATA_DIR`.
    - Migration beim ersten Start:
      - Dateien werden kopiert, nicht verschoben, und nie überschrieben (eine abweichende Programmordner-Version wird als `*.from-program-dir-<ts>.json` abgelegt),
      - Sicherung unter `backup/migration-<ts>/`, Protokoll in `migration.json` und `migration.log`,
      - Konfigurationspfade, die in den Programmordner zeigen, werden korrigiert,
      - Protokolle älterer Versionen werden nach `protocols/` übernommen.
    - Im Repo liegen nur `*.example.json`; die echten Dateien stehen in `.gitignore` und sind aus dem Index genommen (auf der Platte erhalten).
    - Standard-Exportordner ist `<Dokumente>/PS-2600A`, nie der Programmordner.
    - Tests laufen mit einem temporären Datenverzeichnis (conftest setzt die Umgebungsvariable vor jedem Import).

## Entscheidungen (selbst getroffen)

- **Dunkel vor/nach als Block:** Alle Dunkelmessungen liegen in einer Abdeckphase vor und einer nach den Hellmessungen (Kalibrierung: 2 cover/uncover-Zyklen statt 6). Jede Hellmessung ist damit bei gleichem t_int zeitlich eingeklammert und wird linear interpoliert. Bei Geräten ohne Shutter wären Dunkelmessungen direkt vor und nach jeder einzelnen Messung nur mit vielen Benutzerschritten möglich. Nichtlineare Drift innerhalb des Blocks wird nicht erfasst.
- **Referenz der Kreuzprüfung** ist die zeitlich nächste Hellmessung bei t (Wiederholung). Eine in der Stabilitätsprüfung signifikante Lampendrift wird auf den Zeitpunkt der t/2-Messung extrapoliert und herausgerechnet.
- **Pixeltoleranz:** `k = 4` plus die bisherige Prüfschwelle (1 % bzw. 2 %) als relativer Anteil. Die Gesamtentscheidung bestanden/nicht bestanden bleibt beim p95-Kriterium, berechnet über die bestandenen Pixel.
- **Linearitätsmodell:** Bezug ist das Ansprechverhalten bei kleinem Signal (h(0) = 1, also Corrected = Measured bei schwachem Signal). Oberhalb des Messbereichs gilt ein konstantes Endverhältnis. Die Korrektur wirkt auf das Signal über der Frame-Basislinie des Treibers (OB bzw. dunkelste Pixel); gefittet wird auf Netto = Hell − Dunkelframe. Der Unterschied (Dunkelstrom) ist bei kurzen Zeiten vernachlässigbar.
- **Fast Preview:** Alle Treiber skalieren Kurzbelichtungen selbst und melden `ratio = 1`. Deshalb rechnet die Basisklasse den Skalierfaktor nach der Treiberregel nach (`fast_preview_scale`), damit die Linearitätskorrektur auf dem echten Kurzbelichtungsniveau wirkt.
- **„Raw ADC“** ist die Pipeline ohne Profil, mit Dunkelkorrektur und Glättung wie eingestellt. Ohne Profil ist das bitgleich mit der bisherigen Anzeige.
- **„Count rate“** zeigt mit Profil die korrigierte Rate, in derselben Normierung wie „Corrected counts“.
- **Spektrum-Export:** Die Wertespalte enthält die korrigierten Daten (bisheriger Spaltenname). Zusätzlich gibt es `Display_<Einheit>`, falls die Darstellung abweicht, und `Raw_ADC`. Die Zeitreihe (Heatmap, Autosave) speichert die Darstellung und weist Darstellungsart und Einheit im Kopf aus.
- **Display-Modus:** `display_mode` braucht das Steuerrecht, weil er den gemeinsamen Zeitreihenpuffer leert.
- **Protokolle** liegen ohne konfigurierten Exportordner unter `<Datenverzeichnis>/protocols`, mit Exportordner wie bisher unter `<export_dir>/calibration`.
- **Linux-Datenverzeichnis** liegt gemäß Vorgabe unter `$XDG_CONFIG_HOME`, auch für Daten.
- **Wiederherstellung aus der Git-Historie:** `calibration_profiles.json` und `spectrometer_config.json` waren bisher versioniert und werden nach einem `git pull` aus dem Programmordner gelöscht. Die Migration stellt sie in diesem Fall aus der Git-Historie wieder her. Schlägt die Migration fehl, wird kein Marker geschrieben, damit sie beim nächsten Start erneut versucht wird.
- **Demo-Gerät:** zusätzliche Testeffekte `demo_nonlinearity` und `demo_dark_drift`, standardmäßig aus.
- **Testabhängigkeit:** `pdfplumber` steht als reine Testabhängigkeit in `requirements.txt`.

## Abweichungen

- **Bestehende Tests an die geänderten Vorgaben angepasst:**
  - Gain-Spanne ist kein Fehler mehr.
  - CSV-Kopf enthält immer „Unit“; maskierte Zelle als `;;`.
  - Neuer Reiter „Linearity“; Schrittindizes des Assistenten verschoben.
  - Protokollordner `protocols`.
  - CSV-Spalte `check_fail` im Protokoll.
  - Neue MCP-Werkzeuge.
- **Inhalt der Exporte geändert, Pfade unverändert.** Wer Dateien maschinell einliest, muss anpassen:
  - Legacy-CSV: Kopfschlüssel (z. B. „Dark mode (applied)“), zusätzliche Spalten, Wellenlänge mit 3 Nachkommastellen.
  - Heatmap-CSV: Spalte „Time_Frame“, BOM.
  - Protokoll-CSV: `nl_correction` jetzt on/off/n/a.
- **`DerivationWorkflow`** (Ableitung neuer Optik) verwendet weiterhin nur Dunkelmessungen vorher.
- **Desktop-Sättigungswarnung:** Eine eigene Sättigungswarnung gab es im Desktop vorher nicht. Sättigung zeigt jetzt der Aussteuerungsbalken (Danger-Farbe, „N px saturated“). Die Schwellen unterscheiden sich zwischen Desktop (85/98 %) und Web (90/100 %).
- **Verifikation nachträglich** ist über Web und API möglich, im Desktop nur im Assistenten.

## Testergebnisse

- `python -m pytest -q` (QT_QPA_PLATFORM=offscreen): **378 passed** nach den Review-Korrekturen (davor 372; Ausgangsstand `future`: 276).
- **Neue Testdateien:**
  - `test_hardwaretest_calibration.py`
  - `test_calibration_report_layout.py`
  - `test_export_shared.py`
  - `test_display_modes.py`
  - `test_data_paths.py`
  - `test_gui_hardwaretest_a.py`
  - `test_gui_display_modes.py`
  - `test_web_hardwaretest_a.py`
  - `test_web_display_modes.py`
- **Demo-Durchlauf** (Engine-Jobs, isoliertes Datenverzeichnis, Demo mit 2 %/FS Nichtlinearität und 5 Counts/s Dunkeldrift):
  - Linearität: gespeichert und aktiv, max. Abweichung 1,61 %, Restfehler rms 0,32 → 0,18 %.
  - Spektralkalibrierung: gespeichert, Segment 380–780 nm, Dunkeldrift +37 Counts erkannt und interpoliert, keine signifikante Ursache in den Prüfungen.
  - Verifikation: rms 0,33 %; je Band ≤ 0,6 % (350–400 nm +0,57 %, 400–450 nm +0,23 %, sonst ≤ 0,05 %).
  - PDF- und CSV-Protokolle in `<Datenverzeichnis>/protocols`; CSV-Kopf vollständig.
- **Review (review-opus):**
  - 1 kritischer Befund (Datenverlust bei `git pull`) und 8 wichtige Befunde (W1–W8), alle behoben und mit Tests abgedeckt:
    - Sättigung bleibt trotz Linearitätskorrektur sichtbar,
    - Fast-Preview-Faktor,
    - y-Grenzen-Schema Web/Desktop,
    - Display-Modus braucht Steuerrecht,
    - maskierte Heatmap-Zellen leer und dokumentiert,
    - Linearitätshinweis in der Statusmeldung,
    - Migrationsmarker nur ohne Fehler,
    - Einzelframe-Export über die gemeinsame Funktion.
  - Von den geringen Befunden behoben:
    - Nichtlinearitätsbetrag bei t_ref/t_test ≠ 2,
    - `compute()` finalisiert die Prüfungen,
    - Verifikations-Job mit Job-Eigentümer,
    - Handbuch und README zur Datenart von Zeitreihe und Export.

## Bekannte Einschränkungen

- **Ungetestet auf echter Hardware:** LR-2T, HDX und PASCO wurden nicht gemessen → `docs/TEST_hardwaretest_fix.md`.
- **Web-Oberfläche** nur statisch und über die API geprüft, nicht im Browser.
- **Handbuch** nicht kompiliert (kein LaTeX auf dem Rechner; es braucht xelatex/lualatex). Statisch geprüft: Umgebungen und Klammern ausgeglichen, Labels aufgelöst.
- **Exportkopf:** „Frames averaged“ und „Smoothing“ werden aus der aktuellen Konfiguration gelesen, nicht aus dem exportierten Frame. Das kann abweichen, wenn die Einstellung nach dem Einfrieren geändert wurde.
- **WS-Datenmenge:** Die Frames enthalten zusätzlich `display` und `raw`, das ist im LAN etwa dreimal so viel Datenmenge.
- **Alter Protokollordner** `%LOCALAPPDATA%\PS-2600A\calibration` (Fallback früherer Versionen) wird nicht migriert.
- **Eigene relative Dateinamen** in der Konfiguration (andere als die Standardnamen) werden nicht migriert.
- **Benutzerkonfiguration während der Entwicklung:** Die Datei `spectrometer_config.json` im Programmordner wurde bei Testläufen einmal von der neuen Konfigurationsmigration umgeschrieben (`config_version`, neue Standardschlüssel, Gain-Spanne 40 → 200). Sie liegt weiter im Programmordner (jetzt unversioniert) und wird beim ersten Start ins Datenverzeichnis kopiert.
- **Migration auf diesem Rechner bereits gelaufen:** Ein Prüflauf außerhalb von
  pytest hat am 2026-11-25 um 18:14 die echte Erst-Migration ausgelöst. Ziel war
  `C:\Users\Christian\AppData\Roaming\PS-2600A`.
  - Kopiert wurden Konfiguration, Profile, `.v1.bak`, Dunkelspektren, Lampenbibliothek
    und `old_calib_data_*`, dazu 4 alte Protokolle nach `protocols/`.
  - Die Sicherung liegt unter `backup\migration-20261125_181452`, das Protokoll in
    `migration.log`.
  - Die Originale im Programmordner sind unverändert.
  - Der Hardwaretest prüft also den Zustand nach dieser Migration. Für einen
    frischen Migrationstest das Datenverzeichnis umbenennen.
