# PS-2600A Spectrometer – Projektregeln

Diese Regeln gelten für jede Änderung in diesem Repository, unabhängig vom jeweiligen Auftrag.

## Repository
- Standard-Branch ist `future`. Neue Arbeit auf einem eigenen Branch von `future`, ein Commit je in sich geschlossenem Block.
- Kein Push und kein Merge ohne ausdrückliche Anweisung.
- Nicht committen: Laufzeitdaten, Exporte, Protokolle, `.claude/`, Build-Artefakte. Es gibt keinen exe-Build (kein PyInstaller).

## Daten
- Echte Laufzeitdaten liegen im Benutzerdatenverzeichnis (Windows `%APPDATA%\PS-2600A\`). Sie dürfen bei Entwicklung, Tests und Prüfläufen **nie** gelesen oder verändert werden.
- Jeder Testlauf, Demo-Durchlauf oder Start der Anwendung zu Prüfzwecken – auch durch Subagenten – erfolgt mit `PS2600A_DATA_DIR` auf ein temporäres Verzeichnis.
- Exportformate (CSV-Köpfe, Spalten) sind eine Schnittstelle. Änderungen daran im Abschlussbericht ausdrücklich aufführen.

## Architektur
- Logik existiert genau einmal. Geräte-, Verarbeitungs-, Kalibrier- und Exportlogik liegt in den Qt-freien Kernmodulen (`engine.py`, `processing.py`, `calibration_*.py`, `api.py` u. a.). Desktop (Qt) und Web nutzen dieselben Funktionen.
- Qt-freie Module importieren PyQt weder direkt noch indirekt (durch Test abgesichert).
- Bestehende API-Pfade unter `/api/...` und `/api/v1/...` bleiben kompatibel.
- Bezeichner, Bedientexte, Dateinamen, API-Endpunkte, MCP-Werkzeuge, Agents und Skills auf Englisch.

## Oberfläche
- Fenster über `gui_layout.py` an den verfügbaren Bildschirmbereich binden; Tabellen über `gui_tables.py`; Gestaltung ausschließlich über das zentrale Theme (`gui_theme.py`).
- Keine Layoutsprünge, keine abgeschnittenen Texte, keine ungestylten Flächen, keine „nan“-Anzeigen.
- Sachliche Bedientexte, keine Bastel- oder Vorgehenshinweise in Protokollen und Berichten. Protokolle und Berichte enthalten Datum und Uhrzeit mit Zeitzone.

## Messtechnik
- Kalibrierdaten werden als Zählrate (counts/s) nach Linearisierung und Dunkelabzug geführt; Integrationszeit und Mittelung dürfen das Ergebnis nicht beeinflussen.
- Maskierte Pixel (außerhalb des gültigen Bereichs) werden als NaN geführt und nie stillschweigend mit Gain 1 weiterverwendet.
- Neue Prüfkriterien und Grenzwerte sind konfigurierbar und werden im Protokoll ausgewiesen.

## Qualität
- `python -m pytest -q` muss grün sein; neue Logik bekommt Tests (Demo-Backend bzw. synthetische Daten; GUI mit `QT_QPA_PLATFORM=offscreen`).
- Größere Aufträge schließen mit einem Review durch den Subagenten `review-opus` ab; Befunde der Schwere kritisch und wichtig werden behoben.
- Abschlussbericht unter `docs/`: umgesetzte Punkte, Entscheidungen, Abweichungen, Testergebnisse, bekannte Einschränkungen.
