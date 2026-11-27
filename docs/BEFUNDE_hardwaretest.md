# Befundliste Hardwaretest (Stand 2026-11-27, nach feature/radiance)

Grundlage: Branch `future`. Erster Test: LR-2T (S/N ASQ_SPC5636146), blanke Faser M25L02 200 µm NA 0,22, Referenz HAL-CAL 1808001, relative Kalibrierung.

Ergebnis Verifikation (vor PR #4): korrigiertes Spektrum der HAL-CAL vs. `.lmp` (normiert 550–650 nm): 425–949 nm ≤ ±1,2 %; 400–425 nm +1,9 %; 375–400 nm +4,9 %.

## Erledigt mit PR #4 (fix/hardware-test) – Hardwaretest ausstehend
1. Gain-Spanne als Hinweis, Standardgrenze 200×
2. Kreuz- und Stabilitätsprüfung pro Pixel, begrenzen den gültigen Bereich
3. Dunkelmessung vor und nach den Hellmessungen (Block), linear interpoliert
4. Ursachenmodell der Kreuzprüfung (Dunkeldrift, Zeitversatz, Nichtlinearität)
5. Linearitätskalibrierung pro Gerät
6. Verifikation nach dem Speichern inkl. Referenz-Overlay
7. PDF-Protokoll-Layout, NL-Korrektur ausdrücklich
8. Gemeinsame CSV-Exportfunktion, UTF-8, vollständiger Kopf
9. Darstellungsarten (Raw ADC, Corrected counts, Normalised, Count rate, Absolute)
10. Aussteuerungsanzeige in ADC in korrigierten Ansichten
11. Y-Achse je Darstellungsart, Heatmap-Obergrenze
12. Messwertanzeige mit Einheit, „–“ statt „nan“
13. Ergebniszeilen im Kalibrierdialog vollständig sichtbar
14. PyInstaller-Spec entfernt, INSTALL.md
15. Laufzeitdaten im Benutzerdatenverzeichnis mit Migration

## Erledigt mit feature/derivation-itime – Hardwaretest ausstehend
16. Ableitung: eigene Integrationszeit je Seite, Dunkel je Seite vor/nach, Wiederholung der bekannten Optik mit Driftbewertung (calibration_derivation_drift_pct = 2 %), Quellenhinweis, Protokoll PDF/CSV
18. Sättigungsschwelle saturation_warn_frac (0,90) für Desktop, Web, Auto-Belichtung und Kalibrierabläufe
19. CLAUDE.md im Repository

## Erledigt mit feature/radiance – Hardwaretest ausstehend
17. Strahldichte: Messgröße `irradiance`/`radiance` für Referenzquellen und Profile (Migration: bestehende Profile → irradiance), Eingangsoptik `objective`, Zulässigkeitsregel (absolut irradiance: CC/sphere; absolut radiance: bare/objective; relativ: alle); Kugel als Strahldichte-Referenz (≥ 3 Abstände, Ausgleichsrechnung mit Versatz d0, Konsistenz je 50-nm-Band, Drift, Unsicherheit); absolute Strahldichte-Kalibrierung; Leuchtdichte in cd/m²; Desktop, Web, `/api/v1`, MCP; Protokolle PDF/CSV. Bericht: `docs/ABSCHLUSSBERICHT_radiance.md`, Checkliste `docs/TEST_radiance.md`.
20. Testsuite-Absturz: drei Ursachen behoben – (a) GUI-Testmodule legten eigene QApplication an und zerstörten sie (jetzt eine für die ganze Sitzung), (b) TM-30-Thread des Farbtabs lief beim Abräumen des Fensters weiter (Qt-Abbruch ohne Traceback), (c) Pythons zyklische Speicherbereinigung lief in Hintergrundthreads und zerstörte dort Qt-Objekte (jetzt nur im GUI-Thread, auch in der Anwendung). Abnahme: 5 volle Läufe + 1 Lauf in zufälliger Reihenfolge grün.
21. Migrierte Altprofile außerhalb ihres gültigen Bereichs maskiert (NaN), Hinweis in Profilanzeige, Status und Exportkopf.
22. `calibration_itime_match_tol_pct` (0,5 %) und `calibration_dark_bright_frac` (0,5) konfigurierbar, im Protokoll ausgewiesen.
23. Ableitung: Status `drift_check_failed` mit Betrag, unabhängig von der Plausibilitätsprüfung.
24. PyInstaller-Beschreibungen aus `docs/PLAN_api_mcp.md` und `docs/TEST_api_mcp.md` entfernt.
25. CLAUDE.md: Prüfläufe und Hilfsskripte mit `PS2600A_DATA_DIR` (Temp) und `PS2600A_SKIP_MIGRATION=1`; lokale Agentendefinitionen angepasst.

## Offen
- keine

## Später
- Treiber für Ocean USB2000+ (älteres Ocean-Protokoll), nur falls ein solches Gerät angeschafft wird.

## Hardwaretest ausstehend
- Checkliste `docs/TEST_hardwaretest_fix.md`: LR-2T (Linearität, danach Spektralkalibrierung neu, Verifikation), HDX (Dunkelniveau optical_black, Lux bei Absolutprofil), PASCO, Migration.
- Ableitung blanke Faser ↔ CC bzw. Mikroskop (Punkt 16) am Gerät.
- Strahldichte (Punkt 17) nach `docs/TEST_radiance.md`: CC-Absolutprofil, Kugel vermessen (drei Abstände), Objektiv-Kalibrierung, Plausibilität der Leuchtdichte.
