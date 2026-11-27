# Hardwaretest Engine, Remote-Zugriff, REST-API und MCP (Phase 4)

Checkliste zum Abhaken für den Hardwaretest nach der Umstellung auf den gemeinsamen Kern
(Plan: `docs/PLAN_api_mcp.md`; Parität: `docs/PARITAET_Desktop_Web.md`; Handbuch: Kapitel "System architecture", "Remote access", "Web server & API", "MCP server & Claude plugin"; Web/API: `README_WEB.md`).
Der Test ist für jedes Gerät gleich. Das Demo-Backend wird immer geprüft; PASCO PS-2600A, LR-2T und HDX nur, soweit vorhanden.

Eintragen bei jedem Schritt: Datum, Gerät/Seriennummer, Auffälligkeiten (Spalte "Notiz" bzw. Zeile darunter).
Aufbau der Abschnitte: 0 Vorbereitung, A Desktop-Bedienung, B Remote-Zugriff in der App, C Web-Oberfläche, D REST-API, E Headless, F MCP und Claude, G Abschluss.

## 0. Vorbereitung

- [ ] Branch `feature/api-mcp` ausgecheckt, Stand notiert (`git rev-parse --short HEAD`): ______
- [ ] `python -m pytest -q` läuft grün (Ergebnis notieren): ______
- [ ] Backup angelegt (Kopie in separaten Ordner, mit Datum):
  - [ ] `spectrometer_config.json`
  - [ ] `calibration_profiles.json`
  - [ ] `dark_spectra.json`
  - [ ] `reference_sources.json`
  - [ ] ggf. weitere JSON-Dateien im Programmordner (Konfiguration der Geräte, Linienlisten)
- [ ] Abhängigkeiten installiert (Ergebnis von `pip list` für diese Pakete notiert): ______
  - [ ] `pip install -r requirements.txt` (enthält fastapi, uvicorn, websockets, pydantic, httpx)
  - [ ] Für Abschnitt F zusätzlich: `pip install "mcp>=2.3"` (Python 3.10 oder neuer)
- [ ] Zweiter Rechner oder Smartphone/Tablet im selben Netz für den LAN-Test bereit (Abschnitte B, C, D)
- [ ] Firewall (Windows Defender): beim ersten LAN-Start fragt Windows nach Netzwerkzugriff für Python; für das private Netz zulassen, nicht für öffentliche Netze. Alternativ Regel für TCP-Port 8000 (eingehend, nur privates Profil)
- [ ] Hardware: Gerät(e) per Datenkabel angeschlossen; kein anderes Programm hält das Gerät (nur ein Prozess pro Gerät)
- [ ] Ordner für Exporte, Screenshots und Protokolle angelegt: ______

## A. Desktop-Bedienung nach der Engine-Migration

Die Desktop-Oberfläche läuft jetzt über die gemeinsame Engine. Der Test prüft, dass sich die Bedienung nicht verändert hat.
Je Gerät durchführen (Spalten: Demo, PASCO, LR-2T, HDX; "n/a" wenn nicht vorhanden). Remote-Zugriff ist dabei **aus**.

| Gerät | Demo | PASCO | LR-2T | HDX |
|---|---|---|---|---|
| Seriennummer / Hinweis | | | | |

### A1 Verbinden und Trennen

- [ ] Backend wählen, "Scan USB": Geräteliste korrekt (Demo, PASCO, LR-2T, HDX): ______
- [ ] Verbinden: Live-Spektrum erscheint, Verbindungspunkt grün, Frequenzanzeige läuft
- [ ] Trennen und erneut verbinden: kein Hängen, kein doppeltes Öffnen
- [ ] Backend wechseln (verbunden): Trennen wird verlangt bzw. sauber durchgeführt
- [ ] Steuerelemente ohne Gerät deaktiviert; nach Verbinden aktiv
- [ ] Gerätespezifika: LR-2T Wellenlängen-Offset und Pixelspiegelung (`lr2t_wl_offset_nm`, `lr2t_flip_pixels`) wirken wie vor der Umstellung (Linienlage mit bekannter Lampe geprüft): ______

### A2 Erfassung

- [ ] Belichtung ändern (Feld und Pfeile): Spektrum folgt; Wert wird auf die Gerätegrenzen begrenzt
- [ ] Auto-Belichtung ein: Belichtung stellt sich auf Zielbereich ein; manuelle Änderung schaltet sie aus (wie vor der Umstellung)
- [ ] Mittelung (`frames_to_average`) 1, 4, 16: Rauschen sinkt, Aktualisierungsrate sinkt
- [ ] Pause/Start: Spektrum friert ein bzw. läuft weiter; Pause wirkt auch im Heatmap-Puffer
- [ ] Fast Preview ein/aus, Short-Frame-Anteil 10 %: Anzeige flüssig; Ausschalten stellt die Hauptbelichtung sofort wieder her
- [ ] Glättung (Fusion, "Smoothing") ein/aus und Stärke: Anzeige beruhigt sich, Peaks bleiben scharf
- [ ] Räumliche Glättung und Hot-Pixel-Filter ein/aus: Wirkung sichtbar

### A3 Dunkelkorrektur

- [ ] Dunkelkorrektur ein/aus
- [ ] Modus `optical_black` (Geräte mit Optical Black), `parametric`, `stored`: jeweils Anzeige plausibel (Dunkelpegel nahe 0 bei abgedecktem Eingang)
- [ ] "Capture dark" bei abgedecktem Eingang: Dunkelspektrum gespeichert (Statusmeldung), Modus `stored` nutzt es
- [ ] Gerät ohne Shutter: Aufforderung zum Abdecken/Aufdecken erscheint genau einmal
- [ ] Abweichung zum Stand vor der Umstellung (Referenz: Screenshot/CSV vom Hauptbranch, gleiche Einstellung): ______

### A4 Anzeigeeinheit und Kalibrierprofil

- [ ] Ohne Profil: "Display unit" `counts` / `counts/s` umschalten; Achse, Auslesung und CSV-Kopf passen
- [ ] Mit Absolutprofil: "Irradiance unit" `uW/cm2/nm`, `W/m2/nm`, `mW/m2/nm`: Wert ändert sich um den richtigen Faktor; Achsenbeschriftung stimmt; Farbmetrik (Lux) bleibt unverändert
- [ ] Kalibrierprofil im Dropdown wählen: Statuszeile zeigt Profil, gültigen Bereich, Warnungen
- [ ] Profil mit Markierung (z. B. Wellenlänge geändert): **Bestätigungsdialog** erscheint; "Abbrechen" wendet nicht an; "Bestätigen" wendet an
- [ ] Auswahl "None" / "Device default": Anzeige wie vor der Umstellung

### A5 Kalibrierassistent (unverändert)

- [ ] Wellenlängenkalibrierung öffnen, Linien erfassen, anpassen (ohne zu speichern): Ablauf wie zuvor, keine Fehlermeldung
- [ ] Spektrale Kalibrierung: Schritte 1 bis 8 durchlaufbar (mit Demo komplett, mit Hardware bis zur Erfassung); Dunkel, Plausibilitätsprüfung, Abbruch funktionieren
- [ ] Ableitung ("Derive from other optics") startbar
- [ ] Fenster passt auf den Bildschirm (Taskleiste berücksichtigt), ändert beim Wechsel der Schritte seine Größe nicht; Tabellen ohne leere Zeilennummernspalte

### A6 Heatmap

- [ ] Puffergröße ändern (Schritt 10): Puffer wird geleert, Zeitachse passt
- [ ] Messintervall (`heatmap_measure_interval_ms`) 0, 500, 2000 ms: Spaltenabstand entspricht; Span-Anzeige plausibel
- [ ] Scrub/Rewind und Live/Pin: Spektrum des gewählten Frames wird gezeigt, "T-x s ago" stimmt
- [ ] Autosave ein: Datei wird im gewählten Ordner angelegt und wächst; Aus: Datei geschlossen
- [ ] Export (sichtbar, gesamt, ein Frame): CSV öffnet sich korrekt (Zeitstempel, Wellenlängen)
- [ ] Clear leert den Puffer

### A7 Auswertung und Anzeige

- [ ] Peaks ein: Marker mit Wellenlänge auf den bekannten Linien; Schwellen aus der Konfiguration
- [ ] Messcursor A/B: Wellenlängen und Werte, Differenz
- [ ] Overlays: Freeze, Differenz, Peak-Hold, Persistenz, Referenzspektrum
- [ ] Color-Tabs: Overview (CCT, Duv, xy, CRI, Lux), CIE 1931, Rendering, TM-30/CQS (mit `colour-science`); Werte stabil und plausibel; Aktualisierung ohne Ruckeln
- [ ] Filter-Tab: Basislinie setzen, Transmission, Kennwerte
- [ ] Exporte: Spektrum-CSV, PNG, Farbbericht (PDF/PNG/CSV), Filterbericht; Dateinamen nach Vorlage (`export_filename_template`), Zielordner (`export_dir`; im Web nur bei aktivem Token änderbar, sonst Meldung „Not settable remotely“)
- [ ] Sicherheit: `GET /api/config` und WS-hello enthalten kein `remote_token`; `POST /api/config {"remote_lan": true}` ergibt 403; Aufruf aus einer fremden Webseite (anderer Origin) wird abgewiesen
- [ ] Theme-Wechsel (dunkel/hell): alle Bereiche inklusive Remote-Panel und Dialoge umgestellt, keine weißen Flächen
- [ ] Fenstergeometrie: Größe und Position werden gemerkt; keine Größenänderung beim Wechsel von Tabs, Meldungen oder Statuszeile

### A8 Verbindungsverlust

- [ ] USB-Kabel im laufenden Betrieb ziehen (nicht beim Demo): Meldung "Connection Lost", Oberfläche geht in den Zustand "getrennt", kein Absturz, keine hängenden Threads
- [ ] Kabel wieder stecken, erneut verbinden: Betrieb normal
- [ ] Beenden der App danach: Prozess endet vollständig (Task-Manager)

## B. Remote-Zugriff in der App

- [ ] Abschnitt "Remote access" in der Seitenleiste sichtbar, im Stil der übrigen Abschnitte, ohne Größenspringen beim Umschalten
- [ ] Einschalten (lokal, Port 8000): Schalter an, URL `http://127.0.0.1:8000` angezeigt; `http://127.0.0.1:8000/docs` im Browser erreichbar
- [ ] Token: "Show" zeigt, "Hide" maskiert, "Copy" kopiert (in Texteditor einfügen und prüfen)
- [ ] "Copy URL" kopiert die URL
- [ ] Lokal ohne Token: `curl http://127.0.0.1:8000/api/v1/status` liefert Status; steuernder Aufruf ohne Token funktioniert (Standard lokal)
- [ ] "Require token locally" ein: steuernder Aufruf ohne Token liefert 401, mit Token 200
- [ ] Zugriff "LAN": Hinweis auf unverschlüsseltes HTTP erscheint; Server startet neu bzw. nach Aus/Ein auf allen Schnittstellen
- [ ] Von zweitem Gerät im LAN: `http://<IP>:8000/` lädt die Oberfläche; steuernde Aufrufe ohne Token: 401; mit `?token=` bzw. Bearer: ok
- [ ] "Renew": Rückfrage; danach alter Token ungültig (401), neuer gültig; laufender Server wurde neu gestartet
- [ ] Port belegt (z. B. zweiter Prozess oder anderes Programm auf 8000): Einschalten schlägt mit verständlicher Meldung fehl, Schalter springt zurück auf aus, App läuft weiter: ______
- [ ] Anderen Port einstellen (z. B. 8123): Server startet dort
- [ ] Steuerrecht: Remote-Messung per API starten (siehe D): Desktop-Steuerelemente und "Connect" gesperrt, Statusleiste zeigt `Remote: remote:<client> ...` und "Cancel remote"; im Panel Zeile "Control" und Fortschritt
- [ ] "Cancel remote" (Statusleiste) bricht Job ab und entsperrt die Oberfläche sofort
- [ ] Änderungsversuch am Desktop während Remote-Job: Meldung "Remote control active", Einstellung unverändert
- [ ] "Revoke control" entzieht eine explizite Leihe (`POST /api/v1/control`)
- [ ] App beenden und neu starten mit `remote_enabled = true`: Server startet automatisch (gleicher Port, gleicher Token)
- [ ] Schalter aus: Server stoppt; Port frei; `remote_enabled` false nach Neustart
- [ ] Beenden der App bei laufendem Server: Prozess endet ohne Hängen

## C. Web-Oberfläche

Browser: Chrome/Edge und ein Mobilgerät. Server aus der App (B) oder Headless (E).

### C1 Zugang

- [ ] Lokal `http://127.0.0.1:8000/` lädt; Verbindungspunkt zeigt Verbindung
- [ ] LAN `http://<IP>:8000/` ohne Token: Seite zeigt Live-Daten, ist aber schreibgeschützt (Hinweis "Read-only: an API token is required"); steuernde Elemente ohne Wirkung
- [ ] `http://<IP>:8000/?token=<token>`: Eingabe wird übernommen, Token verschwindet aus der Adresszeile, Seite voll bedienbar; nach Neuladen weiterhin bedienbar
- [ ] Falschen Token eingeben: Meldung "Not authorized", kein Steuern möglich
- [ ] Token erneuert (B): Web meldet fehlende Berechtigung, neuer Token wird abgefragt

### C2 Live-Stream und Steuerrecht

- [ ] Live-Spektrum läuft flüssig (Demo und Gerät); Frequenz plausibel
- [ ] Mehrere Browserfenster gleichzeitig: alle laufen, Einstellungen wirken auf alle
- [ ] Anzeige des Steuerrechts: bei Remote-Job (D) zeigt die Seite den Inhaber und ist gesperrt/schreibgeschützt; nach Ende frei
- [ ] Verbindungsverlust (USB ziehen): Seite zeigt "getrennt", kein Absturz; nach Wiederverbinden Betrieb normal
- [ ] Versionsanzeige in der Statusleiste (P-20): ______

### C3 Paritätsfunktionen (`docs/PARITAET_Desktop_Web.md`, Abschnitt 13)

Je Zeile prüfen, dass die Funktion im Web wie im Desktop arbeitet und dieselben Zahlen liefert.

- [ ] P-01 Heatmap-Puffergröße einstellbar (leert den Puffer)
- [ ] P-02 Heatmap-Messintervall und Span-Anzeige
- [ ] P-03 Heatmap-Autosave (Datei liegt auf dem Server; Pfad wird angezeigt)
- [ ] P-04 Heatmap leeren
- [ ] P-05 Heatmap Scrub/Rewind, Live/Pin, "T-x s ago", Spektrum des gewählten Frames
- [ ] P-06 Heatmap-Export (sichtbar, gesamt, ein Frame)
- [ ] P-07 Exportordner und Dateinamen-Vorlage, Vorschau
- [ ] P-08 Belichtungsgrenzen des Geräts im Eingabefeld (je Gerät andere Grenzen)
- [ ] P-09 "Capture dark" (Job; Aufforderung zum Abdecken im Browser)
- [ ] P-10 Kalibrierlauf (siehe C4)
- [ ] P-11 Wellenlängenkalibrierung (siehe C5)
- [ ] P-12 Referenzquellen: Liste, Import einer Lampendatei (`docs/1808001-NIR-CCVISNIR.lmp`), Entfernen eines importierten Eintrags (Built-ins nicht entfernbar)
- [ ] P-13 Dunkelspektren-Liste (Integrationszeit, Frames, Datum)
- [ ] P-14 Kalibrierprotokoll (PDF, CSV) nach einem Lauf abrufbar
- [ ] P-15 Legacy-Antwortkorrektur: bewusst nicht im Web (Matrix prüfen)
- [ ] P-16 Profiltabelle und vollständiger Status (Segmente, Plausibilitätswarnung)
- [ ] P-17 Illuminance und Foot-candle im Color-Tab, "—" ohne Kalibrierung; Wert gleich wie im Desktop bei gleichem Profil bzw. `radiometric_calibration_factor`
- [ ] P-18 Color-Tab: Central λ, u/v, R9, 5-SDCM-Ellipse
- [ ] P-19 Graue Lücken zwischen gültigen Segmenten im Scope
- [ ] P-20 Versionsanzeige
- [ ] P-21 PNG-Export ganze Ansicht inklusive Achsen, auch Color- und Filter-Tab
- [ ] P-22 Anzeigeeinheit und Bestrahlungsstärkeeinheit
- [ ] P-23 Replay-Tab (Heatmap-CSV abspielen)
- [ ] P-24 Berichtsformate Color/Filter (PDF, PNG, CSV) einheitlich
- [ ] P-25 Schalter "Response compensation" folgt der Profilwahl (kein eigener Schalter ohne Wirkung)
- [ ] Offene Punkte der Matrix (nicht umgesetzte Zeilen) notiert: ______

### C4 Kalibrierlauf im Browser

- [ ] "Calibration run…": Formular (Optics, Notes, Eingangsoptik, Typ, Referenzquelle, Integration, Frames) vollständig
- [ ] Lampenkalibrierung mit Demo komplett durchlaufen; Schritte werden angezeigt; Benutzerschritt "Abdecken" erscheint im Browser, "Weiter" setzt fort; Aufdecken analog
- [ ] Mit Hardware (ein Gerät): Ablauf wie im Desktop-Assistenten (Referenz: `docs/TEST_kalibrierung.md`, A3); Stabilität ______ %, Kreuzprüfung ______ %, gültiger Bereich ______ nm
- [ ] Abbrechen während des Laufs: Status `cancelled`, kein Profil gespeichert, Steuerrecht wieder frei
- [ ] Plausibilitätswarnung: Speichern trotz Warnung nur nach ausdrücklicher Bestätigung
- [ ] Ergebnisprofil erscheint in "Calibration data" und ist im Desktop wählbar
- [ ] Protokoll (PDF und CSV) herunterladbar
- [ ] Ableitung (derivation) startet und fordert die Schritte (mount_known, cover, uncover, swap_optics) an
- [ ] Während des Laufs: Desktop gesperrt, Statusleiste zeigt den Fortschritt

### C5 Wellenlängenkalibrierung im Browser

- [ ] "Wavelength calibration…": Lampe wählen (z. B. Cd, Hg), Spektrum erfassen
- [ ] Zuordnung der Linien (Match), Tabelle editierbar, mehrere Lampen kombinierbar
- [ ] "Fit": Vorschau der neuen Achse in allen Clients (Browser und Desktop); Residuen plausibel (RMS ______ nm)
- [ ] Verwerfen (Session löschen) stellt die alte Achse wieder her
- [ ] Speichern nur mit Bestätigung "Back up & confirm"; Backup wurde angelegt: ______
- [ ] Danach Desktop und Web zeigen dieselbe Achse; Profile werden gegebenenfalls als "Wellenlänge geändert" markiert

### C6 Protokolle und Downloads

- [ ] Kalibrierprotokoll-Download (PDF, CSV)
- [ ] CSV-Download der Messung, Farb- und Filterbericht (benötigt `matplotlib`)

## D. REST-API

- [ ] OpenAPI unter `/docs` vollständig; Gruppen system, device, measurement, jobs, data, analysis, reports, calibration, wavelength, heatmap sichtbar
- [ ] `examples/api_client.py` (Token über `SPECTRO_API_TOKEN` oder `--token`):
  - [ ] `status`
  - [ ] `connect --backend "Demo (virtual)"` (bzw. echtes Gerät)
  - [ ] `exposure 50`
  - [ ] `measure --frames 5 --csv spectrum.csv`: CSV enthält Wellenlängen, Werte, Einheit
  - [ ] `series --count 10 --interval 2`: Fortschritt, Abschluss
  - [ ] `report --pdf color_report.pdf`: PDF öffnet sich
- [ ] curl-Beispiele aus `README_WEB.md` (status, connect, measure mit `"integration_ms": "auto"`) funktionieren
- [ ] `dark`-Modi von `POST /measure`: `auto`, `stored` (422 ohne gespeichertes Dunkel bei dieser Belichtung), `capture` (Job `waiting_for_user` bei Gerät ohne Shutter, Fortsetzen mit `POST /jobs/{id}/continue`), `none`
- [ ] Fehlerformat `{"error": {"code", "message", "details"}}` bei 401, 404, 409, 422, 503 (je einen Fall ausgelöst: ______)
- [ ] 401: steuernder Aufruf ohne Token bei LAN bzw. "Require token locally"
- [ ] 409: zweiter Client mit anderer `X-Client-Id` startet während eines Jobs einen steuernden Aufruf: `control_busy` mit Inhaber in `details`; GUI-Änderung während des Jobs gesperrt
- [ ] Job abbrechen (`POST /jobs/{id}/cancel`): Status `cancelled`, Steuerrecht frei
- [ ] Profilauswahl über `POST /calibration/select` mit markiertem Profil: 409 `needs_confirmation`, mit `confirm` ok
- [ ] **Messung identisch zur GUI-Anzeige**: GUI pausieren, Live-Bild einfrieren; `GET /api/v1/spectrum/latest` und das Spektrum im Desktop (CSV-Export) vergleichen: Werte und Einheit gleich (Abweichung ______)
- [ ] `POST /measure` mit `dark: auto` und gleicher Belichtung wie die GUI: Ergebnis stimmt mit der GUI-Anzeige im Rauschen überein (Mittelwert über den Bereich: GUI ______, API ______)
- [ ] `/analyze/color` liefert CCT/Duv/CRI wie der Color-Tab (Abweichung ______)
- [ ] Legacy-Endpunkte (`/api/status`, `/api/config`, `/ws/stream`) funktionieren weiter (Web-Oberfläche läuft)

## E. Headless

- [ ] `python PS-2600A_Pro.py --headless --backend "Demo (virtual)"`: Banner mit URL, `/docs`-URL und Token (bei Token-Pflicht); Web-Oberfläche läuft
- [ ] `--lan --port 8123`: Banner zeigt LAN-IP und Hinweis auf unverschlüsseltes HTTP; Zugriff vom zweiten Gerät nur mit Token
- [ ] `--no-connect`: kein Gerät verbunden, Verbinden über API/Browser möglich
- [ ] Mit echtem Gerät (`--backend` passend): Auto-Connect meldet das Gerät; Messung per API
- [ ] `python web_server.py`: startet weiterhin (lokal), leitet auf denselben Server
- [ ] Optional: virtuelles Environment **ohne** PyQt6/pyqtgraph (nur numpy, scipy, fastapi, uvicorn, websockets, pydantic): `--headless` startet und misst mit Demo
- [ ] Strg+C beendet den Server sauber, das Gerät wird freigegeben (danach Desktop-App öffnet das Gerät)
- [ ] Desktop-App und Headless-Server gleichzeitig am selben Gerät: zweiter Start meldet einen Fehler bzw. kann das Gerät nicht öffnen (erwartet: ein Prozess pro Gerät): ______

## F. MCP-Server und Claude

Voraussetzung: Server läuft (App mit Remote access oder headless, Demo-Backend genügt für den Ablauf), `mcp>=2.3` und `httpx` installiert.

### F1 Einrichtung

- [ ] Plugin per `claude --plugin-dir <repo>/claude-plugin`: Plugin geladen, MCP-Server `spectrometer` verbunden (`/mcp`)
- [ ] Umgebungsvariablen gesetzt: `SPECTRO_API_URL`, `SPECTRO_API_TOKEN` (bei LAN), `SPECTRO_CLIENT_ID`; ggf. `SPECTRO_PYTHON`
- [ ] Lokaler Marketplace: `claude plugin marketplace add <repo>` und `claude plugin install ps2600-spectrometer@ps2600-local`; mit `SPECTRO_MCP_DIR=<repo>` startet der Server
- [ ] Claude-App (Desktop): `claude_desktop_config.json` nach `claude-plugin/examples/claude_desktop_config.example.json` ergänzt (absolute Pfade), App neu gestartet; Werkzeuge sichtbar
- [ ] Falscher Token: Fehlermeldung `unauthorized` verständlich
- [ ] App nicht gestartet: Meldung "Cannot connect to the spectrometer API"
- [ ] Optional: `npx @modelcontextprotocol/inspector python -m mcp_server` zeigt alle Werkzeuge

### F2 Werkzeuge und Ressourcen

- [ ] `status`, `list_devices`, `connect`, `disconnect`
- [ ] `set_acquisition`, `auto_expose`, `capture_dark` (Benutzerschritt: Claude fragt nach, bevor es fortsetzt)
- [ ] `measure`: kompaktes Ergebnis, CSV im `SPECTRO_DATA_DIR`
- [ ] `start_series`, `job_status`, `cancel_job`, `continue_job`
- [ ] `get_spectrum`: Zusammenfassung, höchstens 512 Punkte; volles CSV nur auf Anforderung
- [ ] `analyze_color`, `find_peaks`, `analyze_filter`
- [ ] `list_profiles`, `select_profile`, `calibration_status`
- [ ] `list_references`, `start_calibration`
- [ ] `export_measurement`, `create_report` (PDF, PNG, CSV)
- [ ] Ressourcen `spectrometer://status`, `spectrometer://config`, `spectrometer://profiles` lesbar
- [ ] Keine löschenden Werkzeuge vorhanden (Profile, Dunkelspektren, Lampen)
- [ ] Antworten bleiben kompakt (höchstens 8 KB, "truncated" wird markiert)
- [ ] Während Claude einen Job hält: Desktop gesperrt, Statusleiste "Remote: remote:<SPECTRO_CLIENT_ID>"; "Cancel remote" bricht ab

### F3 Agents und Skills (Claude Code)

- [ ] Agent `measurement`: "Miss diese Lampe mit Auto-Belichtung, 10 Frames": prüft Sättigung, Signalpegel und gültigen Bereich, meldet Bedingungen
- [ ] Agent `calibration`: führt einen Kalibrierlauf nach festem Ablauf durch; fordert Abdecken/Aufdecken vom Benutzer an und wartet auf Bestätigung; Ergebnis mit Stabilität, Kreuzprüfung, gültigem Bereich
- [ ] Agent `test-report`: erstellt einen Prüfbericht (sachlich, technische Darstellung, Datum und Uhrzeit mit Zeitzone, Gerät, Seriennummer, Optik, Kalibrierprofil, Messbedingungen, Ergebnisse); Sprache de und en (`report_language`)
- [ ] Skill `light-source-characterization`: Spektrum, CCT, Duv, CRI (Ra, R9), Lux, Peaks; Zahlen entsprechen dem Color-Tab
- [ ] Skill `filter-transmission`: Referenz ohne Filter, dann mit Filter, gleiche Einstellungen; Transmission plausibel
- [ ] Skill `stability-measurement`: Serie mit Drift von CCT, Lux, Peakhöhe
- [ ] Skill `test-report-template`: Pflichtfelder vollständig
- [ ] Ablauf Abnahme Phase 3: verbinden, messen, Farbmetrik auswerten, Prüfbericht erzeugen (mit Demo und mit Gerät): ______
- [ ] Kalibrierlauf mit Benutzerschritten aus Claude: Claude setzt nie ohne Bestätigung des Benutzers fort

## G. Abschluss

- [ ] Alle Abschnitte durchlaufen; Ergebnisse unten eingetragen
- [ ] Auffälligkeiten (Regressionen im Desktop, Web, API, MCP) mit Schritt-Nummer notiert: ______
- [ ] Konfigurations- und Profil-Dateien mit den Backups verglichen; unerwartete Änderungen: ______
- [ ] Rückmeldung an die Entwicklung: Checkliste, Ergebnistabelle, Logs/Screenshots der Fehler

## Ergebnisse (ausfüllen)

| Test | Gerät / Seriennummer | Ergebnis / Bemerkung | OK |
|---|---|---|---|
| A Desktop Demo | | | [ ] |
| A Desktop PASCO | | | [ ] |
| A Desktop LR-2T | | | [ ] |
| A Desktop HDX | | | [ ] |
| A8 Verbindungsverlust | | | [ ] |
| B Remote access (lokal, LAN, Token, Port) | | | [ ] |
| B Steuerrecht / Sperre / Cancel | | | [ ] |
| C1 Zugang Web (lokal, LAN, Token) | | | [ ] |
| C3 Paritätsfunktionen | | offen: | [ ] |
| C4 Kalibrierlauf im Browser | | Stabilität / Kreuzprüfung: | [ ] |
| C5 Wellenlängenkalibrierung im Browser | | RMS: | [ ] |
| D REST-API (Client, curl, 401/409) | | | [ ] |
| D Messung identisch zur GUI | | Abweichung: | [ ] |
| E Headless (Quelle, Demo, Gerät) | | | [ ] |
| F1 Einrichtung (Plugin, Marketplace, Claude-App) | | | [ ] |
| F2 Werkzeuge | | | [ ] |
| F3 Agents und Skills | | | [ ] |

## Abzulegende Nachweise

- [ ] Ausgefüllte Checkliste und Ergebnistabelle
- [ ] Ausgabe von `python -m pytest -q` und Commit-Stand
- [ ] CSV-Vergleich GUI/API (D) und Farbmetrikvergleich
- [ ] Kalibrierprotokolle (PDF, CSV) aus dem Browserlauf (C4)
- [ ] Prüfbericht(e) aus Abschnitt F, Beispiel-CSV und Berichte aus `SPECTRO_DATA_DIR`
- [ ] Screenshots: Remote-Panel, Statusleiste bei Remote-Job, Web-Oberfläche mit Steuerrechtanzeige

## Bekannte Einschränkungen

- LAN-Betrieb ist unverschlüsseltes HTTP; außerhalb eines vertrauenswürdigen Labornetzes einen Reverse-Proxy mit TLS vorschalten. Den Port nie ins Internet stellen.
- Pro Gerät genau ein Prozess: Desktop-App und eigenständiger Headless-Server können dasselbe Gerät nicht gleichzeitig öffnen.
- Lesende Endpunkte (Status, Spektrum, Messungen, Auswertung, Berichte) verlangen keinen Token; nur steuernde Aufrufe sind geschützt.
- Löschende Operationen (Profile, Dunkelspektren) gibt es weder in der API noch als MCP-Werkzeug; Lampen aus der Referenzbibliothek lassen sich über die REST-API entfernen, nicht über MCP.
- Antworten der MCP-Werkzeuge sind auf 8 KB begrenzt; vollständige Spektren liegen als Datei in `SPECTRO_DATA_DIR`.
- Das Demo-Backend ersetzt keinen Hardwaretest (Dunkelverschluss simuliert).
- Die Legacy-Antwortkorrektur (P-15) ist im Web bewusst nicht angeboten.
