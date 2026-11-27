# Hardwaretest Strahldichte (Kugelcharakterisierung, Objektiv-Kalibrierung)

Checkliste zum Abhaken. Grundlage: Branch `feature/radiance`, Handbuch Kapitel „Spectral radiance“ (`docs/manual/manual.tex`, `sec:radiance`), Bericht `docs/ABSCHLUSSBERICHT_radiance.md`.

Eintragen bei jedem Schritt: Datum, Uhrzeit mit Zeitzone, Gerät/Seriennummer, Auffälligkeiten.

## 0. Vorbereitung

- [ ] Stand notiert (`git rev-parse --short HEAD`): ______
- [ ] `python -m pytest -q` grün (Ergebnis): ______
- [ ] Sicherung des Benutzerdatenverzeichnisses (`%APPDATA%\PS-2600A\`, Kopie mit Datum) angelegt
- [ ] Gerät verbunden, Wellenlängenkalibrierung und ggf. Linearitätskalibrierung aktuell
- [ ] Kugel: Lampe mindestens 20 min eingeschaltet (Drift), Leistung unverändert
- [ ] Kugel am Teil vermessen:
  - [ ] Portdurchmesser an der Messerkante: ______ mm (Nennwert 14,00 mm), Unsicherheit u(r): ______ mm
  - [ ] Abstand Messerkantenebene – Gehäuseoberseite: ______ mm (Nennwert 11,00 mm), Unsicherheit u(d): ______ mm
  - [ ] Lage der CC-Referenzebene relativ zur CC-Auflagefläche (Datenblatt/Messung): ______ mm (Vorgabe 0)
- [ ] Distanzstücke für mindestens drei Abstände bereit (Höhe über Gehäuseoberseite): ______ / ______ / ______ mm

## A. CC-Absolutprofil vorhanden

- [ ] Profilliste: absolutes Profil, Messgröße `irradiance`, Eingang `CC`, Gerät und Seriennummer stimmen
- [ ] Profil ist aktiv; Status `ok` (kein „wavelength calibration changed“ / „linearisation changed“)
- [ ] Verifikation des CC-Profils gegen die Kalibrierlampe liegt vor (Bandabweichung ≤ 2 %): ______
- [ ] Gegenprobe: ohne aktives CC-Absolutprofil (z. B. „None“ oder ein relatives Profil aktiv) wird die Kugelcharakterisierung mit klarer Meldung abgelehnt (Desktop und Web)
- [ ] Unsicherheit der CC-Kalibrierung für die Eingabe festgelegt (Zertifikat Lampe + Verifikation): ______ %

## B. Kugel vermessen (Charakterisierung)

- [ ] Desktop: Calibration → Spectral calibration → Modus „Characterise a sphere (radiance reference)“ (bzw. Web: Kalibrierdialog, gleicher Ablauf)
- [ ] Eingaben: Name/Kennung der Kugel, Portradius, Abstand Messerkantenebene–Gehäuseoberseite, CC-Referenzebene, u(r), u(d), u(CC)
- [ ] CC zentriert auf dem Port, Auflagefläche parallel zur Gehäuseoberseite
- [ ] Integrationszeit (Auto) beim kleinsten Abstand: ______ ms, Aussteuerung ______ % FS
- [ ] Dunkel vor (Licht blockiert)
- [ ] Abstand 1: Höhe ______ mm → d = ______ mm, Aussteuerung ______ %
- [ ] Abstand 2: Höhe ______ mm → d = ______ mm, Aussteuerung ______ %
- [ ] Abstand 3: Höhe ______ mm → d = ______ mm, Aussteuerung ______ %
- [ ] (optional) Abstand 4: Höhe ______ mm → d = ______ mm
- [ ] Wiederholung des ersten Abstands: Drift p95 (über die Bandbreite geglättet) ______ % (Grenze 1 %)
- [ ] Dunkel nach
- [ ] Ergebnis:
  - [ ] Versatz d0 (gesamt): ______ mm, plausibel (|d0| < 1 mm)? ______; d0 je Band streut um ______ mm
  - [ ] Verwendetes Verfahren: Ausgleichsrechnung / Mittelwert
  - [ ] Konsistenz (Streuung von L über die Abstände) gesamt ______ %, schlechtestes Band ______ (Grenze 2 %)
  - [ ] Unsicherheit u(L) Median ______ %, U (k=2) ______ %; größter Beitrag: ______
  - [ ] Gültiger Bereich: ______ – ______ nm (innerhalb des CC-Profilbereichs)
- [ ] Referenzquelle in der Lampenbibliothek: Messgröße `radiance`, Einheit µW/(cm²·sr·nm), Metadaten (Geometrie, Datum, Gerät, CC-Profil-ID/Version) vorhanden
- [ ] Protokoll PDF und CSV abgelegt, Datum/Uhrzeit mit Zeitzone, alle Grenzwerte ausgewiesen
- [ ] Plausibilität: Verlauf von L(λ) glatt, Form wie die Kugellampe (z. B. Halogen); keine Sprünge an den Bandgrenzen

## C. Objektiv-Kalibrierung (absolut, Strahldichte)

- [ ] Port unter dem Objektiv, Fokus auf die Portebene; Sehfeld vollständig im Port (Bild des Ports deutlich größer als das Sehfeld)
- [ ] Spektralkalibrier-Assistent: Eingang `objective`, Typ `absolute`, Referenz = vermessene Kugel; Hinweis zum Ausfüllen des Sehfelds wird angezeigt
- [ ] Gegenprobe Zulässigkeit: `objective` + absolute Bestrahlungsstärke-Referenz wird abgelehnt; `CC` + Strahldichte-Referenz absolut wird abgelehnt
- [ ] Lauf vollständig (Dunkel vor/nach, Stabilität, t/2-Kreuzprüfung): Ergebnis ______
- [ ] Profil gespeichert: Typ `absolute`, Messgröße `radiance`, Einheit µW/(cm²·sr·nm) pro count/s; abgeleitetes relatives Profil vorhanden
- [ ] Verifikation gegen die Kugel: Bandabweichung absolut ______ % (erwartet im Rahmen von u(L))
- [ ] Wiederholung mit leicht verschobenem Fokus / seitlichem Versatz innerhalb des Ports: Änderung ______ % (Gleichmäßigkeit)

## D. Plausibilität der Leuchtdichte

- [ ] Messung der Kugel mit aktivem Objektivprofil: Anzeige in µW/(cm²·sr·nm), Farbtab zeigt „Luminance … cd/m²“ statt Lux (Desktop und Web)
- [ ] Leuchtdichte der Kugel: ______ cd/m²
- [ ] Gegenrechnung aus der CC-Messung am Port: L_v ≈ E_v · (r² + d²) / (π r²) mit E_v (lx) bei Abstand d: ______ cd/m²; Abweichung ______ %
- [ ] Optional Vergleich mit einem Leuchtdichtemessgerät: ______ cd/m²; Abweichung ______ %
- [ ] Export CSV: Kopf enthält Messgröße und Einheit; API `GET /api/v1/spectrum/latest` liefert `quantity = radiance` und Einheit
- [ ] MCP `calibration_status` / `measure`: Messgröße und Einheit, Leuchtdichte in cd/m²

## E. Abschluss

- [ ] Benutzerdaten unverändert bis auf die neuen Profile/Quellen (Vergleich mit der Sicherung)
- [ ] Ergebnisse in `docs/BEFUNDE_hardwaretest.md` eingetragen

| Abschnitt | Datum/Uhrzeit | Ergebnis | OK |
|---|---|---|---|
| A CC-Absolutprofil | | | [ ] |
| B Kugelcharakterisierung | | d0 / Konsistenz / U: | [ ] |
| C Objektiv-Kalibrierung | | | [ ] |
| D Leuchtdichte | | Abweichung: | [ ] |
