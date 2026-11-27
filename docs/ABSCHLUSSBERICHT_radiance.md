# Abschlussbericht feature/radiance (Blöcke A–C)

Stand: 2026-11-27. Branch `feature/radiance` von `future` (f43ac25), drei Commits:

| Commit | Inhalt |
|---|---|
| Block A | Befunde 20–25 |
| Block B | Strahldichte (Befund 17) |
| Block C | Handbuch, Prüfliste, Befundliste, Korrekturen aus dem Review, dieser Bericht |

Kein Push, kein Merge.

## 1. Umgesetzte Punkte

### Block A – Befunde 20–25

**20 – Absturz der Testsuite.** Reproduziert: Die volle Suite brach sporadisch und ohne Python-Traceback ab. Betroffen waren `test_gui_remote`, `test_gui_display_modes` und `test_gui_theme`, je nach Lauf an anderer Stelle. Gefunden wurden drei Ursachen. Alle drei sind behoben, nicht umgangen:

1. **QApplication je Testmodul.** Jedes GUI-Testmodul hatte eine eigene `qapp`-Fixture mit Modul-Scope; drei Tests legten die Anwendung zusätzlich inline an.
   - Am Ende des Moduls, das die `QApplication` angelegt hatte, fiel die letzte Referenz weg. PyQt zerstörte die Anwendung, während Widgets, pyqtgraph-Objekte und Engine-Threads früherer Tests noch lebten.
   - Das nächste Modul legte dann eine zweite `QApplication` im selben Prozess an. Das ist in Qt undefiniertes Verhalten.
   - Behebung: eine einzige `QApplication` für die ganze Sitzung (`tests/conftest.py`, `qt_app()` und die Sitzungs-Fixture `qapp`), am Sitzungsende ein geordnetes Schließen aller Fenster.
   - Ein Guard-Test prüft jede Testdatei per AST: keine eigene `qapp`-Fixture, kein `QApplication(...)`.
2. **TM-30-Thread des Farbtabs.** Jeder Farbtab (also jedes Dashboard-Fenster) startet einen `QThread`, der nur bei `aboutToQuit` gestoppt wurde.
   - Dieses Signal kommt nicht, wenn ein Fenster geschlossen oder abgeräumt wird, ohne dass die Ereignisschleife endet (Tests, eingebettete Fenster).
   - Das Zerstören eines laufenden `QThread` ist ein fataler Qt-Fehler und beendet den Prozess ohne Traceback. Im Experiment reproduzierbar: Exit-Code 127, keine Ausgabe.
   - Behebung (Produktivcode, `gui_cie_tab.py`):
     - Der Thread wird beim Schließen des Fensters gestoppt (`CIETab.shutdown()` aus `DashboardWindow.closeEvent`).
     - Zusätzlich wird er beim Abräumen des Tabs gestoppt (`weakref.finalize`, hält nur den Thread).
     - Eine Registry verhindert, dass ein laufender Thread gelöscht wird; beim Prozessende stoppt `atexit` alle verbliebenen.
   - Ein Regressionstest läuft im Unterprozess.
3. **Speicherbereinigung in Hintergrundthreads.** Pythons zyklische Speicherbereinigung läuft in dem Thread, der gerade Speicher anlegt: Demo-Erfassung, Engine-Jobs, uvicorn-Server.
   - Sammelte sie dort Qt-Widgets oder pyqtgraph-Objekte ein, liefen deren C++-Destruktoren außerhalb des GUI-Threads: Segmentation fault ohne Traceback.
   - Behebung:
     - Anwendung (`PS-2600A_Pro.py`): pyqtgraph-`GarbageCollector`; automatische Bereinigung aus, Sammeln per Timer im GUI-Thread.
     - Tests: automatische Bereinigung aus, sobald die `QApplication` existiert; `gc.collect()` nach jedem Test im Hauptthread.
     - Ein Test prüft beides.

Nachweis vor und nach den Korrekturen:
- Vor der Behebung starben drei volle Läufe ohne Ausgabe (Exit 127 bzw. 139).
- Nach Ursache 1 allein, sowie nach Ursachen 1 und 2, stürzten volle Läufe weiterhin ab.
- Erst mit allen drei Korrekturen war die Suite grün. Die Abnahme steht in Abschnitt 4.

**21 – Migrierte Altprofile.**
- Profile mit `apply = legacy_gain` (migrierte Altprofile und der Altpfad „Referenz = zweite Messung“) liefern außerhalb von `valid_range_nm` den Gain NaN statt 1. Fehlen Grenzen, gilt der Tabellenrand.
- Innerhalb des Bereichs ist der Gain bitgleich zu vorher.
- Peaksuche und Farbmetrik vertragen NaN.
- Der Hinweis „Migrated legacy profile: no calibration outside lo–hi nm (masked).“ erscheint:
  - im Laufzeitstatus und in der Statuszeile von Desktop und Web,
  - in der Profiltabelle von Desktop und Web („legacy, masked outside …“),
  - im Exportkopf (Zeile `Profile note`),
  - in `/api/v1` bzw. MCP (`legacy_valid_range_nm`).
- Im Web-Frame sind maskierte Pixel auch ohne v2-Profil Lücken (`null`) statt 0.

**22 – Konfigurierbare Prüfgrenzen.**
- Neue Konfigurationsgrößen:
  - `calibration_itime_match_tol_pct` (Standard 0,5 %) für „gleiche Integrationszeit“: Erfassung, Dunkelablage, Zählrate, Ablauf, Ursachenmodell, Kugelcharakterisierung.
  - `calibration_dark_bright_frac` (Standard 0,5) für die Warnung „Dunkel ungewöhnlich hell“: Kalibrierung, Ableitung, Kugelcharakterisierung.
- Beide stehen in `record()["settings"]` und werden im PDF (Zeile „Limits“) und im CSV der Kalibrier- und Ableitungsprotokolle ausgewiesen.
- Die zuvor feste Toleranz von 0,5 % im Ursachenmodell (`calibration_profiles.check_causes`) nutzt jetzt denselben Wert (Befund aus dem Review).

**23 – Driftstatus der Ableitung.**
- Bei überschrittener Driftgrenze lautet der Status `drift_check_failed`. Dazu kommt `drift_status` mit `p95_pct`, `mean_pct`, `limit_pct` und der Meldung „Drift check failed: x % (limit y %), mean m %“.
- Die Driftprüfung wird vor der Plausibilität ausgewertet und gilt unabhängig von ihr. Gespeichert wird nur mit `save_on_failed_checks`.
- Desktop, Web und Ableitungsprotokoll zeigen den Driftstatus als Hauptmeldung.

**24 – PyInstaller.** Die Beschreibungen in `docs/PLAN_api_mcp.md` und `docs/TEST_api_mcp.md` (Abschnitt E, Vorbereitung, Ergebnistabelle, Einschränkungen) sind entfernt.

**25 – CLAUDE.md.**
- Abschnitt Daten: Prüfläufe und Hilfsskripte (auch von Subagenten) laufen mit `PS2600A_DATA_DIR` auf ein Temp-Verzeichnis **und** `PS2600A_SKIP_MIGRATION=1`.
- Die lokalen Agentendefinitionen `.claude/agents/impl-sonnet.md` und `.claude/agents/review-opus.md` sind angepasst: Standard-Branch `future`, Grundlage „Plan oder Befundliste aus dem Auftrag“, Prüfläufe mit beiden Variablen. Sie sind nicht committet (`.claude` ist ignoriert).

### Block B – Strahldichte

**B1 – Messgrößen und Einheiten** (`reference_sources.py`, `calibration_profiles.py`, `processing.py`)
- Die Messgröße `irradiance` bzw. `radiance` ist ein eigenes Merkmal von Referenzquellen (`ReferenceSource.quantity`) und Profilen (Feld `quantity`).
- Einheiten:
  - Bestrahlungsstärke: W/(m²·nm), mW/(m²·nm), µW/(cm²·nm).
  - Strahldichte: W/(m²·sr·nm), mW/(m²·sr·nm), µW/(cm²·sr·nm).
  - `convert_units` rechnet nur innerhalb derselben Messgröße um; `convert_irradiance` bleibt als Alias.
- Migration:
  - Profile ohne `quantity` werden beim Laden zu `irradiance` (absolute ausdrücklich, relative ebenfalls).
  - Bibliotheksquellen ohne `quantity` erhalten die Messgröße aus der Einheit, sonst `irradiance`.
- Eingangsoptik `objective` ist neu.
- Zulässigkeit: genau eine Regel, `calibration_profiles.admissible(kind, quantity, input)`; die Matrix liefert `admissibility_matrix()`.

  | Typ / Messgröße | Zulässige Eingangsoptiken |
  |---|---|
  | absolut irradiance | CC, sphere |
  | absolut radiance | bare, objective |
  | relativ (beide Messgrößen) | alle |

- Verwendet wird die Regel im Assistenten (Desktop), im Profilspeicher, in der Ableitung, in `/api/v1` (422 mit Begründung, Endpunkt `GET /calibration/admissibility`), im Web (Optionen gesperrt, Begründung als Hinweis) und in MCP.

**B2 – Kugel als Strahldichte-Referenz** (`calibration_sphere.py`)
- Eingaben:
  - Name und Kennung der Kugel.
  - Portradius r.
  - Abstand Messerkantenebene – Gehäuseoberseite a (Vorgabe 11,00 mm).
  - Lage der CC-Referenzebene relativ zur Auflagefläche c (Vorgabe 0).
  - u(r), u(d) und u(CC) (Vorgabe `sphere_cc_uncertainty_pct` = 3 %).
- Abstand: d_i = a + h_i + c, mit h_i = Gehäuseoberseite → CC-Auflage.
- Voraussetzung: ein aktives Absolutprofil `irradiance` mit Eingang CC (v2, gültig für Gerät, Wellenlängenkalibrierung und Linearisierung). Sonst bricht der Ablauf mit einer klaren Meldung ab (Desktop: Meldung; API: 422).
- Ablauf:
  - Integrationszeit für den ganzen Lauf, Auto-Belichtung beim ersten (kleinsten) Abstand.
  - Dunkel vor dem Block.
  - E(λ) bei N ≥ 3 Abständen (`sphere_min_positions`).
  - Wiederholung des ersten Abstands.
  - Dunkel nach dem Block; jede Messung wird mit dem zeitlich interpolierten Dunkel korrigiert.
  - E = Zählrate · K des CC-Profils, maskiert außerhalb seines gültigen Bereichs und unter der SNR-Grenze.
- Auswertung je Wellenlänge:
  - L_i = E_i·(r² + d_i²)/(π·r²).
  - Ausgleich E(d) = π·L·r²/(r² + (d + d0)²) mit freiem L(λ) und gemeinsamem d0, in ln E (relative Residuen), gesamt und je 50-nm-Band.
  - Verwendet wird L aus dem Fit, wenn |d0| < `sphere_d0_max_mm` (1,0 mm), sonst der Mittelwert von L_i mit Warnung.
  - Konsistenz: Streuung von L_i über die Abstände je Band und gesamt, Grenze `sphere_consistency_max_pct` (2 %).
  - Drift: p95 der Wiederholung, Grenze `sphere_drift_max_pct` (1 %).
- Unsicherheit (vereinfachte GUM-Fortpflanzung, relativ, k = 1) mit den Beiträgen:
  - c_r·u(r) und c_d·u(d),
  - Streuung/√N,
  - Drift/√3,
  - u(CC),
  - c_d·|d0|/√3 (Review-Befund, siehe Entscheidung 4).
  - Ausgewiesen werden der Median, das Maximum, U (k = 2) und die Werte je Band.
- Ergebnis: Bibliotheksquelle mit Messgröße `radiance`, Einheit µW/(cm²·sr·nm) und Unsicherheit je Stützstelle.
  - Gültiger Bereich: größter zusammenhängender Bereich im Schnitt aus CC-Profilbereich und gültiger Messung.
  - Metadaten: Geometrie, Datum, Gerät, CC-Profil-ID/Version, Konsistenz, Drift, Unsicherheit.
- Protokoll PDF/CSV: `calibration_report.write_sphere_protocol`.
- Anbindung: Desktop-Assistent (dritter Modus), Engine-Job `sphere`, `POST /api/v1/calibration/spheres`, Web-Dialog und MCP-Werkzeug `start_sphere_characterization`.

**B3 – Absolute Strahldichte-Kalibrierung**
- Der Spektralkalibrier-Assistent übernimmt die Messgröße aus der Referenzquelle. Typ `absolute` mit einer Strahldichte-Quelle ergibt ein Profil `absolute`, `quantity = radiance` mit Einheit „uW/cm2/sr/nm per count/s“ und zusätzlich das abgeleitete relative Profil mit derselben Messgröße.
- Hinweis `RADIANCE_SOURCE_HINT` (Sehfeld vollständig und gleichmäßig ausfüllen; Kugelport unter dem Objektiv, Fokus auf die Portebene) erscheint in Assistent, Web, Job-Ergebnis und Protokoll.
- Ableitung: Die neue Optik darf `objective` sein. Bekannte Profile beider Messgrößen sind zulässig; das abgeleitete relative Profil übernimmt die Messgröße des bekannten Profils.

**B4 – Anwendung**
- Bei einem Strahldichteprofil liefert `CalibrationRuntime` die Strahldichteeinheit mit der Vorsilbe der Einstellung „Absolute unit“ (Konfigurationsschlüssel `irradiance_unit`).
- Anzeige, Statuszeile und Exportkopf (Zeile `Quantity`) führen Messgröße und Einheit.
- Leuchtdichte L_v = 683 lm/W · ∫L·V dλ in cd/m² ersetzt bei Strahldichte die Beleuchtungsstärke (lx):
  - Desktop: gleiche Kachel, Beschriftung wechselt, fc „—“.
  - Web: Farbkarte, Farbbericht.
  - API: `quantity`, `luminance_cd_m2` in `/status`, `/spectrum/latest`, `/analyze/color`.
  - MCP: `status`, `calibration_status`, `measure`, `get_spectrum`, `analyze_color`, `list_profiles`, `list_references`.
- Referenz-Overlay unskaliert nur bei gleicher Messgröße; die Verifikation wird bei abweichender Messgröße vorab mit 422 abgelehnt.
- Plugin-Agents `measurement`, `calibration` (inkl. Kugelablauf) und `test-report` sowie zwei Skills sind ergänzt.

**B5 – Tests (neu)**

| Datei | Inhalt |
|---|---|
| `tests/test_sphere_model.py` | Lambert-Scheibe mit bekanntem L, r, d0 (rekonstruiert L < 0,5 %, d0 < 0,1 mm, auch mit Rauschen); falsches Abstandsgesetz und deutlich nicht-Lambert'scher Port (cosⁿ, n = 2, 4) erkannt; leicht nicht-Lambert'scher Port (n ≤ 1) von der Unsicherheit gedeckt; Unsicherheitskomponenten; Einheiten und Messgrößen; Leuchtdichte gegen den analytischen Fall |
| `tests/test_radiance_demo.py` | Demo-Gesamtdurchlauf CC-Absolutprofil → Kugel charakterisieren → Objektivprofil `radiance` → Messung in µW/(cm²·sr·nm) mit Leuchtdichte (3 % gegen den Erwartungswert); Voraussetzungen; Zulässigkeitsmatrix (14 Fälle); Migration alter Profile; Einheiten des Appliers |
| `tests/test_api_radiance.py`, `tests/test_mcp_radiance.py` | Durchlauf über `/api/v1` bzw. MCP inkl. Benutzerschritte, 422-Fälle, Protokoll-Download |
| `tests/test_gui_radiance.py` | Zulässigkeit im Assistenten, Kugelmodus (Voraussetzung, kompletter Durchlauf, Rückfrage bei fehlgeschlagener Prüfung), Lampenimport mit sr-Einheit, Farbtab mit cd/m² |
| `tests/test_sphere_report.py` | Kugelprotokoll PDF/CSV, Messgröße in Kalibrier- und Ableitungsprotokoll |
| `tests/test_review_radiance.py` | Korrekturen aus dem Review |

Alle Testläufe liefen mit `PS2600A_DATA_DIR` auf Temp-Verzeichnissen und `PS2600A_SKIP_MIGRATION=1`.

### Block C – Abschluss
- `docs/manual/manual.tex`: neues Kapitel „Spectral radiance“ mit Messgrößen, Zulässigkeit, Kugelcharakterisierung, Formel, Ausgleich, Unsicherheit, Kalibrierung, Messung und Grenzen; die Grenzen der Spektralkalibrierung sind angepasst.
- `docs/TEST_radiance.md`: Checkliste für den Hardwaretest.
- `docs/BEFUNDE_hardwaretest.md`: 17 und 20–25 unter „Erledigt“, Hardwaretest Strahldichte unter „ausstehend“.
- Review durch `review-opus`: kein kritischer Befund, 5 wichtige Befunde (alle behoben, siehe Abschnitt 5) und 12 geringe Befunde.

## 2. Entscheidungen

1. **Integrationszeit der Kugelcharakterisierung:** eine gemeinsame für alle Abstände, per Auto-Belichtung am kleinsten (hellsten) Abstand. Das ergibt zwei Abdeck-Schritte statt zwei je Abstand. Da mit Zählraten gerechnet wird, beeinflusst die Integrationszeit das Ergebnis nicht. Fernere Abstände haben weniger Signal, liegen aber über der SNR-Grenze. Sättigt ein späterer Abstand, bricht der Ablauf mit einer Meldung ab.
2. **Erster Abstand:** Verwendet wird der Mittelwert aus Messung und Wiederholung, also das Niveau zur Mitte des Laufs. Eine lineare Lampendrift wirkt so auf alle Abstände etwa gleich.
3. **Ausgleichsrechnung in ln E** (relative Residuen): Abstände mit kleinem Signal werden gleich gewichtet. d0 wird zuerst grob gerastert und dann verfeinert, um Nebenminima zu vermeiden. Die Bänder werden nur ausgewiesen, nicht bewertet.
4. **d0 in der Unsicherheit:** Auf der Achse lassen sich ein Versatz der Referenzebenen und ein leicht nicht-Lambert'scher Port (L ∝ cosⁿθ) grundsätzlich nicht trennen; beide wirken wie d0. Statt eine Trennung vorzutäuschen, geht |d0| als Rechteckbeitrag c_d·|d0|/√3 in die Unsicherheit ein. Geprüft für n = 0,25/0,5/1: Der Fehler von L (1,0/2,0/4,1 %) liegt jeweils unter u (1,1/2,2/4,3 %). Bei |d0| > 2·u(d) erscheint eine Warnung. Deutliche Abweichungen (n ≥ 2) erkennt die Konsistenzprüfung.
5. **Konsistenzkriterium:** die Streuung mit der tatsächlich verwendeten Geometrie (d + d0); die nominelle Streuung (d0 = 0) wird zusätzlich ausgewiesen. Bestanden heißt: gesamt und jedes Band ≤ Grenze.
6. **Gespeicherter Bereich:** der größte zusammenhängende gültige Bereich. Lücken innerhalb des CC-Profilbereichs werden nicht überbrückt. L wird wie eine Kalibrierung geglättet (`calibration_smooth_px`).
7. **Die Kugelquelle ist an keine Eingangsoptik gebunden** (`input_optic` leer): Sie ist für `bare` und `objective` gleichermaßen verwendbar.
8. **Einheitenwahl:** eine Einstellung („Absolute unit“, Schlüssel unverändert `irradiance_unit`). Bei Strahldichte gilt dieselbe Vorsilbe pro sr. Ein zweiter Schlüssel `radiance_unit` hätte Desktop, Web und API um eine weitere Einstellung erweitert, ohne Mehrwert.
9. **Ableitung (B3):** Eine Ableitung ergibt immer ein relatives Profil (S_new = S_known·C_new/C_known); die Messgröße wird übernommen. Eine „absolute Strahldichte durch Ableitung“ aus einem CC-Profil ist physikalisch nicht möglich, weil die Geometrie fehlt. Absolute Strahldichte entsteht nur gegen eine Strahldichte-Quelle.
10. **Migration relativer Profile:** Auch relative Profile ohne `quantity` werden als `irradiance` geführt. Die Messgröße ist dort nur Information und ändert die Anwendung nicht.
11. **Altpfad „Device default“** (Treiber-`RESPONSE_TABLE`) bleibt unmaskiert. Er ist kein migriertes Profil, und die Treibertabellen decken den Gerätebereich ab.
12. **Zufallsreihenfolge:** `pytest-random-order` statt `pytest-randomly`, weil es nur mit `--random-order` mischt und normale Läufe unverändert lässt. Das Paket ist lokal installiert; `requirements.txt` ist unverändert, da es ein reines Prüfwerkzeug ist.
13. **Speicherbereinigung in der Anwendung** über den pyqtgraph-`GarbageCollector` (Intervall 1 s). Die Ursache für sporadische Abstürze in Tests besteht in der Anwendung genauso (Geräte-, Fusions- und Serverthreads).
14. **Driftkenngröße der Kugel:** p95 der Abweichung zwischen erstem Abstand und Wiederholung, das Verhältnis über die Auswertebandbreite (`sphere_band_nm`, 50 nm) geglättet statt über 21 Pixel. Eine Quellendrift, auch eine spektrale wie eine Farbtemperaturänderung, ist auf dieser Skala glatt; das Pixelrauschen mittelt sich heraus. Schlüssel und Protokollfelder sind unverändert.

## 3. Abweichungen vom Auftrag

- **Handbuch-PDF:** In dieser Umgebung ist kein LaTeX-Compiler vorhanden. `docs/manual/manual.tex` ist ergänzt, `docs/manual/manual.pdf` nicht neu erzeugt.
- **Befund 20 (Wiederholungsläufe) und Lesart des Auftrags:** Die Abnahme wurde am Endstand (Block C) durchgeführt, nicht am Block-A-Commit allein. Sie umfasst damit alle Änderungen. Block A wurde zusätzlich einmal voll und grün geprüft (463 passed).
- **Ablauf der Kugelcharakterisierung:** „Dunkel vor/nach“ ist als Block-Dunkel vor der ersten und nach der letzten Messung umgesetzt (Entscheidung 1), nicht je Abstand.

## 4. Testergebnisse

- Basislauf vor allen Änderungen (`future`): 409 passed (10:17 min).
- Block A allein, alle drei Korrekturen zu Befund 20: 463 passed (11:36 min).
- Block B vollständig: 528 passed (19:41 min).
- Review-Teillauf (`review-opus`, neue Testdateien): 124 passed.

**Abnahme Befund 20** auf Commit `dbb567c` (Endstand Block C ohne diesen Bericht), jeweils frisches Temp-Datenverzeichnis und `PS2600A_SKIP_MIGRATION=1`, `QT_QPA_PLATFORM=offscreen`, Faulthandler aktiv:

| Lauf | Start | Ausführung | Ergebnis | Dauer |
|---|---|---|---|---|
| 1 | 2026-11-27 15:51:45 +01:00 | einzeln | 540 passed | 18:56 min |
| 2 | 2026-11-27 16:11:45 +01:00 | parallel, eigenes Worktree | 540 passed | 19:32 min |
| 3 | 2026-11-27 16:11:45 +01:00 | parallel, eigenes Worktree | 540 passed | 19:40 min |
| 4 | 2026-11-27 16:11:45 +01:00 | parallel, eigenes Worktree | 540 passed | 19:40 min |
| 5 | 2026-11-27 16:11:45 +01:00 | parallel, eigenes Worktree | 540 passed | 19:38 min |
| zufällige Reihenfolge (`--random-order --random-order-seed=383551415`) | 2026-11-27 16:11:45 +01:00 | parallel, eigenes Worktree | 540 passed | 19:23 min |

Ergebnis: 5 volle Läufe und 1 Lauf in zufälliger Reihenfolge grün, keine Abstürze. Die Läufe 2–5 und R liefen gleichzeitig (5 Prozesse auf 24 Kernen), um Zeit zu sparen. Für zeitkritische Tests ist das strenger als Einzelläufe.

Ein erster Abnahmedurchgang auf dem vorherigen Stand von Block C (`3417678`) lieferte in 4 der 6 Läufe 540 passed. In Lauf 4 und im Lauf in zufälliger Reihenfolge scheiterte je ein Test (`test_gui_radiance.py::test_sphere_run_in_the_wizard`), kein Absturz. Ursache: Die Driftprüfung der Kugel (Wiederholung des ersten Abstands, p95 der Abweichung) glättete das Verhältnis nur über 21 Pixel. Bei dem schwachen Testsignal (≈ 540 Counts Spitze, 12 Frames) lag allein das Rauschen des CC-kalibrierten Spektrums bei 0,9–1,0 % und damit an der Grenze von 1 %; die mittlere Drift lag bei 0,05 %. Korrektur: Das Verhältnis wird jetzt über die Auswertebandbreite `sphere_band_nm` geglättet, denn eine Quellendrift ist spektral glatt. Der Test fährt ein realistisches Signal (60 ms statt 20 ms) und war danach 15/15 grün. Handbuch und Prüfanleitung sind angepasst. Ein zweiter Durchgang (`94317ec`) war in allen fünf vollen Läufen grün (540 passed). Im Lauf in zufälliger Reihenfolge scheiterte `test_api_radiance.py::test_full_chain`. Ursache: ein Timing-Race im Test, kein Produktfehler. Die oberste Angabe `quantity` in `/api/v1/status` beschreibt den angezeigten Frame (`engine.current_unit`) und folgt dem Profilwechsel erst mit dem nächsten verarbeiteten Frame. Der Test prüfte sie direkt nach Jobende und wartete erst danach auf den ersten Strahldichte-Frame. Korrektur: Der Test wartet zuerst auf den Frame. Der Kalibrierstatus (`/api/v1/calibration/status`) wird weiterhin sofort geprüft. Das gleiche Muster in `test_mcp_radiance.py` ist ebenso korrigiert. Beide Dateien liefen danach mehrfach grün. Die Tabelle oben gilt für den korrigierten Stand.

## 5. Review (review-opus)

Kritisch: keine.

Wichtig (alle behoben):
1. Der Web-Farbbericht rechnete bei einem Absolutprofil mit Werten in µW statt W: Leuchtdichte bzw. Beleuchtungsstärke waren 100× zu hoch. Er nutzt jetzt dieselben photometrischen Eingangsgrößen wie die Engine. Ein Test vergleicht beide Wege.
2. Ein leicht nicht-Lambert'scher Port wird von d0 absorbiert, und die Konsistenz bleibt unauffällig. Behoben mit dem d0-Beitrag zur Unsicherheit und einer Warnung; Tests mit dem physikalischen cosⁿ-Modell; Aussage im Handbuch korrigiert.
3. Das Ursachenmodell hatte noch eine feste Toleranz von 0,5 % für „gleiche Integrationszeit“. Es nutzt jetzt die Konfigurationsgröße; Test ergänzt.
4. Die Profillisten in API, MCP und Web hatten keine Messgröße. Ergänzt sind `quantity`, `input`, `legacy` und `legacy_valid_range_nm`, im Web eine Spalte „Quantity“ und der Legacy-Hinweis.
5. Abschlussbericht fehlte: dieses Dokument.

Gering:
- Behoben:
  - „nan“ im Drifttext; eigene Warnung, wenn die Drift nicht auswertbar ist.
  - Warnung „Dunkel ungewöhnlich hell“ auch bei der Kugel.
  - Kugelquelle nicht an `objective` gebunden.
  - PDF-Beschriftung der CC-Referenzebene.
  - Overlay-Cache: Kurve und Skalierungsentscheidung werden gemeinsam gespeichert (thread-sicher).
  - Verifikation mit anderer Messgröße wird vorab mit 422 abgelehnt.
  - Maskierte Legacy-Pixel sind im Web-Frame Lücken.
  - `/spectrum/latest` berechnet nur die Leuchtdichte statt der vollen Farbmetrik.
  - Web-Einstellung „Absolute unit“ wie am Desktop, mit drei Vorsilben.
- Als Einschränkung aufgenommen:
  - d0 je Band wird nur ausgewiesen (siehe Abschnitt 7).
  - Cosinusfehler des CC (siehe Abschnitt 7).
  - Speicherbereinigung vor dem Anlegen der `QApplication`: unkritisch, da es dann noch keine Qt-Objekte gibt.

## 6. Änderungen an Exportformaten und Dateien (Schnittstellen)

Bestehende Zeilen und Spalten wurden nicht umbenannt oder entfernt, mit zwei ausdrücklichen Ausnahmen, die nur bei Strahldichte bzw. im Profiltext greifen (unten markiert).

**Messungs-/Spektrum-CSV (Exportkopf)**
- Neue Zeile `Quantity` nach `Unit`, nur bei absoluten Einheiten.
- Neue Zeile `Profile note` bei Legacy-Profilen.
- **Wert geändert:** `Calibration profile` lautet bei Absolutprofilen „… · absolute irradiance · …“ bzw. „… · absolute radiance · …“ statt „… · absolute · …“ (Profilbeschriftung).

**Kalibrierprotokoll-CSV**
- Neue Kopfzeilen `# quantity` und `# source_hint` (nach `# unit`), `itime_match_tol_pct` und `dark_bright_frac`.
- Neue `setting_*`-Zeilen für die neuen Konfigurationsgrößen.

**Ableitungsprotokoll-CSV**
- Neue Kopfzeilen `# quantity`, `itime_match_tol_pct`, `dark_bright_frac`.
- `drift_status` (nur bei nicht bestandener Driftprüfung).

**Farbbericht (CSV/PDF)**
- **Bei Strahldichte ersetzt** die Zeile `Luminance` (cd/m²) die Zeile `Illuminance` (lx). Bei Bestrahlungsstärke bleibt alles unverändert.

**Neues Kugelprotokoll (PDF und CSV)**
- Format wie die übrigen Protokolle: `# key;value`-Kopf, `;`-getrennte Spalten.
- Spalten: `wavelength_nm`, `E_<i>_uW_cm2_nm`, `E_repeat_uW_cm2_nm`, `L_<i>_uW_cm2_sr_nm`, `L_fit`, `L_mean`, `L_used`, `spread_pct`, `u_rel_pct`, `valid`.

**Dateien im Benutzerdatenverzeichnis**
- Profile: Feld `quantity` (neue und migrierte Profile; die Datei wird beim nächsten Speichern mit dem Feld geschrieben).
- Referenzbibliothek: Felder `quantity`, `meta`, `uncertainty`.
- `spectrometer_config.json` bzw. die Vorlage: neue Schlüssel `calibration_itime_match_tol_pct`, `calibration_dark_bright_frac`, `sphere_min_positions`, `sphere_band_nm`, `sphere_consistency_max_pct`, `sphere_d0_max_mm`, `sphere_drift_max_pct`, `sphere_cc_uncertainty_pct`.
- **Rückwärtsgrenze:** Eine ältere Programmversion lehnt Bibliotheksquellen mit sr-Einheit beim Laden ab. Profile mit `quantity` liest sie, ignoriert das Feld aber; ein Strahldichteprofil würde sie wie ein Bestrahlungsstärkeprofil anzeigen.

**API `/api/v1`** (nur Ergänzungen; `/api/...` unverändert)
- Neue Endpunkte:
  - `POST /calibration/spheres`
  - `GET /calibration/spheres/{job_id}/protocol`
  - `GET /calibration/admissibility`
- Neue Felder:
  - `quantity`, `luminance_cd_m2`, `photometric_quantity`
  - Profillisten: `quantity`, `input`, `legacy`, `legacy_valid_range_nm`
  - Referenzen: `quantity`, `unit_label`, `meta`
  - `drift_status`
- Neue Statuswerte:
  - `drift_check_failed` (Ableitung)
  - `saved`, `not_saved_checks_failed` mit `saved_checks_failed` (Kugel)
- Eingang `objective`; Referenzimport mit optionalem `quantity`.
- Legacy `/api/color/report`: optionales Feld `unit` (empfohlen); ohne `unit` wie bisher, maskierte Werte als 0.

**MCP**
- Neues Werkzeug `start_sphere_characterization`.
- `list_profiles` mit `quantity`, `input`, `legacy_valid_range_nm`.
- `optics_input` mit `objective`.

## 7. Bekannte Einschränkungen

- Hardwaretest Strahldichte ausstehend (`docs/TEST_radiance.md`). Alle Ergebnisse beruhen auf dem Demo-Backend und synthetischen Daten.
- Die Scheibenformel setzt einen gleichmäßigen Lambert-Port voraus.
  - Ein leicht nicht-Lambert'scher Port ist nur über die Unsicherheit abgedeckt, nicht korrigiert.
  - Die Gleichmäßigkeit über die Portfläche wird auf der Achse nicht geprüft.
- Der Cosinusfehler des CC bei Einfallswinkeln bis arctan(r/d) (≈ 32° bei d = 11 mm) ist nicht eigens modelliert, sondern Teil von u(CC).
- Die d0-Werte je Band werden ausgewiesen, aber nicht bewertet. Eine wellenlängenabhängige Geometrie fällt nur über die Bandstreuung auf.
- Die Unsicherheit ist vereinfacht: Die Eingangsgrößen sind unkorreliert, und u(CC) ist eine Eingabe, keine Messung.
- Die Driftprüfung der Kugel vergleicht gegen eine feste Grenze, nicht gegen das erwartete Rauschen. Bei sehr schwachem Signal (wenige hundert Counts, wenige Frames) kann sie ohne echte Drift anschlagen. Abhilfe: mehr Signal oder Frames. Das Speichern ist nach Rückfrage trotzdem möglich und wird im Protokoll vermerkt.
- Das Sehfeld der Strahldichte-Optik muss vollständig im gleichmäßigen Teil des Ports liegen. Das wird nur als Hinweis gegeben, nicht geprüft.
- `docs/manual/manual.pdf` ist nicht neu erzeugt (kein LaTeX in dieser Umgebung).
- Einige ältere Dokumente (`docs/ABSCHLUSSBERICHT_api_mcp.md`, `docs/TEST_api_mcp.md`) sprechen weiterhin von „Irradiance unit“. Sie sind historisch und wurden nicht geändert.
