# Befundliste Hardwaretest (Stand 2026-11-26)

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
16. Ableitung einer Optik aus einer anderen (`DerivationWorkflow`): eigene Integrationszeit je Seite (Auto-Belichtung auf dasselbe Zielband oder fest), Dunkel vor/nach je Seite bei ihrer Integrationszeit, abschließende Wiederholung der bekannten Seite mit Driftprüfung (`calibration_derivation_drift_pct`), Quellenhinweis im Assistenten, Protokoll PDF/CSV; Desktop, `/api/v1` und Web.
18. Sättigungsschwelle `saturation_warn_frac` (Standard 0,90) für Desktop, Web, Aussteuerungsanzeige und Kalibrierabläufe.
19. Projektregeln als `CLAUDE.md` im Repository.

Bericht: `docs/ABSCHLUSSBERICHT_derivation.md`.

## Offen
- keine

## Später
17. Strahldichte: Einheit W/(m²·sr·nm), Profiltyp „absolute radiance“, Ablauf „absoluter Anker“ (relatives Profil × skalarer oder spektraler Faktor aus Referenzmessung). Weg ohne Reflexionsstandard: kleine Ulbricht-Kugel als Lambert-Quelle unter dem Objektiv, L = E·(r² + d²)/(π·r²) mit E aus CC-Absolutmessung im Abstand d über dem Port (Radius r), Kontrolle bei mehreren Abständen. Voraussetzung: Kugel vorhanden.
- Treiber für Ocean USB2000+ (älteres Ocean-Protokoll), nur falls ein solches Gerät angeschafft wird.

## Hardwaretest ausstehend
- Checkliste `docs/TEST_hardwaretest_fix.md`: LR-2T (Linearität, danach Spektralkalibrierung neu, Verifikation), HDX (Dunkelniveau optical_black, Lux bei Absolutprofil), PASCO, Migration.
- Ableitung (Punkt 16) am Gerät: bekannte Optik (z. B. CC) und neue Optik (blanke Faser) an von hinten beleuchtetem Opalglas; Integrationszeiten, Aussteuerung, Drift und Protokoll prüfen.
