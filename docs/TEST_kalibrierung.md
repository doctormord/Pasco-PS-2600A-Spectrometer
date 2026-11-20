# Hardwaretest Kalibrierung (Phase 4)

Checkliste zum Abhaken für den Hardwaretest der spektralen Kalibrierung
(Plan: `docs/PLAN_radiometrische_kalibrierung.md`, Abschnitte 4 und 5; Hintergrund: Handbuch, Kapitel "Spectral calibration").
Der Ablauf ist für jedes Backend und jede Optik gleich und braucht keine Codeänderung.

Eintragen bei jedem Schritt: Datum, Gerät/Seriennummer, Optikkonfiguration, Auffälligkeiten.

## 0. Vorbereitung

- [ ] Branch `feature/spectral-calibration` ausgecheckt, Stand notiert (`git rev-parse --short HEAD`): ______
- [ ] `python -m pytest -q` läuft grün (Ergebnis notieren): ______
- [ ] Backup angelegt (Kopie in separaten Ordner, mit Datum):
  - [ ] `spectrometer_config.json`
  - [ ] `calibration_profiles.json` (Migration legt zusätzlich `calibration_profiles.v1.bak.json` an)
  - [ ] `dark_spectra.json`
  - [ ] `reference_sources.json`
- [ ] Halogen-Kalibrierlampe (HAL-CAL) eingeschaltet, mindestens 15-30 min aufgewärmt (Herstellerangabe beachten)
- [ ] Fasern, Adapter, Eingangsoptik (CC) fest verschraubt/fixiert; bis Ende der Messreihe nicht lösen
- [ ] Raumlicht aus bzw. abgeschirmt; Abdeckung für den Eingang (Dunkelspektrum) bereitgelegt
- [ ] Ordner für Protokolle (PDF/CSV) und Exporte angelegt

## A. Ocean HDX

### A1 Wellenlänge (Cd + Hg, Multi-Lamp)

- [ ] Reiter "Wavelength": Cd und Hg nacheinander erfassen, Linien über den gesamten Anzeigebereich (ca. 350 nm bis Gerätegrenze) kombinieren
- [ ] Abdeckungswarnung erscheint nicht (oder ist begründet), Residuen notiert: max ______ nm, RMS ______ nm
- [ ] "Fit & apply", "Save to device"; danach Wellenlänge nicht mehr ändern
- [ ] Fingerprint geprüft: im Reiter "Spectral calibration", Schritt 2, wird ein Fingerprint angezeigt; Wert notiert: ______

### A2 Lampe importieren

- [ ] Reiter "Lamps": `docs/1808001-NIR-CCVISNIR.lmp` (bzw. die zur Lampe passende Datei) importieren
- [ ] Name, Seriennummer der Lampe, Einheit (`uW/cm2/nm`), untere Grenze 380 nm, obere Grenze laut Datei, Eingangsoptik CC eingetragen
- [ ] Lampe erscheint in der Bibliothek; Datenbereich plausibel (300-2500 nm, Maximum bei ca. 1250 nm)

### A3 Absolut mit CC-VIS/NIR

- [ ] Optikkonfiguration benannt (z. B. `HDX-CC-VISNIR`), Eingangsoptik CC, Typ absolut, Referenzquelle HAL-CAL gewählt
- [ ] CC in der Lampenaufnahme montiert, für die die Lampenkalibrierung gilt
- [ ] Auto-Belichtung auf 70-85 % Full Scale, dann fixiert; t_int notiert: ______ ms
- [ ] Abdecken, beide Dunkelspektren (t_int und ca. t_int/2) in einem Abdeckvorgang erfasst, wieder aufdecken
- [ ] Kalibrierspektrum erfasst; Stabilitätsprüfung: ______ % (Erwartung < 1 %)
- [ ] t_int-Kreuzprüfung: ______ % (Erwartung < 2 %)
- [ ] Gültiger Bereich: von ______ nm bis ______ nm (Erwartung: ca. 380 nm bis Gerätegrenze)
- [ ] Gain-Spanne und Warnungen notiert: ______
- [ ] Als neue Profilversion gespeichert; abgeleitetes relatives Profil ebenfalls vorhanden
- [ ] Kalibrierprotokoll (PDF und CSV) erzeugt und abgelegt

### A4 Relativ mit blanker Faser

- [ ] Optikkonfiguration `HDX-bare` mit Eingangsoptik "bare"; "absolut" ist nicht wählbar (erwartet)
- [ ] Variante 1, direkt: Faser reproduzierbar zentriert im Lampenport (Adapterhülse), Typ relativ, Ablauf wie A3
- [ ] Variante 2, Ableitung: "Derive from other optics" (Reiter "Spectral calibration", Mode) mit CC-Profil als bekannter Optik; dieselbe diffuse, ausgedehnte Quelle nacheinander mit CC und blanker Faser
- [ ] Gültiger Bereich, Stabilität und Kreuzprüfung (Variante 1) notiert: ______
- [ ] Profile (und Protokoll) gespeichert; beide Varianten ggf. miteinander verglichen (Abweichung ______ %)

## B. Zweites Backend (PASCO PS-2600A oder LR-2T)

- [ ] Gerät: ______ , Seriennummer laut Anzeige: ______
- [ ] Wellenlängenkalibrierung (Cd + Hg oder Gerätestandard) durchgeführt/geprüft, Fingerprint notiert
- [ ] Identischer Ablauf wie A2-A4 ohne Codeänderung (Lampe aus der Bibliothek, Optikkonfiguration, Absolut und/oder Relativ)
- [ ] Ergebnisse: gültiger Bereich ______ nm, Stabilität ______ %, Kreuzprüfung ______ %
- [ ] Protokoll abgelegt
- [ ] Nur PASCO: meldet keine Seriennummer, Profile sind daher ungebunden (Zustand "unbound"); geprüft und dokumentiert, dass die Profile trotzdem nur für dieses Backend auswählbar sind: ______
- [ ] Bei Gerät ohne Verschluss: Aufforderung zum Abdecken/Aufdecken erscheint genau einmal

## C. Wiederholbarkeit (zwei Tage)

- [ ] Tag 1: Kalibrierung einer Konfiguration (z. B. A3), Protokoll und Profilexport (Reiter "Profiles", Export) abgelegt
- [ ] Tag 2: gleiche Konfiguration, gleicher Aufbau, erneut kalibriert (neue Version)
- [ ] Vergleich beider Versionen (CSV-Protokolle bzw. Profilexporte, K(λ) bzw. S(λ) im gültigen Bereich): Abweichung p95 ______ %, max ______ %
- [ ] Abweichung und mögliche Ursachen (Lampenalter, Steckverbindung, Temperatur) dokumentiert
- [ ] Beide Versionen im Reiter "Profiles" sichtbar; die neueste gültige ist Standard

## D. Querprüfung Kontrollquelle (CC-Profil gegen Blankfaser-Profil)

- [ ] Kontrollquelle (nicht die Kalibrierlampe, z. B. LED/Leuchtstoff/zweite Lampe): ______
- [ ] Messung mit CC-Profil (absolut): CCT ______ K, x ______ , y ______
- [ ] Messung mit Blankfaser-Profil (relativ), gleiche Quelle und Position: CCT ______ K, x ______ , y ______
- [ ] Differenz: ΔCCT ______ K, Δx ______ , Δy ______ ; Bewertung: ______
- [ ] Bei Abweichung: Ausrichtung/Inhomogenität der Quelle als Ursache geprüft (kein Kalibrierfehler, siehe Grenzen)

## E. Anwendung in der Messung

- [ ] Hauptfenster: Dropdown "Response correction" zeigt Name · Typ · Version · Datum; Profil wählbar
- [ ] Statuszeile "Calibration status" zeigt aktives Profil, gültigen Bereich und Warnungen
- [ ] Mit aktivem Profil wird immer ein Dunkelspektrum pro Pixel abgezogen (unabhängig vom Dunkelmodus). Statuszeile prüfen: "dark: stored (this t_int)" bei der Assistenten-t_int bzw. t/2, sonst "dark: per-pixel model b+r·t". Optional zusätzlich je t_int "Processing → Stored dark → Capture dark" (Eingang abgedeckt) und Ergebnis vergleichen.
- [ ] Gegenprobe: alle Dunkelspektren des Geräts entfernen (dark_spectra.json sichern/umbenennen) → Statuszeile zeigt die Warnung "No per-pixel dark spectrum …"
- [ ] Drei Integrationszeiten (z. B. t, t/2, t/4 innerhalb der Linearität): Spektren mit aktivem Profil deckungsgleich; Abweichung ______ %
- [ ] Desktop und Web: dieselben Profile wählbar; dieselbe Messung als CSV aus Desktop und Web exportiert und verglichen (Werte identisch, Einheit in Kopf/Achse): ______
- [ ] Web: keine Kalibriererfassung angeboten (erwartet); Profilauswahl und -verwaltung vorhanden
- [ ] Lux: Absolutprofil, Lux-Anzeige gegen Luxmeter an derselben Stelle: Software ______ lx, Luxmeter ______ lx, Abweichung ______ %
- [ ] Einheitenumschaltung `irradiance_unit` (`uW/cm2/nm` und `W/m2/nm`): Wert ändert sich um Faktor 100 (1 µW/(cm²·nm) = 0,01 W/(m²·nm)); Achsenbeschriftung stimmt
- [ ] Ohne Profil: `display_unit` `counts` und `counts_per_s` umschalten; Achsenbeschriftung stimmt
- [ ] Maskierung: Bereich außerhalb des gültigen Bereichs ist grau (bzw. NaN, im CSV leer); kein Gain 1 dort

## F. Schutzfunktionen

- [ ] Zweites Gerät desselben Typs (z. B. zweites HDX, ersatzweise Demo mit anderer `demo_serial`): Profile des ersten Geräts erscheinen nicht in der Auswahl
- [ ] "Profil auf dieses Gerät kopieren…" ausgeführt; Kopie ist an die neue Seriennummer gebunden und vermerkt die Herkunft (`copied_from`)
- [ ] Wellenlänge am Gerät neu kalibriert: Profil wird als "Wellenlänge geändert – neu kalibrieren" markiert; Anwendung nur nach Bestätigung (ohne Bestätigung nicht angewendet)
- [ ] HDX: `ocean_nonlinearity_correction` nach der Kalibrierung umschalten und neu verbinden. Das Profil wird mit „linearisation changed“ markiert und nur nach Bestätigung angewendet, Statuszeile und Web zeigen die Ursache. Zurückschalten und neu verbinden: Markierung entfällt ohne erneute Bestätigung.
- [ ] Wellenlänge wieder auf den ursprünglichen Stand gebracht bzw. Neukalibrierung durchgeführt: Fingerprint passt, Markierung entfällt (soweit zutreffend)
- [ ] Altes Profil (vor Migration, aus dem Backup `calibration_profiles.v1.bak.json`): wird als relativ, Optik "unknown", ungebunden geführt; Ergebnis ist unverändert gegenüber dem Stand vor der Umstellung (Referenzspektrum vorher/nachher verglichen)
- [ ] Ohne aktives Profil (Auswahl "None"): Anzeige und Exporte unverändert gegenüber vor der Umstellung

## G. Fehlerbilder

- [ ] Sättigung: absichtlich zu hohe t_int; Kalibrierung wird mit Meldung zur Sättigung abgebrochen bzw. abgelehnt, kein Profil gespeichert
- [ ] Dunkel nicht abgedeckt: Dunkelspektrum bei offenem Eingang; Warnung "Dunkelspektrum ungewöhnlich hell / Licht blockiert?"; Ergebnis nicht als gut gewertet
- [ ] t_int-Abweichung: Dunkel und Hell mit unterschiedlicher t_int (oder Kreuzprüfung bewusst verletzt, z. B. durch Verstellen der Lampe); Warnung erscheint, Prüfergebnis "nicht bestanden" im Protokoll
- [ ] Stabilität verletzen (Lampe nicht aufgewärmt oder Faser bewegt): Stabilitätswarnung erscheint und steht im Protokoll
- [ ] Zu geringes Signal (SNR): betroffene Pixel maskiert, gültiger Bereich schrumpft entsprechend
- [ ] Fehlerbilder haben keine Profile mit unplausiblen Werten hinterlassen (Reiter "Profiles" prüfen, ggf. löschen)

## Ergebnisse (ausfüllen)

| Test | Gerät / Seriennummer | Optik | Gültiger Bereich (nm) | Stabilität (%) | Kreuzprüfung (%) | Ergebnis / Bemerkung | OK |
|---|---|---|---|---|---|---|---|
| A3 absolut CC | | | | | | | [ ] |
| A4 relativ blank (direkt) | | | | | | | [ ] |
| A4 relativ blank (abgeleitet) | | | | | | | [ ] |
| B zweites Backend | | | | | | | [ ] |
| C Wiederholbarkeit | | | | | | Abweichung p95 / max: | [ ] |
| D Querprüfung | | | | | | ΔCCT / Δxy: | [ ] |
| E Anwendung (3 t_int) | | | | | | Abweichung: | [ ] |
| E Lux vs. Luxmeter | | | | | | Abweichung: | [ ] |
| F Schutzfunktionen | | | | | | | [ ] |
| G Fehlerbilder | | | | | | | [ ] |

## Abzulegende Protokolle (Abnahme Phase 4)

- [ ] Kalibrierprotokolle (PDF und CSV) aller Läufe: A3, A4 (beide Varianten), B, C (Tag 1 und Tag 2)
- [ ] Profilexporte der verwendeten Profile (Reiter "Profiles", Export)
- [ ] Wellenlängenkalibrierung: Linienlisten/Residuen (Screenshot oder Notiz) je Gerät
- [ ] Vergleich Wiederholbarkeit (C) mit Abweichungswerten
- [ ] Querprüfung (D) mit CCT und xy
- [ ] CSV-Exporte Desktop und Web (E) sowie Luxvergleich
- [ ] Ausgefüllte Ergebnis-Tabelle und diese Checkliste
- [ ] Abnahme: Protokolle aller Durchläufe liegen vor; Ablauf war ohne Codeänderung für beide Backends und alle Optiken wiederholbar

## Bekannte Einschränkungen

- Unterhalb der Gültigkeitsgrenze der Referenzquelle (HAL-CAL: ca. 380 nm) gibt es keine Kalibrierung; beim HDX (Anzeige ab 350 nm) bleibt 350-380 nm maskiert.
- Streulicht aus dem starken Rot/NIR-Anteil der Halogenlampe erhöht die Unsicherheit im Blau.
- Blanke Faser: bei räumlich oder winkelabhängig inhomogenen Quellen hängt das Spektrum von der Ausrichtung ab (kein Kalibrierfehler).
- Absolut nur mit definierter Eintrittsfläche (CC, Kugel); Profile mit Optik "bare" sind immer relativ. Kein Strahlungs-/Lichtstrom, keine Strahldichte.
- S und K gelten nur für die Wellenlängenkalibrierung (Fingerprint), Optikkonfiguration und das Gerät (Seriennummer), mit denen sie erzeugt wurden.
- Die Erfassung der Kalibrierung ist nur am Desktop möglich; im Web gibt es Profilauswahl und -verwaltung, aber keine Erfassung.
- Das Demo-Backend ist reine Software: `demo_sensitivity = synthetic` und `demo_serial` dienen nur zum Testen des Ablaufs und ersetzen keinen Hardwaretest; Dunkelverschluss ist dort simuliert.
- PASCO meldet keine Seriennummer; Profile sind ungebunden (kein Schutz vor Verwechslung zweier Geräte).
- Stabilitäts- und Kreuzprüfung sind Warnungen, keine Sperren; bestandene Prüfungen ersetzen keine Unsicherheitsbetrachtung der Lampenkalibrierung.

### Software-seitig bekannte Punkte (aus dem Code-Review, nicht behoben)

- **Fast Preview:** Hochskalierte Fast-Preview-Frames tragen im Treiber `ratio = 1.0`. Der Kalibrierpfad schaltet Fast Preview und Auto-Exposure während der Erfassung ab und verwirft den ersten Frame. Fast Preview trotzdem vor dem Kalibrieren ausschalten.
- **Nach Belichtungswechsel:** Unmittelbar nach einem t_int-Wechsel kann die Anzeige-Mittelung (frames_to_average, Fusion) kurz Frames verschiedener t_int mischen. Nach einem Wechsel bei der Messung mit Profil 1–2 s warten, bis sich die Anzeige beruhigt hat.
- **Ocean-Linearisierung:** Der Treiber linearisiert um den Median der dunkelsten Pixel desselben Frames, also für Hell- und Dunkelframe um verschiedene Basislinien. Das gibt einen kleinen systematischen Fehler, der in A3 mit der t_int-Kreuzprüfung sichtbar wird.
- **Ohne Seriennummer ungebunden:** Geräte ohne Seriennummer (PASCO ohne USB-iSerial, LR-2T ohne Seriennummer in der Geräte-ID) erzeugen Profile mit dem Kennzeichen „unbound“. Sie erscheinen auf jedem Gerät dieses Typs. Bei zwei gleichen Geräten am selben Rechner die Profile daher eindeutig benennen.
- **Gültiger Bereich und Farbmetrik:** Deckt der gültige Bereich 380–780 nm nicht vollständig ab, gehen die maskierten Pixel mit 0 in Lux, xy und CCT ein. Lux und Farbe sind dann zu niedrig bzw. verschoben. Es gibt dazu keine Warnung.
- **Alter des Dunkelspektrums:** Dunkelspektren haben kein Verfallsdatum. Nach einer Temperaturänderung oder an einem neuen Tag neu erfassen.
