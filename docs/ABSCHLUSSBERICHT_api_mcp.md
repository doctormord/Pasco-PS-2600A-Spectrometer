# Abschlussbericht: Gemeinsamer Kern, Remote-Server, REST-API, MCP, Claude-Paket

Plan: `docs/PLAN_api_mcp.md` (Revision 3). Branch `feature/api-mcp` (von `future`, 92f82b7), Stand 2026-11-20.
Hardwaretest steht aus → `docs/TEST_api_mcp.md`.

## 1. Commits

| Commit | Inhalt |
|---|---|
| `0e3c5dd` | Phase 1 – Qt-freie Engine, Migration Desktop und Web, Funktionsmatrix |
| `f9c276a` | Phase 2 – `/api/v1`, Token, `server_runner`, Remote-Zugriff in der App, Headless, Spec |
| `b5ccbc0` | Phase 3 – MCP-Server, Claude-Code-Plugin, `examples/api_client.py` |
| `447bce9` | Phase 4 – Web-Parität, Wellenlängenkalibrierung als gemeinsamer Kern, Doku |
| (letzter) | Review-Korrekturen (Sicherheit, Steuerrecht, Stream, Spec) + dieser Bericht |

## 2. Umgesetzt

**Phase 1 – Kern**
- `engine.py`: eine Engine je Gerät: Geräte-Lebenszyklus, Fusion, Mittelung, Pipeline (`processing.process_frame` + `pipeline_kwargs`), `CalibrationRuntime`, Heatmap-Puffer (Größe, Messintervall, Zeitstempel, Autosave), gedrosselte Farbmetrik, Peaks, Listener (`subscribe`), thread-sichere Befehle, Steuerrecht (Jobs, Lease, lokale Lease), Job-System (Einzelmessung, Messreihe, Dunkelspektrum, Kalibrierlauf, Ableitung) über Frame-Taps mit `waiting_for_user`.
- Desktop (`gui_dashboard.py`) rendert nur noch Engine-Frames (Qt-Signal-Brücke, Frames koalesziert); keine eigene Geräte-, Fusions-, Pipeline-, Heatmap- oder Peak-Logik mehr. `gui_cie_tab.py` übernimmt die Engine-Farbmetrik.
- Web (`web_server.py`) ist eine dünne Hülle über `api.create_app(engine)`; Legacy-`/api/...` und `/ws/stream` mit unveränderten Pfaden und Formaten.
- `spectrometer_core.py` Qt-frei (Tooltip-Helfer → `gui_help.py`).
- `docs/PARITAET_Desktop_Web.md`: Matrix, 25 Lücken, 15 Logik-Unterschiede.

**Phase 2 – API und Remote-Server**
- `/api/v1` mit Pydantic-Modellen (`api_models.py`), OpenAPI unter `/docs`, Fehlerformat `{"error":{code,message,details}}` (400/401/404/409/422/503).
- Gruppen: System, Gerät/Erfassung/Steuerrecht, Messen (measure/series/dark), Jobs, Daten (Live-Spektrum, Messungen, CSV/JSON-Export), Auswertung (color, peaks, filter, tm30), Berichte, Kalibrierung (Profile, Referenzen, Dunkelspektren, Kalibrierlauf, Ableitung, Protokoll), Heatmap/Export-Namen, Wellenlängenkalibrierung.
- Token: `Authorization: Bearer`; bei LAN zwingend, lokal konfigurierbar; WS `?token=`.
- `server_runner.ServerRunner` (uvicorn im Thread, sauberer Start/Stopp); Desktop-Panel „Remote access“ (an/aus, Port, Local/LAN, Token anzeigen/kopieren/erneuern, URL, Steuerrecht/Job mit Abbruch), Statusleiste mit „Cancel remote“, Sperre der Bedienelemente bei fremdem Steuerrecht.
- `PS-2600A_Pro.py --headless` ohne Qt-Import; `web_server.py` leitet weiter; Spec mit Hidden Imports und `static/`.

**Phase 3 – MCP und Plugin**
- `mcp_server/` (SDK `mcp` 2.3, `MCPServer`, stdio): 23 Werkzeuge, 3 Ressourcen, nur HTTP zur API, Antworten ≤ 8 KB, volle Daten als Dateien, keine löschenden Werkzeuge.
- `claude-plugin/` (`ps2600-spectrometer`): `.mcp.json`, Launcher, Agents `measurement`, `calibration`, `test-report`, Skills `light-source-characterization`, `filter-transmission`, `stability-measurement`, `test-report-template`, Claude-App-Beispielkonfiguration; lokaler Marketplace `.claude-plugin/marketplace.json`; `claude plugin validate` bestanden.
- `examples/api_client.py`.

**Phase 4 – Parität und Doku**
- Web: Token-Handling, Steuerrecht/Jobs mit Prompt-Dialog, Heatmap (Puffer, Intervall, Autosave, Clear, Export, Rewind), Data-Abschnitt, Belichtungsgrenzen, Capture dark, Kalibrierlauf und Ableitung über Jobs, Wellenlängenkalibrierung, Referenzquellen, Dunkelspektren, Profiltabelle, Protokoll-Links, lx/fc und Radiometrie-Faktor, Segmentlücken, Version.
- Desktop: Auswahl Display unit / Irradiance unit (P-22).
- `wavelength_calibration.py`: Qt-freie Wellenlängen-Sitzung, gemeinsam für Desktop-Dialog und `/api/v1/wavelength`.
- Matrix: keine offenen Lücken in *Messung/Einstellung* und *Kalibrierung* (P-15 bewusst nicht portiert).
- Doku: `README_WEB.md`, `docs/manual/manual.tex` (Architektur, Remote access, API, MCP, Konfiguration), `docs/TEST_api_mcp.md`, README/INSTALL/requirements.

## 3. Getroffene Entscheidungen

1. `api.py` (Legacy-Routen über der Engine) entstand bereits in Phase 1, nicht erst in Phase 2. So wurde die Web-Migration nicht doppelt gebaut.
2. Engine-Attribute heißen wie der frühere `ServerState`; `web_server.state` ist die Engine. Bestehende Skripte und Tests bleiben lauffähig.
3. Für alle Frames gilt jetzt ein einziger Verarbeitungspfad (`process_frame`). Verhaltensänderung im Desktop bei Geräten ohne Optical Black (LR-2T, HDX, Demo), Dunkelmodus `optical_black`: Steigung/Offset (1, 0) statt der PASCO-Konstanten (B-04; bisher nur im Web so).
4. Peaks im Desktop kommen aus `processing.detect_peaks` (gleicher Algorithmus, Parameter aus der Config).
5. Farbmetrik einheitlich: jeder 5. Frame, Lux mit `radiometric_calibration_factor` (das Web zeigte bisher kein Lux).
6. Verbinden setzt `lr2t_wl_offset_nm`/`lr2t_flip_pixels` jetzt auch im Web (wie bisher im Desktop). Die mögliche Doppelzählung des Offsets (Matrix B-05) wird im Hardwaretest geprüft.
7. Manuelle Belichtung schaltet die Auto-Belichtung überall aus (Desktop-Verhalten).
8. Der Heatmap-Puffer ist auch für das Web einstellbar (Engine, Standard 100).
9. `capture_snapshot` liefert das Signal vor der Antwortkorrektur aus der Pipeline. Bei aktivem v2-Profil ist es in counts/s statt counts; das ist für die Peaksuche im Wellenlängenassistenten ohne Belang.
10. Steuerrecht:
    - Ein laufender Job hält es für seinen Auftraggeber; dazu gibt es explizite Leases.
    - Der Desktop hält eine lokale Lease, solange der Kalibrierdialog offen ist. Diese lokale Lease kann remote weder per `force` übernommen noch freigegeben werden.
    - Remote-Clients unterscheidet `X-Client-Id`.
11. Messung:
    - `profile` wird nur für die Messung gesetzt und danach zurückgesetzt; t_int wird wiederhergestellt.
    - Dunkelmodi: `auto` wie die Live-Anzeige; `stored` liefert 422, wenn kein Dunkelspektrum vorhanden ist; `capture` mit Benutzerschritt; `none` ergibt Rohwerte ohne Profil.
12. Ein gemeinsames Token. `X-Client-Id` ist eine Kennzeichnung, keine Authentifizierung.
13. Lesende Endpunkte brauchen auch im LAN kein Token (Plan: Token für steuernde Endpunkte). `POST /api/v1/reports` braucht es, weil der Server dabei Dateien schreiben kann.
14. Gesperrte Schlüssel:
    - Remote nie lesbar: `remote_token`.
    - Remote nie setzbar: `remote_*` und die Pfade der Datendateien.
    - `export_dir` ist nur bei aktivem Token setzbar.
    - Fremde Origins sowie (bei Bindung an 127.0.0.1) fremde Host-Header werden abgewiesen.
15. `DELETE /api/v1/calibration/references/{id}` gibt es für die Web-Parität (P-12; Built-ins sind ausgenommen), aber nicht als MCP-Werkzeug. Profile und Dunkelspektren lassen sich in `/api/v1` nicht löschen.
16. Die Legacy-Antwortkorrektur (P-15) wird nicht portiert, weil sie durch den v2-Kalibrierlauf abgelöst ist.
17. MCP-SDK 2.x: `FastMCP` heißt jetzt `MCPServer`; das wurde am installierten Paket geprüft. Der Plugin-Name ist `ps2600-spectrometer`, weil das Präfix `claude-` reserviert ist. Der Launcher findet `mcp_server/` über `SPECTRO_MCP_DIR` oder das übergeordnete Repo-Verzeichnis.
18. Die Wellenlängenkalibrierung wurde aus dem Qt-Dialog in `wavelength_calibration.py` ausgelagert, damit die Logik nur einmal existiert. Neu ist der Ableitungs-Job (Teil von P-10).
19. Der Remote-Zugriff ist standardmäßig aus. `remote_enabled` startet ihn beim nächsten App-Start wieder.

## 4. Abweichungen vom Plan

- Eine einzige Test-Anpassung: In `tests/test_phase3_measurement.py::test_desktop_and_web_identical` setzt der Desktop-Teil den Zustand jetzt an seiner Engine statt an Dashboard-Attributen. Der Vergleich bleibt bitgenau, ist aber konstruktionsbedingt trivial, weil beide Oberflächen dieselbe Funktion nutzen.
- Berichtssprache (de/en): Der Parameter `language` wird angenommen und als Metadatum geführt. Die bestehenden Berichtsrenderer sind englisch und wurden nicht übersetzt; der Agent `test-report` schreibt den Prüfbericht in der gewünschten Sprache.
- `docs/manual/manual.pdf` wurde nicht neu übersetzt, weil auf dem Rechner kein XeLaTeX/LuaLaTeX vorhanden ist. Nur die `.tex`-Quelle ist aktualisiert.

## 5. Testergebnisse

- Ausgangsstand: 172 Tests grün.
- Endstand: **276 Tests grün** (`python -m pytest -q`, ca. 4 min). Neu: Engine, Qt-Freiheit, `/api/v1` komplett, Server-Thread, Headless ohne PyQt, Desktop-Remote-End-to-End, Remote-Panel, MCP-Werkzeuge, Wellenlängenkalibrierung, Web-Statik (inkl. `node --check`), Review-Regressionen.
- Abnahme Phase 1: Desktop und Web verarbeiten mit derselben Engine-Funktion. Es gibt keine doppelte Geräte- oder Pipeline-Logik mehr.
- Abnahme Phase 2: `tests/test_remote_e2e.py` startet die App (Demo), schaltet den Remote-Zugriff ein und misst per REST. Das Live-Spektrum per REST ist identisch zur GUI-Anzeige. `tests/test_headless.py` startet headless mit blockiertem PyQt6.
- Abnahme Phase 3: Claude Code 2.1.329 mit `--plugin-dir claude-plugin` gegen eine Headless-Instanz (Demo). Ergebnis: Status, Messung (Auto-Belichtung, 4 Frames), Farbmetrik (CCT 4569 K, Ra 85,4, R9 51,6), PDF-Farbbericht, Prüfbericht `pruefbericht.md` über den Agenten `test-report`.
- Abnahme Phase 4: Die Matrix hat keine offenen Lücken in *Messung/Einstellung* und *Kalibrierung*. Die Web-Oberfläche wurde per headless Chrome durchgeklickt, ohne JS-Fehler.
- Review (`review-opus`):
  - 2 kritische und 6 wichtige Befunde sind behoben und durch Tests abgesichert (`tests/test_review_security.py`):
    - Token-Leck über `/api/config` und WS-hello
    - remote schreibbare Sicherheitsschlüssel, Cross-Site-WebSocket, DNS-Rebinding
    - wirkungsloser Revoke-Knopf
    - unvollständige Spec
    - hängender Frame-Stream
    - fehlende Steuerrecht-Prüfungen (Heatmap, Autosave, Profil-Import/-Kopie, continue/cancel fremder Jobs)
    - lokale Lease remote brechbar
    - keine Frame-Koaleszenz im Desktop
  - Von den geringen Befunden sind G1 (Token-Vergleich als Bytes), G2 (Konfig/Profilwahl im Worker-Thread), G11 (`--reload` wird akzeptiert) und G15 (Prompt-Text) behoben. G14 war ein Fehlalarm.

## 6. Bekannte Einschränkungen und offene Punkte

- LAN-Betrieb läuft über unverschlüsseltes HTTP. Außerhalb des Labors ist ein Reverse-Proxy mit TLS nötig.
- Es gibt ein gemeinsames Token, keine Benutzerverwaltung, und `X-Client-Id` ist nicht authentifiziert.
- Desktop-App und eigenständiger Headless-Server können nicht gleichzeitig dasselbe Gerät öffnen.
- Offene geringe Review-Punkte:

  | Punkt | Befund |
  |---|---|
  | G3 | Statuswechsel bei cancel/run nicht unter Lock |
  | G4 | `auto_exposure`-Flag in Messreihen mit `dark=capture` |
  | G5 | 422 bei `dark=stored` erst nach der Lichterfassung |
  | G6 | `dark_for` im Job-Thread ändert den Live-Status |
  | G7 | Achsenwechsel leert den Heatmap-Puffer nicht |
  | G8 | Dateinamen-Vorschau setzt die Vorlage kurz global |
  | G10 | `uvicorn web_server:app --host 0.0.0.0` umgeht den Token-Zwang; der empfohlene Start ist `--headless` |
  | G12 | `port_free` mit `SO_REUSEADDR` |
  | G13 | Hub abonniert vor dem Lifespan |
  | G16 | Wildcard in `tools:` der Agents: Im Abnahmelauf funktionierte der Agent `test-report` |

- Die Desktop-GUI liest den Heatmap-Puffer der Engine ohne Kopie (Referenz auf das jeweils neue Array; ohne Einfluss auf Messwerte).
- Das Demo-Gerät unterstützt keine Wellenlängen-Fits. Fit und Speichern im Web sind deshalb nur mit Fake-Backend getestet; der Test mit einer Linienlampe steht aus.
- Der PyInstaller-Build wurde nicht ausgeführt. Mit `console=False` bleiben Banner und Token im Headless-Betrieb der EXE unsichtbar; das Token steht in der App bzw. in `spectrometer_config.json`.
- Die Hardwaretests (PASCO, LR-2T, HDX) und die Bedienung der Desktop-App nach der Migration stehen aus. Die Checkliste dafür ist `docs/TEST_api_mcp.md`.
