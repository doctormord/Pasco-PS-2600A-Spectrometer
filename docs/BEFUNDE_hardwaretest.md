# Befundliste Hardwaretest (Stand 2026-11-25)

Grundlage: Branch `future` nach PR #3 (API/MCP). Getestet: LR-2T (S/N ASQ_SPC5636146), blanke Faser M25L02 200 µm NA 0,22, Referenz HAL-CAL 1808001, relative Kalibrierung.

Ergebnis Verifikation: korrigiertes Spektrum der HAL-CAL vs. `.lmp` (normiert 550–650 nm): 425–949 nm ≤ ±1,2 %; 400–425 nm +1,9 %; 375–400 nm +4,9 %.

## Kalibrierablauf
1. Gain-Spanne nur als Hinweis bzw. Standardgrenze ≥ 200× (`calibration_max_gain_span`); Linienquellen über Abdeckung, Rauigkeit, isolierte Spitzen erkennen.
2. Kreuzprüfung (t/2) und Stabilitätsprüfung pro Pixel mit rauschabhängiger Toleranz; durchfallende Pixel begrenzen den gültigen Bereich (bisher p95 nur über signalstarke Pixel, gültiger Bereich enthielt Pixel mit C_half/C_cal bis 1,75 bzw. negativ).
3. Dunkelmessung vor und nach jeder Hellmessung, zeitlich interpoliert abgezogen (LR-2T: Dunkelversatz springt zwischen Läufen um +70 … −200 Counts).
4. Kreuzprüfung mit Ursachenmodell (konstanter Count-Versatz = Dunkeldrift, gleichmäßiger Faktor = Zeitversatz, signalabhängig = Nichtlinearität); Meldung mit Ursache und Betrag.
5. Linearitätskalibrierung pro Gerät (Seriennummer) als eigener Ablauf: 6–10 Integrationszeiten 10–90 % FS, Dunkel vor/nach, Referenz-Wiederholung gegen Lampendrift; Korrekturpolynom am Anfang der Pipeline (LR-2T, PASCO; HDX nur Kontrolle). LR-2T: ca. +1,5 % zwischen 39 % und 78 % FS.
6. Verifikation nach dem Speichern: Referenzlampe mit neuem Profil durch die Live-Pipeline messen, Abweichung zur Referenzdatei in % anzeigen, Referenzkurve als Overlay im Scope.

## Protokoll und Export
7. PDF-Protokoll: kein Überlappen (Warnings/Saved profiles, Kopfdaten/Diagrammtitel), kein Abschneiden in Tabellenzellen (Zeilenumbruch); NL-Korrektur ausdrücklich („on“/„off“/„n/a“).
8. CSV-Export: UTF-8 statt Windows-1252; Kopf dokumentiert die tatsächlich angewendeten Einstellungen (Profil-ID/Version, Einheit, tatsächlicher Dunkelmodus, Integrationszeit, Mittelung, Glättung, Datum/Uhrzeit mit Zeitzone, Gerät, Seriennummer); widersprüchliche Legacy-Einträge („Response compensation;OFF“ bei aktivem Profil) entfernen.

## Anzeige
9. Darstellungsarten wählbar: Raw ADC, Corrected counts (Standard bei aktivem Profil; im empfindlichsten Bereich ≈ Rohcounts), Normalised (max = 1), Count rate (counts/s), Absolute (nur Absolutprofil). Gleiches im Web.
10. In korrigierten Ansichten Aussteuerungsanzeige in ADC (Balken oder blasse Rohkurve) sichtbar halten.
11. Manuelle Y-Grenze je Darstellungsart bzw. Auto-Skalierung bei kalibrierten Einheiten; Neu-Skalierung beim Wechsel der Darstellung; Heatmap-Obergrenze nach derselben Logik.
12. Messwertanzeige: aktive Einheit statt fest „ADC“, „–“ statt „nan“ für maskierte Pixel (Desktop und Web).
13. Kalibrierdialog: erste Ergebniszeile („Valid segments …“) wird vom Diagramm abgeschnitten.

## Repo
14. `PS-2600A_Pro.spec` entfernen; `INSTALL.md`: Start über Python/requirements, kein exe-Build.
15. Laufzeitdaten (`calibration_profiles.json`, `spectrometer_config.json`, `dark_spectra.json`, Backups) in Benutzerdatenverzeichnis (`%APPDATA%\PS-2600A\` bzw. Linux/macOS-Äquivalent) mit Migration; im Repo nur Vorlagen; Einträge in `.gitignore`. Standard-Exportordner nicht im Repo.

## Offen
- Hardwaretest HDX (Dunkelniveau vor/nach Umstellung optical_black, Lux bei Absolutprofil) und PASCO.
