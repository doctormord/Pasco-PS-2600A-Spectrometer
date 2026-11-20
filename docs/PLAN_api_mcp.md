# Plan: Gemeinsamer Kern, Remote-Server in der App, REST-API, MCP-Server, Claude-Paket

Arbeitsplan für die Umsetzung in Claude Code im Repo `doctormord/Pasco-PS-2600A-Spectrometer`.
Basis: `future` (Standard-Branch) nach Merge von PR #1 (spektrale Kalibrierung) und PR #2 (Korrektur Kalibrierdialog, Commit 92f82b7).
Stand: 2026-11-20, Revision 3

---

## 1. Ziel

1. **Ein gemeinsamer, Qt-freier Kern** für Gerät, Erfassung, Verarbeitung, Kalibrierzustand und Messaufträge. Desktop-App, Web-Oberfläche, REST-API und MCP nutzen ausschließlich diesen Kern. Keine doppelte Logik.
2. **Remote-Server aus der App startbar** (Schalter „Remote-Zugriff“); zusätzlich Betrieb ohne Bildschirm über denselben Einstiegspunkt (`--headless`).
3. **REST-API für automatisierte Messungen** (Einzelmessung, Messreihe, Auswertung, Kalibrierprofile, Kalibrierlauf), versioniert, mit OpenAPI-Beschreibung und Token-Schutz.
4. **MCP-Server** als dünne Schicht über der REST-API.
5. **Claude-Paket** (Claude-Code-Plugin) mit MCP-Konfiguration, Agents und Skills für Messabläufe, Kalibrierung und Prüfberichte.
6. **Abgleich Desktop ↔ Web**: Funktionsmatrix und Schließen der relevanten Lücken.

## 2. Verbindliche Regeln

- `engine.py`, `api.py`, `mcp_server/` und alle davon importierten Module importieren **kein PyQt** (auch nicht indirekt). Ein Test erzwingt das.
- Logik existiert genau einmal. Desktop und Web unterscheiden sich nur in der Darstellung.
- Bestehende Endpunkte unter `/api/...` und `/ws/stream` bleiben mit Pfad und Antwortformat erhalten (Web-Oberfläche, bestehende Skripte). Neue Funktionen kommen unter `/api/v1/...`.
- Bestehende Tests (`tests/`) bleiben grün; Verhalten ohne aktivierten Remote-Zugriff ist unverändert.
- Ein Gerät wird genau von einer Engine-Instanz geöffnet.
- Standardmäßig lauscht der Server nur auf `127.0.0.1`. LAN-Betrieb nur explizit und nur mit Token.
- **Bezeichner englisch**: Endpunkte, MCP-Werkzeuge, Agents, Skills, Dateinamen und Bedientexte auf Englisch (Kleinbuchstaben mit Bindestrich bei Agents/Skills). Berichtsinhalte in konfigurierbarer Sprache (de/en), Standard de.
- **Oberflächenqualität (Messgerätestandard)** für jede neue oder geänderte Qt-Oberfläche: Fenster passen auf den verfügbaren Bildschirmbereich (`availableGeometry`, Taskleiste berücksichtigt) und werden darin zentriert; keine Größenänderung des Fensters durch wechselnde Inhalte; alle Widgets über das zentrale Theme (`gui_theme.py`) gestaltet, keine ungestylten (weißen) Flächen; Tabellen ohne leere Zeilennummernspalte, sinnvolle Spaltenbreiten, Elision langer Texte mit Tooltip. Dafür die mit PR #2 eingeführten Helfer `gui_layout.py` (bildschirmgebundene Fenster) und `gui_tables.py` (Tabellenkonfiguration) sowie das zentrale Theme verwenden, nichts davon nachbauen.

## 3. Ist-Zustand (Befund)

| Bereich | Desktop (`gui_dashboard.py`) | Web (`web_server.py`) | Befund |
|---|---|---|---|
| Geräteverbindung, Parameterübernahme | `connect_device`, `disconnect_device` | `_connect`, `_disconnect`, `_apply_device_params` | doppelt |
| Fusion, Frame-Mittelung | eigene Instanz | `ServerState.fusion`, `spectrum_history` | doppelt |
| Verarbeitung | `process_new_spectrum` → `processing.pipeline_kwargs` | `_fused_emit` → `processing.pipeline_kwargs` | Kernfunktion bereits gemeinsam, Aufrufrahmen doppelt |
| Kalibrierzustand | `CalibrationRuntime` | `ServerState.calib` (`CalibrationRuntime`) | Klasse gemeinsam, Instanz/Verwaltung doppelt |
| Heatmap-Puffer, Farbmetrik-Drosselung | eigene Umsetzung | `ServerState` | doppelt |
| Auto-Belichtung, Verbindungsverlust | Callbacks | Callbacks | doppelt |
| Kalibriererfassung | `calibration_capture` / `calibration_workflow` (Qt-frei) | nur Profilverwaltung, keine Erfassung | Grundlage für API vorhanden |
| Frame-Taps | `BaseSpectrometer.add_frame_tap` | – | Grundlage für Messaufträge vorhanden |
| Start | `PS-2600A_Pro.py` (Qt) | `web_server.py` (`0.0.0.0:8000`, ohne Authentifizierung) | zwei getrennte Programme |
| Tabs | Scope, Heatmap, Color (Overview, CIE 1931, Rendering, TM-30), Filter, Kalibrierassistent | Scope, Heatmap, Color (Overview·CRI, TM-30), Replay, Filter | Abweichungen, Details in Phase 1 |

## 4. Zielarchitektur

```
                 ┌───────────────────────────────┐
                 │ engine.py  (Qt-frei)          │
                 │  Engine: Gerät, Fusion,       │
                 │  Mittelung, Verarbeitung,     │
                 │  CalibrationRuntime, Heatmap, │
                 │  Farbmetrik, Jobs, Steuerrecht│
                 └──────┬───────────────┬────────┘
            Listener    │               │   Listener
   ┌────────────────────┴──┐       ┌────┴─────────────────────┐
   │ GUI (Qt)              │       │ api.py (FastAPI, Qt-frei)│
   │ Qt-Signal-Brücke      │       │ create_app(engine)       │
   │ startet/stoppt Server │──────▶│ REST /api, /api/v1, WS   │
   └───────────────────────┘       └────┬─────────────────────┘
                                        │ HTTP + Token
                                   ┌────┴─────────────────────┐
                                   │ mcp_server/ (stdio)      │
                                   └────┬─────────────────────┘
                                        │
                                   Claude (Plugin: Agents, Skills)
```

- **Engine** besitzt die Threads (Treiber-Erfassung, Fusion-Emit). Ausgaben über registrierte Listener (`engine.subscribe(callback)`); GUI hängt eine Qt-Signal-Brücke an, der Server `asyncio.run_coroutine_threadsafe`.
- **Befehle** an die Engine sind thread-sicher (Lock bzw. Befehlswarteschlange). Lesezugriffe liefern Kopien/Snapshots.
- **Steuerrecht**: Die Engine kennt einen aktuellen Steuernden (`local` | `remote:<client>` | frei). Laufende Remote-Jobs halten das Steuerrecht; konkurrierende Steuerbefehle erhalten HTTP 409 bzw. sind in der GUI gesperrt. Die GUI zeigt den Zustand an und kann einen Remote-Job abbrechen.
- **Jobs**: Messaufträge (Einzelmessung, Messreihe, Kalibrierlauf) laufen als Jobs mit ID, Status (`queued`, `running`, `waiting_for_user`, `done`, `failed`, `cancelled`), Fortschritt, Ergebnis. Erfassung über Frame-Taps (`calibration_capture`), nicht über die Anzeigepipeline.
- **Einstiegspunkt**: `PS-2600A_Pro.py` → GUI; `PS-2600A_Pro.py --headless [--host --port --token]` → Engine + API ohne Qt-Import. `web_server.py` bleibt als Weiterleitung auf den Headless-Start.

## 5. API (Zielumfang `/api/v1`)

| Bereich | Endpunkte (Auszug) |
|---|---|
| System | `GET /status` (Gerät, Verbindung, t_int, Mittelung, aktives Profil, Einheit, Steuerrecht, laufende Jobs), `GET /backends`, `GET /devices` |
| Gerät | `POST /connect`, `POST /disconnect`, `PUT /acquisition` (t_int, Mittelung, Dunkelmodus, Verarbeitungsoptionen), `POST /auto-exposure` |
| Messen | `POST /measure` (t_int oder auto, Frames, Dunkel: gespeichert / neu erfassen / keins, Profil; Antwort: λ, Werte, Einheit, gültiger Bereich, Metadaten inkl. Zeitstempel mit Zeitzone, Gerät/Seriennummer, Profil; optional Farbmetrik und Peaks), `POST /series` (Anzahl, Intervall → Job), `POST /dark` |
| Daten | `GET /spectrum/latest` (Downsampling, λ-Fenster), `GET /measurements/{id}`, Export CSV/JSON |
| Auswertung | `POST /analyze/color`, `/analyze/peaks`, `/analyze/filter`, `/analyze/tm30` (für übergebenes Spektrum oder Mess-ID) |
| Kalibrierung | Profile listen/wählen/kopieren/exportieren/importieren, Referenzquellen, Dunkelspektren, Kalibrierlauf als Job inkl. Benutzerschritten (`waiting_for_user` + `POST /jobs/{id}/continue`), Protokoll abrufen |
| Jobs | `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` |
| Berichte | Farb-/Filter-/Kalibrierbericht als PDF/CSV |

- Einheitliches Fehlerformat `{"error": {"code", "message", "details"}}`; 400/401/404/409/422/503 konsistent.
- Authentifizierung: `Authorization: Bearer <token>` für alle steuernden Endpunkte; bei Bindung an `127.0.0.1` konfigurierbar, bei LAN zwingend. Token in der Konfiguration, in der GUI anzeig- und erneuerbar.
- OpenAPI unter `/docs`; Pydantic-Modelle für alle `/api/v1`-Ein- und Ausgaben.

## 6. MCP-Server und Claude-Paket

**MCP-Server** (`mcp_server/`, offizielles Python-SDK `mcp`, Transport stdio; Konfiguration über Umgebungsvariablen `SPECTRO_API_URL`, `SPECTRO_API_TOKEN`). Kommuniziert ausschließlich über die REST-API und funktioniert damit lokal, mit App oder headless und über das Netz.

| Werkzeug | Zweck |
|---|---|
| `status`, `list_devices`, `connect`, `disconnect` | Geräteverwaltung |
| `set_acquisition`, `auto_expose`, `capture_dark` | Erfassungsparameter |
| `measure`, `start_series`, `job_status`, `cancel_job` | Messen |
| `get_spectrum` | Spektrum (Standard: zusammengefasst und stark ausgedünnt; volle Daten als Datei/Pfad) |
| `analyze_color`, `find_peaks`, `analyze_filter` | Auswertung |
| `list_profiles`, `select_profile`, `calibration_status`, `start_calibration`, `continue_job` | Kalibrierung |
| `export_measurement`, `create_report` | Export, Berichte |

- Werkzeugantworten kompakt halten (Kennwerte, Metadaten, Dateipfad bzw. URL); keine vollständigen Pixelarrays ohne ausdrückliche Anforderung.
- Löschende Operationen (Profile, Dunkelspektren) werden nicht als Werkzeug angeboten.
- Ressourcen: Gerätestatus, aktive Konfiguration, Profilliste.

**Claude-Paket** (`claude-plugin/`, Format nach aktueller Claude-Code-Plugin-Dokumentation prüfen):
- MCP-Konfiguration für den Server.
- Agents:
  - `measurement` – plant und führt Messabläufe über die MCP-Werkzeuge aus, prüft Sättigung, Signalpegel und gültigen Bereich.
  - `calibration` – führt einen Kalibrierlauf nach dem festen Ablauf durch und fordert Benutzerschritte (Abdecken, Umstecken) an.
  - `test-report` – erstellt aus Messungen einen Prüfbericht: sachlich, technische Darstellung, Datum und Uhrzeit, Gerät, Seriennummer, Optik, Kalibrierprofil, Messbedingungen, Ergebnisse.
- Skills (englische Namen, z. B. `light-source-characterization`, `filter-transmission`, `stability-measurement`, `test-report-template`): Lichtquelle charakterisieren (Spektrum, CCT, CRI, Lux), Filtertransmission, Stabilitäts-/Langzeitmessung, Berichtsvorlage.
- Beispielkonfiguration für die Nutzung des MCP-Servers aus der Claude-App (Desktop).
- `examples/api_client.py`: Automatisierung per REST ohne Claude.

## 7. Umsetzung (4 Phasen, ein Durchgang)

**Ausführung in einem Durchgang:** Phasen 1–4 ohne Zwischenstopps und ohne Rückfragen. Offene Entscheidungen werden nach bestem Ermessen getroffen und im Abschlussbericht aufgeführt. Abnahmekriterien werden selbst geprüft (pytest, Demo-Backend), Fehler im selben Durchgang behoben. Hardwaretest erfolgt durch den Nutzer anhand `docs/TEST_api_mcp.md`.

Arbeitsweise: Branch `feature/api-mcp` von aktuellem `future`, ein Commit je Phase, kein Push ohne Rückfrage.

### Phase 1 – Bestandsaufnahme und gemeinsamer Kern
- `docs/PARITAET_Desktop_Web.md`: vollständige Funktionsmatrix Desktop ↔ Web (Tabs, Bedienelemente, Einstellungen, Exporte, Kalibrierung) mit Einstufung je Lücke: *Messung/Einstellung*, *Kalibrierung*, *nur Darstellung*.
- `engine.py`: Engine-Klasse gemäß Abschnitt 4 (Geräte-Lebenszyklus, Fusion, Mittelung, Verarbeitung, CalibrationRuntime, Heatmap-Puffer, Farbmetrik-Drosselung, Listener, thread-sichere Befehle, Steuerrecht, Job-System mit Einzelmessung und Messreihe über Frame-Taps).
- Migration: `web_server.py`-Zustand (`ServerState`, `_connect`, `_fused_emit` …) und die entsprechenden Teile von `gui_dashboard.py` auf die Engine umstellen. Reine Anzeigefunktionen (Freeze, Diff, Peak-Hold, Persistenz, Overlays) bleiben in der jeweiligen Oberfläche.
- Tests: Engine mit Demo-Backend (Verbinden, Frames, Einzelmessung, Messreihe, Abbruch, Steuerrecht), Qt-Freiheitstest (Import von `engine`, `api`, `mcp_server` bei blockiertem `PyQt6`).

**Abnahme:** alle bisherigen Tests grün; Desktop und Web liefern mit Demo-Backend identische verarbeitete Spektren aus derselben Engine-Logik; keine doppelte Geräte-/Pipeline-Logik mehr in `gui_dashboard.py` und `web_server.py`.

### Phase 2 – API und Remote-Server in der App
- `api.py` mit `create_app(engine, settings)`: bestehende Endpunkte unverändert übernommen, `/api/v1` nach Abschnitt 5, Pydantic-Modelle, Fehlerformat, Token-Prüfung, OpenAPI.
- `server_runner.py`: uvicorn im eigenen Thread mit sauberem Start/Stopp.
- GUI: Bereich „Remote-Zugriff“ (Ein/Aus, Port, nur lokal / LAN, Token anzeigen/erneuern, URL), Statusanzeige Steuerrecht und laufender Remote-Job mit Abbruch; Sperre konkurrierender Bedienelemente während Remote-Jobs. Einstellungen in der Konfiguration.
- Einstieg: `PS-2600A_Pro.py --headless` ohne Qt-Import; `web_server.py` als Weiterleitung. PyInstaller-Spec (`PS-2600A_Pro.spec`) um benötigte Hidden Imports ergänzen.
- Tests: API mit `TestClient` (alle `/api/v1`-Endpunkte, Token, 409 bei belegtem Steuerrecht, Kalibrierlauf-Job mit Benutzerschritten im Demo-Backend), Start/Stopp des Server-Threads.

**Abnahme:** App mit Demo-Backend starten, Remote-Zugriff einschalten, per REST messen, Ergebnis identisch zur GUI-Anzeige; Headless-Start ohne installiertes PyQt lauffähig.

### Phase 3 – MCP-Server und Claude-Paket
- `mcp_server/` nach Abschnitt 6, inkl. README mit Einrichtung (Claude Code und Claude-App).
- `claude-plugin/` mit MCP-Konfiguration, Agents, Skills.
- `examples/api_client.py`.
- Tests: MCP-Werkzeuge gegen die API (In-Prozess-Server mit Demo-Backend); Antwortgrößen begrenzt.

**Abnahme:** In Claude Code mit installiertem Plugin und laufender App (Demo-Backend): verbinden, messen, Farbmetrik auswerten, Prüfbericht erzeugen.

### Phase 4 – Web-Parität und Dokumentation
- Lücken der Kategorie *Messung/Einstellung* aus `docs/PARITAET_Desktop_Web.md` im Web schließen.
- Kalibrierung im Web: Kalibrierlauf über die Job-API (Ablauf identisch zum Desktop-Assistenten).
- Lücken der Kategorie *nur Darstellung*: schließen, wo mit geringem Aufwand möglich; übrige in der Matrix als bewusst offen markieren.
- Doku: `README_WEB.md`, `docs/manual/manual.tex` (Kapitel Architektur, Remote-Zugriff, API, MCP), `docs/TEST_api_mcp.md` (Checkliste Hardwaretest).
- Abschluss: Review durch `review-opus` über den gesamten Diff, Befunde der Schwere kritisch/wichtig beheben, Abschlussbericht (umgesetzte Punkte, Entscheidungen, Abweichungen, Testergebnisse, bekannte Einschränkungen).

**Abnahme:** Matrix zeigt keine offenen Lücken der Kategorien *Messung/Einstellung* und *Kalibrierung*; alle Tests grün.

## 8. Bekannte Grenzen und Risiken

- Die Migration von `gui_dashboard.py` (≈3 250 Zeilen) ist der größte Eingriff; Regressionen in der Desktop-Oberfläche sind nur teilweise automatisch testbar → Hardwaretest der Bedienung ist Teil von `docs/TEST_api_mcp.md`.
- Desktop-App und eigenständiger Headless-Server können nicht gleichzeitig dasselbe Gerät öffnen; vorgesehen ist ein Prozess pro Gerät.
- Bei LAN-Betrieb ist die Verbindung unverschlüsselt (HTTP); für Netze außerhalb des Labors ist ein vorgeschalteter Reverse-Proxy mit TLS vorzusehen.
