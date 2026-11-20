# Plan: Generische spektrale Kalibrierung (Wellenlänge, relativ, absolut)

Arbeitsplan für die Umsetzung in Claude Code im Repo `doctormord/Pasco-PS-2600A-Spectrometer`.
Stand: 2026-11-20, Revision 3

---

## 1. Ziel und Grundsatz

Ein **einheitlicher, beliebig wiederholbarer Kalibrierprozess** für alle Backends (`BaseSpectrometer`: Ocean HDX, PASCO PS-2600A, LR-2T, Demo) und beliebige Eingangsoptiken (Fasern mit/ohne Cosine-Corrector). Keine Sonderlösung für ein Gerät, eine Faser oder eine Lampe.

| Kalibrierung | Ergebnis | Gilt für | Einheit |
|---|---|---|---|
| Wellenlänge | Pixel → λ (Polynom) | Gerät | nm |
| Relativ | Empfindlichkeit S(λ), Ausgabe ∝ spektrale Bestrahlungsstärke | Gerät + Optikkonfiguration | relativ (Peak = 1) |
| Absolut | Faktor K(λ), Ausgabe als spektrale Bestrahlungsstärke | Gerät + Optikkonfiguration mit definierter Eintrittsfläche (CC) | µW/(cm²·nm) |

**Optikkonfiguration** = frei benannte Kombination aus Faser, Eingangsoptik (CC / blank / später Kugel) und Steckverbindung. Für jede Konfiguration kann jederzeit neu kalibriert werden; jede Kalibrierung erzeugt eine neue Profilversion.

Nicht Ziel: Strahlungs-/Lichtstrom (Ulbricht-Kugel), Strahldichte.

## 2. Festlegungen

1. **Zählrate.** Alle Kalibrier- und Messdaten werden auf `C(λ) = (Roh_linearisiert − Dunkelspektrum) / t_int` [counts/s] bezogen. Mittelung über N Frames als Mittelwert. Dadurch sind S und K unabhängig von Integrationszeit und Mittelung.
2. **Dunkelspektrum pro Pixel** bei gleicher Integrationszeit ist für die Kalibrierung Pflicht. Kein Abschneiden negativer Werte im Kalibrierpfad.
3. **Relativ:** `S = C_cal / E_ref`, normiert; Korrektur `C / S`.
   **Absolut:** `K = E_ref / C_cal`; Messung `E = C · K`.
4. **Gültiger Bereich** = Schnittmenge aus
   - Anzeigegrenzen des Geräts (`spec_range_nm()` bzw. `<device>_spec_min_nm` / `_max_nm`),
   - Definitionsbereich der Referenzquelle (mit konfigurierbarer Untergrenze je Quelle, HAL-CAL: 380 nm),
   - Pixel mit ausreichendem SNR des Kalibriersignals (`C_cal > k·σ_dunkel`).
   Außerhalb wird maskiert (NaN, im Plot ausgegraut), nicht mit Gain 1 weitergerechnet.
5. **Abhängigkeit von der Wellenlängenkalibrierung.** Die Pixelbreite in nm steckt in S und K. Jedes Profil speichert einen Fingerprint der Wellenlängenkalibrierung; bei Abweichung wird es als ungültig markiert.
6. **Absolut nur mit definierter Eintrittsfläche.** Profile mit Optik „blank“ sind immer relativ.
7. **Referenzquellen sind Daten, keine Sonderfälle.** Jede kalibrierte Lampe (Avantes `.lmp`, CSV anderer Hersteller) wird einmal importiert und in einer Bibliothek mit Seriennummer, Einheit, gültigem Bereich und vorgesehener Eingangsoptik geführt.

## 3. Ist-Zustand und Lücken

| Bereich | Stelle | Befund |
|---|---|---|
| Wellenlänge | `gui_calibration.py` Reiter „Wavelength“, `calibration_utils.fit_wavelength_polynomial`, `calibrate_from_lines` je Treiber | generisch vorhanden, mehrere Lampen (z. B. Cd, Hg) kombinierbar; Fingerprint fehlt |
| Gerätegrenzen | `device_manager.BaseSpectrometer.spec_range_nm`, `<device>_spec_min/max_nm` | vorhanden, wird für den gültigen Bereich übernommen |
| Response | `calibration_profiles.compute_response_table` | Schwelle 5 % vom Referenzmaximum (schneidet bei Halogen das Blau ab), Gain 1 außerhalb |
| Gain | `calibration_utils.build_response_gain` | `max_gain = 10` fest |
| Referenz-Import | `gui_calibration._csv_read_spectrum` | nur CSV mit Dezimalpunkt; keine Lampenbibliothek |
| Erfassung | `gui_calibration._Capturer` über `capture_snapshot` | counts statt counts/s, nach Glättung, mit skalarem Dunkelwert |
| Dunkel | `processing.apply_dark_correction`, Ocean: Median der 50 dunkelsten Pixel | nur skalar, Ergebnis `max(0, …)` |
| Sättigung | nur Ocean (`ocean_adc_saturation`) | kein generisches Attribut im Basistreiber |
| Profilspeicher | `calibration_profiles.json` | kein Typ, keine Einheit, keine Optik, kein Fingerprint, keine Versionen |
| Absolut | `radiometric_calibration_factor` (Skalar für Lux) | nicht wellenlängenabhängig |
| Pipeline | `processing.process_frame` (Web), `gui_dashboard` (Desktop) | keine Normierung auf t_int, keine Einheiten |

## 4. Umsetzung (4 Phasen)

**Ausführung in einem Durchgang:** Phasen 1–3 werden ohne Zwischenstopps und ohne Rückfragen nacheinander umgesetzt. Entscheidungen, die der Plan offen lässt, werden nach bestem Ermessen getroffen und im Abschlussbericht aufgeführt. Die Abnahmekriterien der Phasen 1–3 werden selbst geprüft (pytest, Demo-Backend), Fehler werden im selben Durchgang behoben. Phase 4 ist der Hardwaretest durch den Nutzer; am Ende wird dafür eine Testanleitung übergeben.

Arbeitsweise: Branch `feature/spectral-calibration`, ein Commit je Phase, kein Push ohne Rückfrage. Rechenlogik nur in geräteunabhängigen Modulen (`calibration_profiles.py`, `processing.py`, neues `reference_sources.py`); Desktop und Web liefern bitidentische Ergebnisse. Bestehende Profile und das Verhalten ohne aktives Profil bleiben unverändert.

### Phase 1 – Grundlagen: Daten, Dunkelspektrum, Erfassung

- **Basistreiber:** `BaseSpectrometer` um `serial`, `adc_full_scale` (Sättigung) und `wl_fingerprint()` erweitern; alle Backends implementieren das (Ocean, PASCO, LR-2T, Demo).
- **Referenzquellen** (`reference_sources.py`): Parser für Avantes `.lmp` (Tab/Leerzeichen, Dezimalkomma/-punkt, CRLF, optionale Indexspalte) und CSV; Bibliothek als JSON (Name, Seriennummer, Einheit, untere/obere Gültigkeitsgrenze, Eingangsoptik, Quelldatei, Importdatum); Interpolation auf die Pixelachse per PCHIP.
- **Dunkelspektrum pro Pixel:** Erfassung, Speicherung mit t_int und Zeitstempel je Gerät; zusätzlich als Dunkelmodus „Stored dark“ in der Hauptpipeline nutzbar.
- **Kalibriererfassung (generisch):** Rohdatenpfad nach Linearisierung, vor Glättung/Response/Fusion; N Frames mitteln, Dunkelspektrum abziehen, durch t_int teilen. Sättigungsprüfung je Pixel gegen `adc_full_scale`; Warnung bei abweichender t_int zwischen Dunkel und Hell.
- **Profilschema v2** mit Migration (alte Profile → `type: relative`, Optik „unbekannt“):
  ```json
  {
    "schema": 2,
    "type": "relative | absolute",
    "device": {"backend": "...", "serial": "..."},
    "optics": {"name": "frei", "input": "CC | bare | sphere", "notes": ""},
    "reference": {"name": "...", "serial": "...", "unit": "uW/cm2/nm"},
    "wl_fingerprint": "...",
    "valid_range_nm": [lo, hi],
    "table": [[lambda_nm, value], ...],
    "unit": "relative | uW/cm2/nm per count/s",
    "acquisition": {"integration_us": 0, "averages": 0, "nl_correction": true},
    "checks": {"stability_pct": 0, "itime_crosscheck_pct": 0},
    "created": "ISO-8601 mit Zeitzone",
    "version": 1
  }
  ```
  Profile werden je (Gerät, Optikkonfiguration, Typ) versioniert; die neueste gültige Version ist Standard, ältere bleiben abrufbar.
- **Tests (pytest, neu):** Parser mit der vorliegenden `.lmp` (441 Punkte, 300–2500 nm, Maximum 401,25 bei 1250 nm); synthetische Rückgewinnung von S und K (< 0,1 %); Unabhängigkeit von t_int; Migration alter Profile.

**Abnahme:** Tests grün; Demo-Backend liefert counts/s-Erfassung mit Dunkelspektrum; bestehende Profile laden unverändert.

### Phase 2 – Kalibrierassistent und Rechnung

- Reiter „Response“ ersetzen durch einen Assistenten mit festem, für alle Geräte identischem Ablauf:
  1. Gerät, Optikkonfiguration (neu anlegen oder wählen), Eingangsoptik, Referenzquelle aus der Bibliothek wählen; Typ relativ/absolut (absolut nur bei Eingangsoptik ≠ blank).
  2. Prüfung Wellenlängenkalibrierung vorhanden (Fingerprint).
  3. Integrationszeit setzen bzw. Auto-Belichtung auf Zielpegel (z. B. 70–85 % Full Scale), dann fixieren.
  4. Dunkelspektrum erfassen.
  5. Kalibrierspektrum erfassen; **Stabilitätsprüfung** automatisch (zwei Erfassungen im Abstand, Abweichung im gültigen Bereich < Schwelle).
  6. **Integrationszeit-Kreuzprüfung** automatisch (zweite Erfassung bei ca. halber t_int inkl. eigenem Dunkel, Abweichung von C(λ) < Schwelle).
  7. Berechnung S bzw. K, gültiger Bereich nach Abschnitt 2.4, Anzeige von C_cal, E_ref, Ergebnis und Gain-Spanne.
  8. Speichern als neue Profilversion; bei absolut zusätzlich das abgeleitete relative Profil.
- `compute_response_table` um SNR-basierte Gültigkeit erweitern; `max_gain` konfigurierbar; optionale Glättung mit kleinem Fenster. Der bisherige Weg „Referenz = zweite Messung“ bleibt als Option erhalten.
- Ableitung einer Optikkonfiguration aus einer anderen (z. B. blank aus CC) über das Verhältnis zweier Messungen derselben diffusen, ausgedehnten Quelle: `S_neu = S_bekannt · C_neu / C_bekannt`.
- **Kalibrierprotokoll** als PDF und CSV je Lauf: Datum und Uhrzeit, Gerät und Seriennummer, Optikkonfiguration, Referenzquelle, t_int, N, gültiger Bereich, Ergebnisse der Stabilitäts- und Integrationszeitprüfung, Plots.
- Export/Import einzelner Profile als Datei.

**Abnahme:** vollständiger Durchlauf mit Demo-Backend (synthetische Lampe) reproduziert die vorgegebene Empfindlichkeit; zwei aufeinanderfolgende Läufe ergeben Profile innerhalb der Stabilitätsschwelle; Protokoll wird erzeugt.

### Phase 3 – Anwendung in der Messung (Desktop und Web)

- `processing.process_frame` und Desktop-Pipeline: Normierung auf t_int, Anwendung von S oder K, Maskierung außerhalb des gültigen Bereichs; Y-Achse und Exporte mit Einheit (counts, counts/s, relativ, µW/(cm²·nm), W/m²/nm).
- Lux/CIE: bei Absolutprofil `illuminance_lux` direkt aus E(λ); `radiometric_calibration_factor` dann ignoriert. Farbmetrik nutzt bei Relativprofil die korrigierte Verteilung.
- Profilauswahl je Gerät nach Optikkonfiguration (Dropdown zeigt Name, Typ, Version, Datum); Statusanzeige des aktiven Profils.
- Schutz:
  - Profile sind an die Seriennummer gebunden. Bei einem anderen Gerät desselben Typs (z. B. zweites HDX) erscheinen die Profile des ersten nicht in der Auswahl. Bewusstes Übernehmen ist über „Profil auf dieses Gerät kopieren“ möglich und wird im Profil vermerkt.
  - Weicht der Fingerprint der aktuellen Wellenlängenkalibrierung vom gespeicherten ab, wird das Profil als „Wellenlänge geändert – neu kalibrieren“ markiert und nur nach Bestätigung angewendet.
- Web: `/api/calibration/*` auf Schema v2, Einheiten in Plot und Export; Erfassung im Web nicht in dieser Phase.

**Abnahme:** gleiche Messung bei drei Integrationszeiten liefert mit aktivem Profil deckungsgleiche Spektren; Desktop und Web liefern identische Werte; Lux aus Absolutprofil plausibel gegen Luxmeter.

### Abschluss der Umsetzung (nach Phase 3)

- `docs/manual/manual.tex`: Kapitel Kalibrierung (Begriffe, Gültigkeit, Ablauf, Grenzen).
- Abschlussbericht: umgesetzte Punkte, getroffene Entscheidungen, Abweichungen vom Plan, Testergebnisse (pytest, Demo-Backend).
- Testanleitung für Phase 4 als Checkliste (`docs/TEST_kalibrierung.md`).

### Phase 4 – Hardwaretest durch den Nutzer

- Durchlauf am Ocean HDX: Wellenlänge (Cd + Hg), absolut mit CC-VIS/NIR und HAL-CAL, relativ mit blanker Faser.
- Durchlauf an mindestens einem weiteren Backend (PASCO oder LR-2T) mit identischem Ablauf.
- Wiederholbarkeit: Kalibrierung derselben Konfiguration an zwei Tagen; Abweichung dokumentieren.
- Querprüfung: eine Kontrollquelle mit CC-Profil und Blankfaser-Profil messen, CCT und xy vergleichen.

**Abnahme:** Protokolle aller Durchläufe liegen vor; Ablauf ist ohne Codeänderung für jedes Backend und jede Optik wiederholbar.

## 5. Messablauf (für jedes Gerät und jede Optik gleich)

1. Wellenlängenkalibrierung des Geräts mit Linienlampen (z. B. Cd und Hg) über den gesamten Anzeigebereich; danach nicht mehr ändern, sonst Response neu kalibrieren.
2. Optikkonfiguration festlegen und benennen; Steckverbindungen bis zum Ende der Messreihe nicht lösen.
3. Gerät und Lampe aufwärmen; Assistent durchlaufen (Abschnitt 4, Phase 2).
4. **Absolut:** Eingangsoptik mit definierter Fläche (CC) in der Aufnahme der Referenzlampe, für die deren Kalibrierung gilt.
   **Relativ, blanke Faser:** Faser reproduzierbar zentriert im Lampenport (Adapterhülse) oder Ableitung über diffuse Quelle (Phase 2).
5. Protokoll ablegen.

## 6. Bekannte Grenzen

- Unterhalb der Gültigkeitsgrenze der Referenzquelle (HAL-CAL: ca. 380 nm) keine Kalibrierung; beim HDX (Anzeige ab 350 nm) bleibt 350–380 nm maskiert.
- Streulicht aus dem starken Rot/NIR-Anteil einer Halogenlampe erhöht die Unsicherheit im Blau.
- Blanke Faser: bei räumlich oder winkelabhängig inhomogenen Quellen hängt das Spektrum von der Ausrichtung ab (kein Kalibrierfehler).
