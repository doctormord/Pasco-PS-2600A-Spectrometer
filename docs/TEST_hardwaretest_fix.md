# Wiederholter Hardwaretest nach den Korrekturen (Branch fix/hardware-test)

Checkliste zum Abhaken. Grundlage: `docs/BEFUNDE_hardwaretest.md` (Punkte 1-15). Handbuch: Kapitel "Spectral calibration", "The graphical interface" (Display modes), "Data directory & migration"; Installation: `INSTALL.md`.
Geräte: LR-2T (S/N ASQ_SPC5636146), Ocean HDX, PASCO PS-2600A. Referenz: HAL-CAL 1808001 (`.lmp`), blanke Faser M25L02 200 µm NA 0,22 bzw. Cosinuskorrektor.
Je Gerät durchführen; "n/a" eintragen, wenn nicht vorhanden. Eintragen: Datum, Gerät/Seriennummer, Messwerte, Auffälligkeiten.

| Gerät | LR-2T | HDX | PASCO |
|---|---|---|---|
| Seriennummer | | | |
| Datum | | | |

Start nur über Python: `python PS-2600A_Pro.py` (Desktop) bzw. `python PS-2600A_Pro.py --headless` (Web/API). Datenverzeichnis: `%APPDATA%\PS-2600A\` (Windows), sonst siehe `INSTALL.md`.

## 0. Vorbereitung

- [ ] Branch `fix/hardware-test`, Stand notiert (`git rev-parse --short HEAD`): ______
- [ ] `python -m pytest -q` grün (Ergebnis): ______
- [ ] Alte Laufzeitdateien im Programmordner gesichert (Kopie mit Datum): `spectrometer_config.json`, `calibration_profiles.json`, `calibration_profiles.v1.bak.json`, `dark_spectra.json`, `reference_sources.json`, `old_calib_data_*.json`
- [ ] Zeitstempel/Hashes der Originale im Programmordner notiert (Abschnitt 1): ______
- [ ] Lampe (HAL-CAL) mindestens 15 min eingebrannt, Gerät warmgelaufen
- [ ] Kein anderes Programm hält das Gerät; Datenkabel angeschlossen

## 1. Erster Start und Migration (Punkt 14, 15)

Einmalig, danach je Gerät nur noch "Profile vorhanden" prüfen. Für den Test ggf. das Datenverzeichnis vorher umbenennen oder mit `--data-dir <leerer Ordner>` starten.

- [ ] `PS-2600A_Pro.spec` ist nicht mehr vorhanden; `INSTALL.md` beschreibt nur Start über Python (kein PyInstaller/exe) -- erwartet: keine Treffer für "pyinstaller" in `INSTALL.md`, `README.md`, `README_WEB.md`
- [ ] Erster Start mit leerem Datenverzeichnis: Programm startet ohne Fehlermeldung
- [ ] Datenverzeichnis angelegt (`%APPDATA%\PS-2600A\`) und enthält: `migration.json`, `migration.log`, `backup\migration-<Zeitstempel>\`
- [ ] Profile und Einstellungen nach dem ersten Start vorhanden: Profilliste (Kalibrierdialog, Reiter "Profiles") zeigt die bisherigen Profile; Einstellungen (Exposure, Anzeige, Remote) wie vorher
- [ ] Gespeicherte Dunkelspektren und Lampenbibliothek (HAL-CAL) vorhanden
- [ ] `migration.log` listet jede kopierte Datei; `backup\migration-...\` enthält Kopien aller Quelldateien
- [ ] Originale im Programmordner unverändert (Größe/Änderungsdatum/Hash wie in Abschnitt 0)
- [ ] Neue Dateien (neues Profil, Dunkelspektren, Protokolle, Konfiguration) entstehen nur im Datenverzeichnis, nicht im Programmordner (Kontrolle: Programmordner nach einer Kalibrierung unverändert)
- [ ] Zweiter Start: keine erneute Migration (`migration.log` ohne neuen Lauf, nichts überschrieben)
- [ ] Bei vorhandener gleichnamiger Datei im Datenverzeichnis: Programmordner-Version liegt als `<name>.from-program-dir-<Zeitstempel>.json` daneben, vorhandene Datei unverändert
- [ ] Start mit `--data-dir <anderer Ordner>` und mit `PS2600A_DATA_DIR`: es wird dieser Ordner verwendet (Vorrang: Option vor Umgebungsvariable)
- [ ] Vorlagen im Repo: `*.example.json` vorhanden; echte Dateien nicht im Repo (`git status` sauber, `.gitignore` greift)
- [ ] Standard-Exportordner: `<Dokumente>\PS-2600A` (nicht der Programmordner); ein CSV-Export landet dort
- [ ] Headless-Start (`--headless`) nutzt dasselbe Datenverzeichnis (Profile über API sichtbar)

## 2. Kalibrierablauf (Punkt 1-4, 13)

Je Gerät eine relative Kalibrierung mit HAL-CAL durchführen (Reiter "Spectral calibration"). Ausgangsprofil und Optik notieren: ______

### 2.1 Ablauf und Dialog

- [ ] Reihenfolge der Schritte: Gerät/Optik/Referenz -> Wellenlängenprüfung -> Integrationszeit -> Dunkel vor -> Kalibrierung + Wiederholung -> t/2 -> Dunkel nach -> Prüfungen -> Berechnung -> Speichern -> Verifikation
- [ ] Aufforderungen zum Abdecken/Freigeben erscheinen zweimal (vor und nach den Hellmessungen) und sind eindeutig
- [ ] Erste Ergebniszeile ("Valid segments ...") wird im Dialog vollständig angezeigt, nicht vom Diagramm abgeschnitten (Punkt 13)

### 2.2 Dunkel vor/nach, Drift (Punkt 3)

- [ ] Dunkelspektren bei t und t/2 vor und nach den Hellmessungen aufgenommen
- [ ] Dunkeldrift (Mittelwert in Counts, Rauschen, Zeitabstand) wird angezeigt und im Protokoll festgehalten; Wert notieren: ______ Counts
- [ ] LR-2T: Dunkelversatz zwischen Läufen (bisher +70 ... -200 Counts) wird durch die zeitliche Interpolation abgefangen; Kreuzprüfung ohne Dunkeldrift-Ursache bei guter Messung
- [ ] Künstlich provoziert (Dunkel nach kurz mit Licht-Leck oder lange Pause): Warnung "Dunkeldrift" mit Betrag erscheint

### 2.3 Pixelweise Prüfungen und Maskierung (Punkt 2)

- [ ] Stabilitäts- und t/2-Prüfung werden pro Pixel mit rauschabhängiger Toleranz ausgewertet; Anzahl durchgefallener Pixel je Prüfung angezeigt: Stabilität ______ / t/2 ______
- [ ] Durchfallende Pixel sind im Ergebnis maskiert und begrenzen die gültigen Segmente (keine Pixel mit C_half/C_cal bis 1,75 oder negativ mehr im gültigen Bereich)
- [ ] Gültige Segmente (nm) im Ergebnis und im Protokoll identisch: ______
- [ ] Schwache Randpixel fallen nicht allein wegen Rauschens durch (Rauschterm)

### 2.4 Kreuzprüfung mit Ursachenmodell (Punkt 4)

- [ ] Meldung nennt Ursache und Betrag (z. B. "Dunkeldrift +x Counts", "Zeitversatz", "Lampendrift", "Nichtlinearität x %")
- [ ] Konstanter Count-Versatz (Dunkel verfälscht, z. B. Dunkel mit Restlicht) -> Ursache "Dunkeldrift/Dunkelabweichung"
- [ ] Gleichmäßiger Faktor beim t/2-Vergleich -> "Zeitversatz"; beim Wiederholen mit t -> "Lampendrift"
- [ ] Signalabhängige Abweichung (LR-2T ohne Linearitätskorrektur, ca. +1,5 % zwischen 39 und 78 % FS) -> "Nichtlinearität" mit Verweis auf die Linearitätskalibrierung
- [ ] Meldung ist verständlich formuliert (Ursache, Betrag, Abhilfe)

### 2.5 Gain-Spanne (Punkt 1)

- [ ] Gain-Spanne erscheint als Hinweis (Standardgrenze 200x), blockiert das Speichern nicht
- [ ] Halogenlampe auf Si-Detektor (typisch 50-100x) wird ohne Fehler gespeichert
- [ ] Alte Konfiguration mit `calibration_max_gain_span` = 40 wird einmalig auf 200 gesetzt; ein vom Benutzer geänderter Wert bleibt erhalten
- [ ] Ungeeignete Referenz (Linienquelle, z. B. Hg/Cd-Lampe als Referenz) wird über Abdeckung/Rauigkeit/isolierte Spitzen abgelehnt bzw. gewarnt

### 2.6 Speichern

- [ ] Profil als neue Version gespeichert (Datenverzeichnis); Protokoll (PDF/CSV) unter `protocols\`
- [ ] Profil trägt den Linearitätszustand (an/aus/Kennung) der Messung

## 3. Linearitätskalibrierung (Punkt 5)

### 3.1 LR-2T und PASCO: Kalibrierung

- [ ] Reiter "Linearity": Zustand "none"/"active"/"off" und gespeicherte Datensätze der Einheit (Seriennummer) sichtbar
- [ ] Ablauf: stabile Breitbandquelle, 6-10 Integrationszeiten (ca. 10-90 % FS, unterhalb Sättigung), Dunkel vor/nach, Referenzschritt (ca. 50 % FS) am Anfang, nach jedem dritten Schritt und am Ende
- [ ] Lampendrift aus den Referenzschritten wird ausgewiesen (Warnschwelle 1 %): ______ %
- [ ] Ergebnis: max. Abweichung / Gültigkeitsbereich / Restfehler angezeigt; LR-2T ca. +1,5 % zwischen 39 und 78 % FS bestätigt: ______ %
- [ ] Protokoll (PDF/CSV) erzeugt, Plot lesbar
- [ ] Korrektur aktivierbar; Datensatz wird pro Seriennummer gemerkt (nach Neustart und Wiederverbinden aktiv)
- [ ] Mit aktiver Korrektur: t/2-Kreuzprüfung der spektralen Kalibrierung besteht ohne Ursache "Nichtlinearität"
- [ ] Anzeige bei schwachem Signal unverändert (corrected = gemessen im unteren Bereich)
- [ ] Korrektur ausschaltbar ("off"); Zustand erscheint in Protokoll/Export als "on"/"off"
- [ ] Profil, das mit anderem Linearitätszustand erstellt wurde, zeigt den Hinweis "Linearitätskorrektur war an/aus ... neu kalibrieren" (kein Block)

### 3.2 HDX: nur Kontrolle (kein Doppelabzug)

- [ ] Zustand "factory"; Ablauf bietet nur eine Kontrollmessung an
- [ ] Kontrollmessung zeigt Restabweichung der werkseitigen Linearisierung: ______ %
- [ ] Es wird keine gespeicherte Korrektur zusätzlich angewendet (Export: NL-Korrektur (Gerät) "on", Linearity correction "n/a (factory)")
- [ ] `ocean_nonlinearity_correction` aus: Zustand wechselt, Profile zeigen den Hinweis zu geändertem Linearitätszustand; wieder einschalten

## 4. Verifikation (Punkt 6)

Nach dem Speichern bzw. später über Reiter "Profiles" (Desktop), Web, API.

- [ ] Verifikation startet nach dem Speichern: HAL-CAL freigegeben, Messung durch die Live-Pipeline (gespeichertes Dunkel bei t des Profils)
- [ ] Ergebnis: Abweichung je 50-nm-Band und gesamt (rms, Maximum), normiert 550-650 nm
- [ ] Erwartete Toleranzen (aus den Befunden): 425-949 nm <= +-1,2 %; 400-425 nm ca. +1,9 %; 375-400 nm ca. +4,9 %. Gemessen: ______
- [ ] Bänder oberhalb 2 % sind markiert (Warnung), Randbereiche (< 425 nm) plausibel erklärt (Lampenleistung, Streulicht)
- [ ] Absolutprofil: zusätzlich unnormierte Abweichung (falls getestet): ______
- [ ] Verifikation für ein älteres v2-Profil nachträglich startbar
- [ ] Referenzkurve als Overlay im Scope ein-/ausschaltbar; im Modus "Corrected counts"/"Normalised"/"Count rate" auf die Messung skaliert (Kurven decken sich im Bereich 550-650 nm); im Modus "Absolute" unskaliert in der Anzeigeeinheit
- [ ] Verifikation ohne Lampe/mit abgedeckter Faser: verständliche Fehlermeldung

## 5. Darstellungsarten (Punkt 9-12)

Je Gerät mit aktivem Relativprofil prüfen, danach Absolutprofil (falls vorhanden) und ohne Profil.

### 5.1 Auswahl und Definition

- [ ] Auswahl "Display" (Raw ADC, Corrected counts, Normalised, Count rate, Absolute) in Desktop und Web vorhanden
- [ ] Ohne Profil: nur Raw ADC, Normalised, Count rate wählbar; mit Relativprofil zusätzlich Corrected counts; "Absolute" nur mit Absolutprofil
- [ ] Standard ("Auto") mit aktivem Profil: Corrected counts; ohne Profil: Raw ADC (bzw. Count rate bei `display_unit` counts_per_s)
- [ ] Corrected counts: im empfindlichsten Bereich entspricht die Kurve den Rohcounts (Corrected = Raw im Pixel maximaler Empfindlichkeit): ______
- [ ] Normalised: Maximum = 1 über die gültigen Pixel
- [ ] Count rate: Counts/s, Wert = Counts / Integrationszeit
- [ ] Absolute: Strahlungseinheit, Einheit korrekt (uW/cm2/nm bzw. W/m2/nm)
- [ ] Moduswechsel während der Messung ohne Fehler; Auswertungen (CIE, CRI, TM-30, Filter, Peaks) liefern dieselben Werte in jeder Darstellungsart
- [ ] Heatmap folgt der Darstellungsart

### 5.2 Aussteuerungsanzeige und Sättigung (Punkt 10)

- [ ] In korrigierten Ansichten bleibt die Aussteuerung in ADC sichtbar (Balken bzw. blasse Rohkurve): Spitzenwert, % FS
- [ ] Integrationszeit erhöhen bis Sättigung: Warnung erscheint, gesättigte Pixelzahl angezeigt; in jeder Darstellungsart
- [ ] Bei Raw ADC verhält sich die Anzeige wie zuvor

### 5.3 Y-Grenzen und Heatmap (Punkt 11)

- [ ] Manuelle Y-Grenze wird je Darstellungsart gemerkt (Wert in Raw setzen, in Normalised anderen Wert, zurückwechseln: Werte bleiben)
- [ ] Kalibrierte Einheiten: Auto-Skalierung; Neu-Skalierung beim Wechsel der Darstellung (keine abgeschnittene oder winzige Kurve)
- [ ] Heatmap-Obergrenze folgt derselben Logik (je Darstellungsart, nach Wechsel passend)
- [ ] Nach Neustart sind Darstellungsart und Grenzen wiederhergestellt

### 5.4 Messwertanzeige (Punkt 12)

- [ ] Anzeige zeigt die aktive Einheit (counts, counts (corrected), counts/s, normalised, uW/cm2/nm), nicht fest "ADC"
- [ ] Cursor/Hover über maskierten Bereich (außerhalb gültiger Segmente): "–" statt "nan"; gilt für Scope, Heatmap-Crosshair und Kopfleiste
- [ ] Web: gleiches Verhalten (Einheit, "–")

## 6. Protokoll und Export (Punkt 7, 8)

### 6.1 PDF-Protokoll (Punkt 7)

Kalibrierprotokoll und Linearitätsprotokoll öffnen und prüfen:

- [ ] Kein Überlappen: Warnungen/"Saved profiles", Kopfdaten und Diagrammtitel getrennt
- [ ] Keine abgeschnittenen Tabellenzellen (lange Texte umgebrochen)
- [ ] "NL correction" ausdrücklich "on", "off" oder "n/a" (HDX: Gerät "on", LR-2T/PASCO: "n/a" bzw. Linearitätskorrektur "on"/"off")
- [ ] Prüfergebnisse enthalten Ursache und Betrag; Dunkeldrift ausgewiesen
- [ ] Verifikationsergebnis im Protokoll bzw. getrennt gespeichert

### 6.2 CSV-Export (Punkt 8)

- [ ] Datei in Excel (Doppelklick) öffnen: Umlaute, µ, ², Gedankenstriche korrekt (UTF-8 mit BOM), Spalten getrennt (`;`)
- [ ] Kopf enthält: Spectrometer, Serial, Date/time (ISO mit Zeitzone), Exported, Integration time (ms), Frames averaged, Smoothing, Dark correction, Dark mode (applied) (tatsächlicher Modus, z. B. stored/optical_black), Optical Black mean, NL correction (device), Linearity correction, Calibration profile / Profile id / Profile name / Profile version / Profile type / Profile state, Display mode, Display unit, Unit, Valid range / Valid segments, Masked pixels
- [ ] Bei aktivem Profil kein "Response compensation;OFF" (widersprüchlicher Legacy-Eintrag)
- [ ] Angaben stimmen mit den tatsächlich verwendeten Einstellungen überein (Dunkelmodus, Integrationszeit, Mittelung, Glättung)
- [ ] Datenspalten sind korrigierte Daten, unabhängig von der Darstellungsart; maskierte Pixel leer
- [ ] Heatmap-CSV enthält die gekürzte Kopfzeile (Gerät, Seriennummer, Profil, Linearität, Darstellung)
- [ ] Export aus Desktop und Web liefern denselben Kopf und dieselben Werte

## 7. Web-Parität

`python PS-2600A_Pro.py --headless` (oder Fernzugriff in der App), Browser auf `http://localhost:8000`.

- [ ] Darstellungsarten, Einheitenanzeige, "–" für maskierte Pixel wie am Desktop
- [ ] Aussteuerungsanzeige, Sättigungswarnung und Y-Grenzen je Darstellungsart wie am Desktop
- [ ] Referenz-Overlay und Verifikationsergebnis (Bandabweichungen) sichtbar
- [ ] Linearitätszustand sichtbar (none/active/off/factory); Linearitätslauf startbar (LR-2T/PASCO), Aufforderungen Abdecken/Freigeben im Browser bestätigbar
- [ ] CSV-Download identisch zum Desktop-Export (UTF-8 mit BOM, Kopf)
- [ ] Profilliste und Dunkelspektren entsprechen dem Datenverzeichnis

## 8. API und MCP Smoke-Test

- [ ] `GET /api/v1/status`, `GET /api/v1/acquisition`: `display_mode` enthalten
- [ ] `PUT /api/v1/acquisition` mit `display_mode` = `normalised`: Antwort und Live-Anzeige wechseln; nicht verfügbarer Modus (`absolute` ohne Absolutprofil) -> Rückfall auf corrected
- [ ] `POST /api/v1/calibration/verifications` (`profile_id`, `reference_id`): Job (202), nach Abschluss `GET /api/v1/calibration/verification` mit Bandabweichungen
- [ ] `GET /api/v1/calibration/reference-overlay`: Referenzkurve passend zur aktuellen Darstellung
- [ ] `GET /api/v1/calibration/linearity`: Zustand und Datensätze der Einheit
- [ ] `POST /api/v1/calibration/linearity/runs` (LR-2T/PASCO): Job pausiert bei Abdecken/Freigeben (`waiting_for_user`), `POST /jobs/{id}/continue`, Ergebnis mit Protokollpfad; HDX: nur Kontrollmessung
- [ ] `POST /api/v1/calibration/linearity/select` (Datensatz aktivieren / `null` = aus)
- [ ] MCP: `set_acquisition` mit `display_mode`; `verify_profile`; `linearity_status`; `start_linearity` (Job, `continue_job` erst nach Bestätigung)
- [ ] MCP/API schreiben Daten ins Datenverzeichnis bzw. in `SPECTRO_DATA_DIR`, nicht in den Programmordner

## 9. Abschluss

- [ ] Alle Auffälligkeiten mit Punktnummer aus `docs/BEFUNDE_hardwaretest.md` notiert: ______
- [ ] Offene Punkte aus den Befunden: Dunkelniveau HDX vor/nach Umstellung optical_black, Lux bei Absolutprofil (HDX), Test PASCO -- Ergebnisse: ______
- [ ] `git status` im Programmordner: keine neuen Laufzeitdateien (nur Datenverzeichnis betroffen)
- [ ] Datenverzeichnis nach dem Test gesichert (`%APPDATA%\PS-2600A\`)
